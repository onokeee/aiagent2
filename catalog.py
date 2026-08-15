"""データカタログ層。

「自動プロファイル（機械の知識）」と「サイドカーYAML（人間の知識）」を統合し、
UI表示・ER図・LLM用 system prompt を **同じ情報源** から生成する。

ファイル配置:
  data/sales.db                                 … DB本体（読み取り専用で扱う）
  data/sales.db.meta.yaml                       … メタ情報（全員で1つ。編集は管理者のみ）
  data/.profile_cache/sales.db.profile.json     … 自動プロファイル（mtime+sizeで自動再生成）

メタ情報(YAML)の構造:
  title: 受注管理DB
  description: ...
  caveats: [ ... ]
  tables:
    orders:
      description: 受注明細。1行 = 1受注明細行。
      ai_draft: true          # AI下書きのまま人間が未確認ならtrue
      columns:
        status: { description: 受注状態, values: { "1": 受付, "2": 出荷済 } }
      glossary:               # そのテーブル固有の業務用語
        有効な受注:
          description: キャンセル以外の、実際に売上になる受注   # 自然言語だけでもよい
          sql: status != '9'                                  # あればAIはこの式をそのまま使う
  relationships:
    - { from: orders.customer_id, to: customers.id, cardinality: "N:1" }
      # to には "他DBエイリアス.テーブル.列" の3要素形式も書ける
  glossary:                   # テーブルをまたぐ業務用語だけをここに書く
    稼働率: { description: 実働時間÷所定時間 }
  examples:
    - q: 今月の売上は？
      description: 締め日は月末。キャンセルは除く   # 任意。この例の読み方をAIに伝える
      sql: SELECT ...
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import yaml

import config
import db

# =============================================================================
# メタ情報（サイドカーYAML）
# =============================================================================

_META_KEYS = ("title", "description", "caveats", "tables", "relationships", "glossary",
              "examples", "checks", "er_layout", "er_external", "tools", "builtin_tools")


# カタログは全員で1つ。DBの中身が何かは人によって変わらないので、
# 定義を分けると「同じ質問なのに人によって答えが違う」ことになる。
#   data/<DB>.db.meta.yaml … 唯一のカタログ。書き換えるのは管理者だけ
#                            （画面側は web/catalog_bp.py が admin_required で守る）

def meta_path(db_path) -> Path:
    """カタログの置き場所。DBファイルの隣に同じ名前で置く。"""
    return Path(str(db_path) + ".meta.yaml")


def _read_yaml(p: Path) -> dict:
    if not p.exists():
        return {}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"[catalog] メタ情報を読めませんでした: {p} ({e})")
        return {}


def load_meta(db_path) -> dict:
    """カタログを読む（全員が同じものを見る）。"""
    return _read_yaml(meta_path(db_path))


def save_meta(db_path, meta: dict) -> None:
    """カタログを保存する（内容をまるごと書く）。呼べるのは管理者の画面だけ。"""
    target = meta_path(db_path)
    cleaned = {}
    for k in _META_KEYS:
        v = meta.get(k)
        if v in (None, "", [], {}):
            continue
        cleaned[k] = v

    target.parent.mkdir(parents=True, exist_ok=True)
    if not cleaned:
        target.write_text("", encoding="utf-8")
        return
    target.write_text(
        yaml.safe_dump(cleaned, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )


# =============================================================================
# 業務用語（用語集）
# =============================================================================
#
# 用語は「テーブル固有」と「テーブルをまたぐもの」の2種類あるので、置き場所も2つ。
#   meta["tables"][テーブル名]["glossary"]  … そのテーブルの用語（基本はこちら）
#   meta["glossary"]                        … 複数テーブルにまたがる用語
# テーブル側に置くと、そのテーブルが選択されているときだけプロンプトに載る。
#
# 1つの用語は次の2つを持つ。どちらか一方だけでもよい。
#   description … 自然言語の説明（AIはこれを読んで自分でSQLを組み立てる）
#   sql         … SQLの条件式や計算式（あればAIはこの式をそのまま使う）

def normalize_glossary(gl) -> dict:
    """用語集を {用語: {"description":…, "sql":…}} の形に揃える。

    値は必ず辞書。手でYAMLを書いて文字列になっていた場合は「説明」として扱う。
    以前はSQL式として扱っていたが、説明文が書かれていると
    「この式をそのまま使う」とAIに渡してしまい、構文エラーのSQLを作らせていた。
    説明として扱えば、間違っていてもAIが列情報から組み立て直せる。
    """
    out = {}
    for term, val in (gl or {}).items():
        term = str(term).strip()
        if not term:
            continue
        if isinstance(val, dict):
            desc = str(val.get("description") or "").strip()
            sql = str(val.get("sql") or "").strip()
        else:
            print(f"[catalog] 用語 '{term}' が古い書き方です。説明として扱います。")
            desc, sql = str(val or "").strip(), ""
        if desc or sql:
            out[term] = {"description": desc, "sql": sql}
    return out


def table_glossary(meta: dict, tname: str) -> dict:
    """テーブル固有の用語。"""
    return normalize_glossary(((meta.get("tables") or {}).get(tname) or {}).get("glossary"))


def db_glossary(meta: dict) -> dict:
    """テーブルをまたぐ用語。"""
    return normalize_glossary(meta.get("glossary"))


def set_table_glossary(meta: dict, tname: str, gl: dict) -> None:
    """テーブル固有の用語を書き戻す（空なら削除）。"""
    tm = meta.setdefault("tables", {}).setdefault(tname, {})
    if gl:
        tm["glossary"] = gl
    else:
        tm.pop("glossary", None)
        if not tm:
            meta["tables"].pop(tname, None)


def glossary_lines(gl: dict) -> list[str]:
    """プロンプトに載せる用語の行。"""
    lines = []
    for term, e in gl.items():
        desc, sql = e.get("description") or "", e.get("sql") or ""
        lines.append(f"- {term}: {desc}" if desc else f"- {term}:")
        if sql:
            lines.append(f"    SQL式: {sql}   ← この式をそのまま使う")
        else:
            lines.append("    （SQL式は未登録。上の列情報をもとに自分で組み立てる）")
    return lines


def glossary_count(meta: dict) -> int:
    """DB全体＋全テーブルの用語数。"""
    n = len(db_glossary(meta))
    for tname in (meta.get("tables") or {}):
        n += len(table_glossary(meta, tname))
    return n


# =============================================================================
# 自動プロファイル
# =============================================================================

def _qi(name: str) -> str:
    """SQLite識別子のクオート。"""
    return '"' + str(name).replace('"', '""') + '"'


def _cache_path(db_path) -> Path:
    return config.PROFILE_CACHE_DIR / (Path(db_path).name + ".profile.json")


def _make_timeout(conn: sqlite3.Connection, seconds: float):
    """接続にタイムアウトを仕掛け、クエリごとに呼ぶ reset 関数を返す。"""
    box = {"t": time.time()}
    conn.set_progress_handler(lambda: 1 if (time.time() - box["t"]) > seconds else 0, 100000)

    def reset():
        box["t"] = time.time()
    return reset


def _profile_table(conn: sqlite3.Connection, name: str, reset) -> dict:
    t = _qi(name)
    info: dict = {"columns": [], "fks": [], "row_count": None,
                  "sample_columns": [], "sample_rows": [], "col_stats": {}}

    reset()
    # PRAGMA table_info の pk は 0=非キー / 1以上=複合主キー内の順番。
    # 複合キーの構成順は「1行が何を表すか」の手がかりになるので pk_seq に残す。
    for cid, cname, ctype, notnull, dflt, pk in conn.execute(f"PRAGMA table_info({t})"):
        info["columns"].append({"name": cname, "type": ctype or "", "notnull": bool(notnull),
                                "pk": bool(pk), "pk_seq": int(pk or 0)})

    reset()
    try:
        for row in conn.execute(f"PRAGMA foreign_key_list({t})"):
            # (id, seq, table, from, to, on_update, on_delete, match)
            info["fks"].append({"from": row[3], "table": row[2], "to": row[4] or "id"})
    except sqlite3.Error:
        pass

    reset()
    try:
        info["row_count"] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    except sqlite3.Error:
        pass  # タイムアウト等 → 行数不明として続行

    reset()
    try:
        cur = conn.execute(f"SELECT * FROM {t} LIMIT {config.PROFILE_SAMPLE_ROWS}")
        info["sample_columns"] = [d[0] for d in cur.description] if cur.description else []
        info["sample_rows"] = [[_jsonable(v) for v in r] for r in cur.fetchall()]
    except sqlite3.Error:
        pass

    # 列統計（巨大テーブルはスキップ）
    rc = info["row_count"]
    if rc is not None and rc <= config.PROFILE_STATS_MAX_ROWS and rc > 0:
        limit = config.PROFILE_LOW_CARDINALITY
        for col in info["columns"]:
            c = _qi(col["name"])
            stat: dict = {}
            reset()
            try:
                vals = conn.execute(
                    f"SELECT {c} AS v, COUNT(*) AS n FROM {t} GROUP BY 1 ORDER BY n DESC LIMIT {limit + 1}"
                ).fetchall()
                if len(vals) <= limit:
                    stat["values"] = [[_jsonable(v), n] for v, n in vals]
                else:
                    reset()
                    mn, mx = conn.execute(f"SELECT MIN({c}), MAX({c}) FROM {t}").fetchone()
                    stat["min"], stat["max"] = _jsonable(mn), _jsonable(mx)
            except sqlite3.Error:
                pass
            if stat:
                info["col_stats"][col["name"]] = stat
    return info


def _jsonable(v):
    if isinstance(v, bytes):
        return f"<BLOB {len(v)} bytes>"
    return v


def profile_db(db_path, force: bool = False) -> dict:
    """DBを読み取り専用でプロファイリング。mtime+sizeが一致するキャッシュがあれば再利用。"""
    db_path = Path(db_path)
    st = db_path.stat()
    # v はプロファイルの構造バージョン。上げると古いキャッシュが無効になる。
    key = {"v": 2, "mtime": st.st_mtime, "size": st.st_size}

    cache = _cache_path(db_path)
    if not force and cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if data.get("key") == key:
                return data
        except Exception:
            pass

    conn = db.connect_ro(db_path)
    try:
        reset = _make_timeout(conn, config.PROFILE_TIMEOUT_SEC)
        tables: dict = {}
        reset()
        rows = conn.execute(
            "SELECT name, type FROM sqlite_master "
            "WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        for name, typ in rows:
            try:
                t = _profile_table(conn, name, reset)
                t["type"] = typ
                tables[name] = t
            except sqlite3.Error as e:
                tables[name] = {"type": typ, "error": str(e), "columns": [], "fks": [],
                                "row_count": None, "sample_columns": [], "sample_rows": [],
                                "col_stats": {}}
    finally:
        conn.close()

    profile = {
        "file": db_path.name,
        "key": key,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "tables": tables,
    }
    config.PROFILE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(profile, ensure_ascii=False, default=str), encoding="utf-8")
    return profile


# =============================================================================
# 乖離検知・結合候補・カバレッジ
# =============================================================================

def drift_warnings(profile: dict, meta: dict) -> list[str]:
    """メタ情報がスキーマの実体からズレている箇所を警告として返す。"""
    warns = []
    ptables = profile.get("tables", {})
    for tname, tmeta in (meta.get("tables") or {}).items():
        if tname not in ptables:
            warns.append(f"メタ情報のテーブル '{tname}' はDBに存在しません（改名/削除された可能性）。")
            continue
        pcols = {c["name"] for c in ptables[tname]["columns"]}
        for cname in ((tmeta or {}).get("columns") or {}):
            if cname not in pcols:
                warns.append(f"メタ情報の列 '{tname}.{cname}' はDBに存在しません。")
        for cname in ((tmeta or {}).get("primary_key") or []):
            if cname not in pcols:
                warns.append(f"指定された主キーの列 '{tname}.{cname}' はDBに存在しません。")
    for rel in (meta.get("relationships") or []):
        for end in (rel.get("from", ""), rel.get("to", "")):
            parts = str(end).split(".")
            if len(parts) == 2:  # table.col（同一DB内）のみ検証。db付き3要素は他DBなので対象外
                tname, cname = parts
                if tname in ptables:
                    if cname not in {c["name"] for c in ptables[tname]["columns"]}:
                        warns.append(f"結合定義の '{end}' に対応する列がありません。")
                else:
                    warns.append(f"結合定義の '{end}' に対応するテーブルがありません。")
    return warns


def join_suggestions(profile: dict, meta: dict) -> list[dict]:
    """列名ヒューリスティックによる結合候補（FK宣言済み・登録済みは除く）。"""
    ptables = profile.get("tables", {})
    existing = set()
    for rel in (meta.get("relationships") or []):
        existing.add((str(rel.get("from", "")).lower(), str(rel.get("to", "")).lower()))
    for tname, t in ptables.items():
        for fk in t.get("fks", []):
            existing.add((f"{tname}.{fk['from']}".lower(), f"{fk['table']}.{fk['to']}".lower()))

    sugs = []
    for tname, t in ptables.items():
        for col in t.get("columns", []):
            cname = col["name"]
            low = cname.lower()
            if not low.endswith("_id") and not low.endswith("id"):
                continue
            base = low[:-3] if low.endswith("_id") else None
            if not base:
                continue
            # 候補テーブル名: base / base+"s" / base+"es"
            for cand in (base, base + "s", base + "es"):
                target = next((n for n in ptables if n.lower() == cand), None)
                if not target or target == tname:
                    continue
                tcols = ptables[target]["columns"]
                pk = next((c["name"] for c in tcols if c["pk"]), None)
                # 複合主キーの相手に1列だけで結合する候補は誤りになるので出さない
                if len([c for c in tcols if c["pk"]]) > 1:
                    continue
                to_col = pk or next((c["name"] for c in tcols if c["name"].lower() in ("id", low)), None)
                if not to_col:
                    continue
                frm, to = f"{tname}.{cname}", f"{target}.{to_col}"
                if (frm.lower(), to.lower()) in existing:
                    continue
                sugs.append({"from": frm, "to": to, "cardinality": "N:1",
                             "reason": f"列名 '{cname}' → テーブル '{target}' の推測"})
                break
    return sugs


def coverage(profile: dict, meta: dict) -> dict:
    """メタ情報の充実度。カタログページの案内表示に使う。"""
    ptables = profile.get("tables", {})
    mtables = meta.get("tables") or {}
    n_tables = len(ptables)
    n_tdesc = sum(1 for t in ptables if (mtables.get(t) or {}).get("description"))
    n_cols = sum(len(t["columns"]) for t in ptables.values())
    n_cdesc = 0
    for tname, t in ptables.items():
        mcols = (mtables.get(tname) or {}).get("columns") or {}
        for c in t["columns"]:
            cm = mcols.get(c["name"]) or {}
            if cm.get("description") or cm.get("values"):
                n_cdesc += 1
    return {
        "tables": (n_tdesc, n_tables),
        "columns": (n_cdesc, n_cols),
        "relationships": len(meta.get("relationships") or []),
        "glossary": glossary_count(meta),
        "examples": len(meta.get("examples") or []),
    }


# =============================================================================
# 結合の端点表記（"table.col" / "alias.table.col" の相互変換）
# =============================================================================

def parse_endpoint(end: str, default_alias: str):
    """'table.col' または 'alias.table.col' を (alias, table, column) に解く。"""
    parts = [p.strip() for p in str(end).split(".")]
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return default_alias, parts[0], parts[1]
    return None


def node_id(alias: str, table: str) -> str:
    """テーブル（親ノード）のID。"""
    return f"{alias}.{table}"


# 列ノード（親テーブルの中に並ぶ子ノード）のID。
# テーブルIDが "alias.table" なので、列との区切りには "::" を使う。
COL_SEP = "::"


def col_node_id(alias: str, table: str, column: str) -> str:
    return f"{alias}.{table}{COL_SEP}{column}"


def edge_label(cardinality: str | None) -> str:
    """IPA表記の関連ラベル。線は列ノード同士を結ぶので、列名はラベルに出さず
    多重度だけを示す（始点側 ─ 終点側）。例: "* ─ 1"
    """
    tail, head = _CARD_ENDS.get(cardinality or "N:1", ("*", "1"))
    return f"{tail} ─ {head}"


def collect_edges(entries: list[dict]) -> list[dict]:
    """キャンバス/ER図に描く結合を集める。

    entries: [{"alias": str, "profile": dict, "meta": dict}, ...]
    戻り値の各要素:
      {"id", "source", "target", "label", "kind": "fk"|"meta", "owner", "index"}
      kind="fk"   … DBに宣言されたFOREIGN KEY（削除不可）
      kind="meta" … .meta.yaml の relationships（編集・削除可。index は配列位置）
    """
    nodes = {node_id(e["alias"], t) for e in entries for t in e["profile"].get("tables", {})}

    # メタ側の端点集合（FKと重複したら FK 側を出さない）
    meta_pairs = set()
    for e in entries:
        for rel in (e["meta"].get("relationships") or []):
            a = parse_endpoint(rel.get("from", ""), e["alias"])
            b = parse_endpoint(rel.get("to", ""), e["alias"])
            if a and b:
                meta_pairs.add((a, b))

    def valid(p):
        """端点(alias, table, column)が実在し、キャンバス上にあるか。"""
        if node_id(p[0], p[1]) not in nodes:
            return False
        e = next((x for x in entries if x["alias"] == p[0]), None)
        cols = {c["name"] for c in (e["profile"]["tables"].get(p[1]) or {}).get("columns", [])}
        return p[2] in cols

    edges: list[dict] = []
    for e in entries:
        alias = e["alias"]
        for tname, t in e["profile"].get("tables", {}).items():
            for fk in t.get("fks", []):
                a = (alias, tname, fk["from"])
                b = (alias, fk["table"], fk["to"])
                if not valid(a) or not valid(b) or (a, b) in meta_pairs:
                    continue
                edges.append({
                    "id": f"fk||{a[0]}.{a[1]}.{a[2]}||{b[0]}.{b[1]}.{b[2]}",
                    "source": col_node_id(*a), "target": col_node_id(*b),
                    "from": a, "to": b,
                    "label": edge_label("N:1"), "cardinality": "N:1",
                    "kind": "fk", "owner": alias, "index": None,
                })
        for i, rel in enumerate(e["meta"].get("relationships") or []):
            a = parse_endpoint(rel.get("from", ""), alias)
            b = parse_endpoint(rel.get("to", ""), alias)
            if not a or not b or not valid(a) or not valid(b):
                continue
            card = rel.get("cardinality") or "N:1"
            edges.append({
                "id": f"rel||{alias}||{i}",
                "source": col_node_id(*a), "target": col_node_id(*b),
                "from": a, "to": b,
                "label": edge_label(card), "cardinality": card,
                "kind": "meta", "owner": alias, "index": i,
            })
    return edges


def declared_pk(profile: dict, tname: str) -> list[str]:
    """DBが宣言している主キー（複合キーは構成順）。宣言が無ければ空リスト。"""
    t = profile.get("tables", {}).get(tname) or {}
    cols = [c for c in t.get("columns", []) if c.get("pk")]
    cols.sort(key=lambda c: c.get("pk_seq") or 0)
    return [c["name"] for c in cols]


def effective_pk(profile: dict, meta: dict, tname: str):
    """実際に主キーとして扱う列と、その出所を返す。

    戻り値: (列名リスト, "override" | "declared" | "none")
    メタの tables.<name>.primary_key があれば、DB宣言より優先する。
    主キーが宣言されていないテーブル（CSV取込など）に人が指定できるようにするため。
    """
    valid = [c["name"] for c in (profile.get("tables", {}).get(tname) or {}).get("columns", [])]
    ov = ((meta.get("tables") or {}).get(tname) or {}).get("primary_key")
    if ov:
        cols = [c for c in ov if c in valid]
        if cols:
            return cols, "override"
    d = declared_pk(profile, tname)
    return (d, "declared") if d else ([], "none")


def fk_columns(entries: list[dict], alias: str, tname: str) -> set:
    """外部キーとして扱う列（FK宣言 + メタの relationships の from 側）。"""
    out = set()
    for e in entries:
        if e["alias"] == alias:
            t = e["profile"].get("tables", {}).get(tname) or {}
            for fk in t.get("fks", []):
                out.add(fk["from"])
        for rel in (e["meta"].get("relationships") or []):
            p = parse_endpoint(rel.get("from", ""), e["alias"])
            if p and p[0] == alias and p[1] == tname:
                out.add(p[2])
    return out


# --- IPA表記のノードラベル -------------------------------------------------------
#: 1DBあたりに持つ例文の上限。例文は毎回 system prompt に載るので、
#: 増えるほどテーブル定義の説明が押し出される。多くても効果は上がらない。
EXAMPLES_MAX = 20


def _norm_sql(sql: str) -> str:
    """比べるためだけの正規化。空白の入れ方と大小の違いを無視する。"""
    return " ".join(str(sql or "").split()).lower()


def dedupe_examples(examples: list[dict]) -> list[dict]:
    """例文から重複を落とす。

    同じSQLが複数あると、毎回のプロンプトが太るうえ、AIがその型を
    過剰に当てはめるようになる。SQLが同じものは最初の1件だけ残す。
    質問文が同じものも、後から入れた方（確認し直した方）を残す。
    説明は任意なので、書かれているものだけを残す。
    """
    by_sql: dict = {}
    for ex in examples or []:
        q = str(ex.get("q") or "").strip()
        sql = str(ex.get("sql") or "").strip()
        if not q or not sql:
            continue
        key = _norm_sql(sql)
        if key in by_sql:
            continue                      # 同じSQLは1件でよい
        desc = str(ex.get("description") or "").strip()
        by_sql[key] = {"q": q, **({"description": desc} if desc else {}), "sql": sql}

    # 質問文の重複は後勝ち（同じ問いに対する新しいSQLを正とする）
    by_q: dict = {}
    for ex in by_sql.values():
        by_q[ex["q"]] = ex
    return list(by_q.values())[:EXAMPLES_MAX]


def find_example(examples: list[dict], sql: str) -> dict | None:
    """同じSQLの例文が既にあれば返す。"""
    key = _norm_sql(sql)
    for ex in examples or []:
        if _norm_sql(ex.get("sql")) == key:
            return ex
    return None


def load_layout(meta: dict) -> dict:
    """メタからノード座標を読む。{'alias.table': (x, y)}"""
    raw = meta.get("er_layout") or {}
    out = {}
    for k, v in raw.items():
        try:
            out[str(k)] = (float(v[0]), float(v[1]))
        except (TypeError, ValueError, IndexError):
            continue
    return out


# =============================================================================
# ER図（DOT生成 → st.graphviz_chart で描画。静的な俯瞰ビュー用）
# =============================================================================

# IPA表記の多重度ラベル: 線の両端に "1" と "*" を置く
_CARD_ENDS = {
    "N:1": ("*", "1"),   # from(多側) ─ to(1側)
    "1:N": ("1", "*"),
    "1:1": ("1", "1"),
    "N:M": ("*", "*"),
}

#: 向きを入れ替えたときの多重度。1:1 と N:M は入れ替えても同じ。
_CARD_FLIP = {"N:1": "1:N", "1:N": "N:1", "1:1": "1:1", "N:M": "N:M"}


def _is_sole_pk(profile: dict, meta: dict, table: str, column: str) -> bool:
    """その列が、そのテーブルの主キー全体か（単独主キーか）。"""
    pk, _ = effective_pk(profile, meta, table)
    return len(pk) == 1 and pk[0] == column


def normalize_direction(a: tuple, b: tuple, cardinality: str, lookup) -> tuple:
    """関連の向きを「子（外部キー側）→ 親（主キー側）」に揃える。

    ER図はIPA表記なので矢印を描かない。見た目に向きが無いぶん、人は
    好きな方向にドラッグする。ところが from/to は単なる描画順ではなく、
    「どちらが参照している側か」を表しており、参照整合性の検査
    （親に居ない子を数える）はこの向きに依存する。逆向きに登録されると
    「入金の無い請求」を異常として数えるような、意味の反転が起きる。

    lookup(alias) は (profile, meta) を返す関数。判断できないときは触らない。

    戻り値: (from, to, cardinality)
    """
    card = cardinality or "N:1"
    try:
        pa, ma = lookup(a[0])
        pb, mb = lookup(b[0])
    except Exception:
        return a, b, card
    if not (pa and pb):
        return a, b, card
    a_is_pk = _is_sole_pk(pa, ma, a[1], a[2])
    b_is_pk = _is_sole_pk(pb, mb, b[1], b[2])
    # 片方だけが主キーなら、そちらを親（to）にする
    if a_is_pk and not b_is_pk:
        return b, a, _CARD_FLIP.get(card, card)
    return a, b, card


def child_parent(entries: list[dict], edge: dict) -> tuple:
    """この関連の (子, 親)。参照整合性の検査はこの向きでしか意味を持たない。

    保存済みの from/to を鵜呑みにせず、主キーがどちら側にあるかで決め直す。
    手で書いた .meta.yaml が逆向きでも、検査は正しい向きで走る。
    """
    frm, to = tuple(edge["from"]), tuple(edge["to"])
    by_alias = {e["alias"]: e for e in entries}

    def sole_pk(ep):
        e = by_alias.get(ep[0])
        if not e:
            return None
        return _is_sole_pk(e["profile"], e.get("meta") or {}, ep[1], ep[2])

    f_pk, t_pk = sole_pk(frm), sole_pk(to)
    if f_pk and not t_pk:
        return to, frm            # from が親だった。入れ替える
    return frm, to


# =============================================================================
# LLM用テキスト生成（プロンプト＝カタログの直列化）
# =============================================================================

def _fmt_value_list(values, col_meta_values: dict) -> str:
    """実値一覧を '1=受付(120), 2=出荷済(300)' 形式で。メタのコード値辞書で意味を補完。"""
    parts = []
    for v, n in values:
        key = "" if v is None else str(v)
        label = (col_meta_values or {}).get(key)
        disp = "NULL" if v is None else str(v)
        if label:
            disp += f"={label}"
        parts.append(f"{disp}({n})")
    return ", ".join(parts)


def table_text(alias: str, tname: str, profile: dict, meta: dict, full: bool) -> str:
    """1テーブル分の説明テキスト。full=False なら1行要約のみ。"""
    t = profile["tables"].get(tname)
    if t is None:
        return f"- {alias}.{tname} : (プロファイル未取得)"
    tmeta = ((meta.get("tables") or {}).get(tname)) or {}
    desc = (tmeta.get("description") or "").strip()
    draft = "（AI推測・未確認）" if tmeta.get("ai_draft") else ""
    rc = t.get("row_count")
    rc_s = f"{rc:,}行" if rc is not None else "行数不明"
    head = f"{alias}.{tname}（{rc_s}）"
    if not full:
        line = f"- {head}" + (f" : {desc}{draft}" if desc else "")
        terms = list(table_glossary(meta, tname))
        # 用語があることだけ知らせる。定義は describe_table で取りに行かせる
        return line + (f" / 業務用語: {', '.join(terms)}" if terms else "")

    lines = [f"### {head}"]
    if desc:
        lines.append(f"{desc}{draft}")

    # 主キーは「1行が何を表すか（粒度）」の手がかりなので、複合キーは構成順で明示する
    pk_cols, pk_src = effective_pk(profile, meta, tname)
    note = {"override": "（人が指定）", "declared": "", "none": ""}[pk_src]
    if len(pk_cols) > 1:
        lines.append(f"主キー{note}: ({', '.join(pk_cols)}) の複合キー → この組み合わせで1行が一意。"
                     f"結合するときは{len(pk_cols)}列すべてを条件にする。")
    elif len(pk_cols) == 1:
        lines.append(f"主キー{note}: {pk_cols[0]}")
    else:
        lines.append("主キー: なし（DBに宣言が無く、指定もされていない）。"
                     "重複行があり得るので COUNT(DISTINCT ...) の要否に注意する。")

    mcols = tmeta.get("columns") or {}
    lines.append("列:")
    for c in t["columns"]:
        cm = (mcols.get(c["name"])) or {}
        parts = [f"- {c['name']} {c['type']}".rstrip()]
        if c["pk"]:
            parts.append("PK")
        if cm.get("description"):
            parts.append(f": {cm['description']}")
        stat = t.get("col_stats", {}).get(c["name"]) or {}
        if "values" in stat:
            parts.append(f"/ 値: {_fmt_value_list(stat['values'], cm.get('values'))}")
        elif cm.get("values"):
            vv = ", ".join(f"{k}={v}" for k, v in cm["values"].items())
            parts.append(f"/ コード値: {vv}")
        elif "min" in stat:
            parts.append(f"/ 範囲: {stat['min']} 〜 {stat['max']}")
        lines.append(" ".join(parts))
    if t.get("sample_rows"):
        lines.append(f"サンプル行 {t['sample_columns']}:")
        for r in t["sample_rows"][:3]:
            lines.append(f"  {r}")

    tgl = table_glossary(meta, tname)
    if tgl:
        lines.append(f"{tname} の業務用語（質問にこの言葉が出たら必ずこの定義に従う）:")
        lines.extend(glossary_lines(tgl))
    return "\n".join(lines)


def db_text(alias: str, db_path, tables: list[str] | None, full: bool) -> str:
    """1DB分の説明テキスト（プロファイル＋メタの合成）。"""
    profile = profile_db(db_path)
    meta = load_meta(db_path)
    names = tables or list(profile["tables"].keys())

    lines = []
    title = meta.get("title") or ""
    lines.append(f"## DB: {alias}" + (f"（{title}）" if title else "") + f" — ファイル: {Path(db_path).name}")
    if meta.get("description"):
        lines.append(str(meta["description"]).strip())
    for c in (meta.get("caveats") or []):
        lines.append(f"⚠ {c}")
    lines.append("")
    if full:
        for tname in names:
            lines.append(table_text(alias, tname, profile, meta, full=True))
            lines.append("")
    else:
        lines.append("テーブル一覧:")
        for tname in names:
            lines.append(table_text(alias, tname, profile, meta, full=False))
        lines.append("")

    rels = [r for r in (meta.get("relationships") or [])]
    fk_lines = []
    for tname in names:
        for fk in profile["tables"].get(tname, {}).get("fks", []):
            fk_lines.append(f"- {alias}.{tname}.{fk['from']} = {alias}.{fk['table']}.{fk['to']} (FK宣言)")
    if rels or fk_lines:
        lines.append("結合キー（JOINにはこれを使う）:")
        lines.extend(fk_lines)
        for r in rels:
            card = f" ({r['cardinality']})" if r.get("cardinality") else ""
            lines.append(f"- {r.get('from')} = {r.get('to')}{card}")
        lines.append("")

    gl = db_glossary(meta)
    if gl:
        lines.append("テーブルをまたぐ業務用語（質問にこの言葉が出たら必ずこの定義に従う）:")
        lines.extend(glossary_lines(gl))
        lines.append("")

    exs = meta.get("examples") or []
    if exs:
        lines.append("正しいと確認済みの質問とSQLの例:")
        for ex in exs:
            lines.append(f"Q: {ex.get('q')}")
            # 説明は「この例をどう読むか」の注意書き。人が書いたときだけ載せる
            if ex.get("description"):
                lines.append(f"補足: {ex['description']}")
            lines.append(f"SQL: {ex.get('sql')}")
        lines.append("")
    return "\n".join(lines)


#: 組み立て済みのカタログ本文。DBが多いと1回あたり数十msかかり、
#: 質問のたび・対象を選び直すたびに作り直すのは無駄なので覚えておく。
_TEXT_CACHE: dict = {}


def _text_key(alias: str, path, tables, full: bool):
    """中身が変わったら別物になるキー。DB・メタ・プロファイルの更新時刻を見る。"""
    p = Path(path)

    def stamp(f: Path) -> int:
        try:
            return f.stat().st_mtime_ns
        except OSError:
            return 0

    return (alias, str(p), tuple(tables or ()), full,
            stamp(p), stamp(meta_path(p)), stamp(_cache_path(p)))


def forget(db_path) -> None:
    """そのDBについて覚えているものを捨てる。DBを消したときに呼ぶ。

    本文のキャッシュは更新時刻で自動的に切り替わるが、プロファイルの
    キャッシュはファイルとして残る。DBが無くなったあとも残っていると、
    同じ名前で作り直したときに古い中身が出てくる。
    """
    p = Path(db_path)
    try:
        _cache_path(p).unlink(missing_ok=True)
    except OSError as e:
        print(f"[catalog] キャッシュを消せませんでした: {e}")
    for key in [k for k in _TEXT_CACHE if k[1] == str(p)]:
        _TEXT_CACHE.pop(key, None)


def db_text_cached(alias: str, path, tables=None, full: bool = True) -> str:
    """db_text の結果を使い回す版。カタログを直せば自動で作り直される。"""
    try:
        key = _text_key(alias, path, tables, full)
    except Exception:
        return db_text(alias, path, tables, full=full)
    hit = _TEXT_CACHE.get(key)
    if hit is None:
        hit = db_text(alias, path, tables, full=full)
        if len(_TEXT_CACHE) > 64:            # 古い世代が溜まりすぎないように
            _TEXT_CACHE.clear()
        _TEXT_CACHE[key] = hit
    return hit


def inline_length(scope: list[dict]) -> int:
    """詳細版カタログの文字数（列名までAIに渡せるかの判断に使う）。"""
    return sum(len(db_text_cached(s["alias"], s["path"], s.get("tables"), full=True))
               for s in (scope or []))


def inline_limit() -> int:
    """カタログを全文のまま入れる上限。「モデル設定」画面の値が最優先。

    models は画面側の都合（設定ファイル）を持つので、下位のここからは
    呼ぶときだけ参照する。読めなければ env の初期値に落とす。
    """
    try:
        import models
        return models.prompt_inline_limit()
    except Exception:
        return config.PROMPT_INLINE_LIMIT_CHARS


def prompt_for_scope(scope: list[dict], limit: int | None = None) -> str:
    """選択スコープ全体のカタログテキスト。

    全文が上限（limit。省略時は管理者設定/env）以下なら詳細をインライン、
    超えるなら要約のみ（詳細は describe_table ツールで取得させる）。
    limit は「選択中のモデルが一度に読める量」から呼び出し側が渡せる
    （models.inline_limit_for 参照。固定値だと小さいモデルで溢れるため）。
    """
    if not scope:
        return "（現在、対象のDBが選択されていません。サイドバーでDBとテーブルを選ぶよう案内してください。）"
    full = "\n".join(db_text_cached(s["alias"], s["path"], s.get("tables"), full=True)
                     for s in scope)
    if len(full) <= (limit if limit is not None else inline_limit()):
        return full
    compact = "\n".join(db_text_cached(s["alias"], s["path"], s.get("tables"), full=False)
                        for s in scope)
    # ここに載っているのはテーブル単位の説明までで、列名は入っていない。
    # それを言わずに渡すと「その列は無い」と早合点して、できることまで断ってしまう。
    return (compact + "\n"
            "【重要】選択中のDBが多いため、上には各テーブルの説明までしか載せていません。"
            "**列名は1つも載っていません。**\n"
            "そのため、上に見当たらないという理由で「その列は無い」「そのテーブルは無い」と"
            "判断してはいけません。必要な列があるかどうかは、必ず describe_table を呼んで"
            "確かめること。名前から中身が推測できるテーブル（商品なら products、"
            "社員なら employees など）は、まず describe_table で列を見てから答えること。\n"
            "ユーザーに「その情報は無い」と答えてよいのは、関係しそうなテーブルを"
            "describe_table で実際に確認した後だけです。")


def describe_table_text(scope: list[dict], db_alias: str, tname: str) -> str:
    """describe_table ツールの実体。alias と テーブル名から詳細テキストを返す。"""
    # "alias.table" 形式で渡された場合に対応
    if "." in tname and not db_alias:
        db_alias, tname = tname.split(".", 1)
    entry = next((s for s in scope if s["alias"].lower() == str(db_alias).lower()), None)
    if entry is None:
        aliases = ", ".join(s["alias"] for s in scope)
        return f"エラー: DBエイリアス '{db_alias}' は選択されていません。選択中: {aliases}"
    profile = profile_db(entry["path"])
    meta = load_meta(entry["path"])
    if tname not in profile["tables"]:
        cand = ", ".join(profile["tables"].keys())
        return f"エラー: テーブル '{tname}' は {db_alias} にありません。存在するテーブル: {cand}"
    return table_text(entry["alias"], tname, profile, meta, full=True)
