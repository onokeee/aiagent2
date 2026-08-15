"""データカタログ画面。テーブル/列の説明・用語集・結合(ER)・ツールを編集する。"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

from flask import Blueprint, g, jsonify, render_template, request

import advanced
import catalog
import catalog_history
import charts
import custom_tools
import db
import jobs
import llm
import sqlusage
import tools
import verify

from .helpers import admin_required, dbs_in_sql, jsonable, login_required

bp = Blueprint("catalog", __name__)


@bp.errorhandler(FileNotFoundError)
def _db_missing(e: FileNotFoundError):
    """db.path_for が見つけられなかったとき。存在しないDB名を投げられても500にしない。"""
    return jsonify({"error": str(e)}), 400


def _pick(name: str | None) -> Path | None:
    files = db.list_db_files()
    if not files:
        return None
    return next((f for f in files if f.name == name), files[0])


def _builtin_view(tool: dict) -> dict:
    """組み込みツールを画面で見られる形にする。

    AIに渡しているのは JSON Schema そのものなので、説明もパラメータも
    切らずに全部見せる。「AIがこのツールをどう理解しているか」が
    そのまま分かるようにするのが目的（説明の上書きを決める材料になる）。
    """
    fn = tool["function"]
    params = fn.get("parameters") or {}
    required = set(params.get("required") or [])
    return {
        "name": fn["name"],
        "description": fn.get("description") or "",
        # SQLを受け取るツールか（実行したSQLがチャットに表示される対象）
        "is_sql": fn["name"] in tools.SQL_TOOLS,
        "params": [{"name": k,
                    "type": (v or {}).get("type") or "",
                    "required": k in required,
                    "description": (v or {}).get("description") or "",
                    # 選択肢が決まっている引数は、そのまま候補を見せる
                    "enum": (v or {}).get("enum") or []}
                   for k, v in (params.get("properties") or {}).items()],
    }


def _tool_source(name: str) -> list[dict]:
    """組み込みツールが実際に何をしているかを、コードそのもので見せる。

    チャットで生成SQLを見せているのと同じ考え方で、「AIがこのツールを呼ぶと
    データに何が起きるか」を確かめられるようにする。
    統計系のツールは共通の入れ物でくるまれているので、中の呼び出しと
    その先の実装（advanced.py）まで辿って出す。
    """
    fn = tools._HANDLERS.get(name)
    if fn is None:
        return []

    out: list[dict] = []

    def add(target, label):
        try:
            src = inspect.getsource(target)
            where = f"{Path(inspect.getsourcefile(target)).name}:{inspect.getsourcelines(target)[1]}"
        except (OSError, TypeError):
            return
        out.append({"label": label, "where": where, "code": src})

    # _analysis_tool でくるまれたものは、中の呼び出し（どの分析を呼ぶか）を出す
    inner = None
    if fn.__name__ == "run" and fn.__closure__:
        inner = fn.__closure__[0].cell_contents
    add(inner or fn, "ツールの処理")

    # advanced.py に委譲しているなら、その本体も見せる（実際の計算はここ）
    if out:
        m = re.search(r"\badvanced\.(\w+)", out[0]["code"])
        if m:
            target = getattr(advanced, m.group(1), None)
            if callable(target):
                add(target, f"実際の計算 advanced.{m.group(1)}()")
    return out


@bp.get("/api/catalog/builtin/source")
@admin_required
def builtin_source():
    """組み込みツールのコード。開いたときだけ取りに行く（全部で65KBあるため）。"""
    name = request.args.get("name") or ""
    if name not in tools._HANDLERS:
        return jsonify({"error": f"未知のツールです: {name}"}), 404
    return jsonify({"name": name, "parts": _tool_source(name)})


def _overview(path: Path) -> dict:
    profile = catalog.profile_db(path)
    meta = catalog.load_meta(path)
    cov = catalog.coverage(profile, meta)
    return {"profile": profile, "meta": meta, "coverage": cov,
            "drift": catalog.drift_warnings(profile, meta)}


@bp.get("/catalog")
@admin_required
def index():
    target = _pick(request.args.get("db"))
    if target is None:
        return render_template("catalog.html", db_files=[], target=None)
    ov = _overview(target)
    profile, meta = ov["profile"], ov["meta"]

    tables = []
    for tname, t in profile["tables"].items():
        tmeta = (meta.get("tables") or {}).get(tname) or {}
        mcols = tmeta.get("columns") or {}
        pk_cols, pk_src = catalog.effective_pk(profile, meta, tname)
        cols = []
        for c in t["columns"]:
            cm = mcols.get(c["name"]) or {}
            stat = (t.get("col_stats") or {}).get(c["name"]) or {}
            if "values" in stat:
                actual = ", ".join(str(v) for v in stat["values"][:12])
            elif "min" in stat:
                actual = f"{stat['min']} 〜 {stat['max']}"
            else:
                actual = ""
            cols.append({"name": c["name"], "type": c["type"], "pk": c["name"] in set(pk_cols),
                         "description": cm.get("description", ""),
                         "codes": cm.get("values") or {}, "actual": actual})
        tables.append({
            "name": tname, "rows": t.get("row_count"),
            "description": tmeta.get("description", ""),
            "ai_draft": bool(tmeta.get("ai_draft")),
            "pk": pk_cols, "pk_src": pk_src,
            "columns": cols,
            "glossary": catalog.table_glossary(meta, tname),
            "sample_columns": t.get("sample_columns") or [],
            "sample_rows": (t.get("sample_rows") or [])[:5],
        })

    return render_template(
        "catalog.html",
        db_files=[f.name for f in db.list_db_files()],
        target=target.name, title=meta.get("title", ""),
        description=catalog.db_description(meta),
        coverage=ov["coverage"], drift=ov["drift"], tables=tables,
        db_glossary=catalog.db_glossary(meta),
        relationships=meta.get("relationships") or [],
        examples=meta.get("examples") or [],
        checks=verify.normalize(meta.get("checks")),
        suggestions=(catalog.join_suggestions(profile, meta)
                     + sqlusage.suggestions_for(db.alias_for(target), profile, meta)),
        er=_er_payload(target, profile, meta),
        # ツールはDBに紐づけずに作るので、一覧も全DB分を出す（組み込みと同じ扱い）
        custom=custom_tools.collect_everywhere(),
        builtin=[_builtin_view(t) for t in tools.BUILTIN_TOOLS],
        chart_fields={t: list(charts.required_fields(t)) for t in charts.CHART_TYPES},
        builtin_overrides=meta.get("builtin_tools") or {},
        cat_history=[{**r, "summary": catalog_history.summarize(r)}
                     for r in catalog_history.recent(50)],
        intervals=list(jobs.INTERVALS.keys()),
        llm_ready=llm.is_configured(),
    )


# =============================================================================
# ER図（キャンバス用のデータ）
# =============================================================================

def _er_payload(path: Path, profile: dict, meta: dict) -> dict:
    """ERキャンバス用ペイロード。実体は catalog.er_payload（チャットのツールと共用）。"""
    return catalog.er_payload(path, profile, meta)


def _alias_lookup(own_alias: str, own_profile: dict, own_meta: dict):
    """エイリアス → (profile, meta)。DBまたぎの関連を扱うのに要る。"""
    cache = {own_alias: (own_profile, own_meta)}

    def get(alias: str):
        if alias not in cache:
            p = next((f for f in db.list_db_files() if db.alias_for(f) == alias), None)
            cache[alias] = ((catalog.profile_db(p), catalog.load_meta(p)) if p
                            else (None, None))
        return cache[alias]
    return get


def _endpoint_error(ep: tuple, lookup) -> str | None:
    """関連の端点が実在するかを確かめる。DB名を含む3要素も受ける。"""
    alias, table, column = ep
    profile, _ = lookup(alias)
    if profile is None:
        return f"DB '{alias}' が見つかりません。"
    t = (profile.get("tables") or {}).get(table)
    if t is None:
        return f"{alias} にテーブル '{table}' がありません。"
    if column not in {c["name"] for c in t.get("columns", [])}:
        return f"{alias}.{table} に列 '{column}' がありません。"
    return None


def _ref(ep: tuple, own_alias: str) -> str:
    """保存する文字列。自DBなら 'table.col'、他DBなら 'alias.table.col'。"""
    return f"{ep[1]}.{ep[2]}" if ep[0] == own_alias else f"{ep[0]}.{ep[1]}.{ep[2]}"


@bp.post("/api/catalog/relationship")
@admin_required
def relationship():
    """関連の追加・多重度変更・削除。ER図キャンバスから呼ばれる。"""
    body = request.json or {}
    path = db.path_for(body["db"])
    meta = catalog.load_meta(path)
    rels = meta.setdefault("relationships", [])
    action = body.get("action")
    alias = db.alias_for(path)

    # 関連の指定は2通り。ドラッグ直後は from_table/from_column、
    # 「元に戻す／やり直す」は保存済みの文字列 from/to（'table.col' や 'db.table.col'）で来る
    def _ep(side):
        if body.get(side):
            return catalog.parse_endpoint(str(body[side]), alias)
        return catalog.parse_endpoint(f"{body.get(side + '_table')}.{body.get(side + '_column')}", alias)

    if action == "add":
        lookup = _alias_lookup(alias, catalog.profile_db(path), meta)
        # テーブル名は 'table' でも 'otherdb.table' でもよい（DBまたぎ）
        a, b = _ep("from"), _ep("to")
        if not a or not b:
            return jsonify({"error": "関連の指定が正しくありません。"}), 400
        for ep in (a, b):
            err = _endpoint_error(ep, lookup)
            if err:
                return jsonify({"error": err}), 400
        if a == b:
            return jsonify({"error": "同じ列同士は関連にできません。"}), 400
        if a[0] != alias and b[0] != alias:
            return jsonify({"error": "どちらか一方は、いま開いているDBのテーブルにしてください。"}), 400

        # 向きを「子（外部キー側）→ 親（主キー側）」に揃えてから保存する。
        # ER図は矢印を描かないので、人はどちら向きにもドラッグする。
        # from/to は描画順ではなく参照の向きで、整合性チェックがこれに依存する。
        a, b, card = catalog.normalize_direction(a, b, body.get("cardinality"), lookup)
        if a[0] != alias:
            # 入れ替えた結果、子が他DBになった。その関連は相手のDBが持つべき
            return jsonify({"error":
                            f"この向きの関連は {a[0]} 側で登録してください"
                            f"（外部キーを持つのは {a[0]}.{a[1]} です）。"
                            "DBを切り替えてから、同じようにつないでください。"}), 400

        new = {"from": _ref(a, alias), "to": _ref(b, alias), "cardinality": card}
        if any(r.get("from") == new["from"] and r.get("to") == new["to"] for r in rels):
            return jsonify({"error": "この関連はすでに登録されています。"}), 400

        # 結んでよい列か、実データを見て確かめる。
        #   block … 保存しない（値が全く重ならない等。JOINが成立しない線をAIに教えない）
        #   warn  … 理由を返して止める。人が確認して force=true で送り直せば保存する
        def _path_of(al):
            return next((f for f in db.list_db_files() if db.alias_for(f) == al), path)
        check = catalog.link_check(a, b, lookup, _path_of)
        if check["level"] == "block" or (check["level"] == "warn" and not body.get("force")):
            # 200 で返す: 画面の api() は非2xxだと本文を捨てて例外にするため
            return jsonify({"ok": False, "check": check,
                            "from": new["from"], "to": new["to"], "cardinality": card})
        rels.append(new)
        extra = {"added": new}
    elif action in ("update", "delete"):
        # 位置（index）でも、保存済みの from/to 文字列でも指せる。
        # 「元に戻す」は index がずれるので from/to で来る
        if body.get("from") and body.get("to"):
            i = next((k for k, r in enumerate(rels)
                      if r.get("from") == body["from"] and r.get("to") == body["to"]), -1)
        else:
            i = int(body.get("index", -1))
        if not (0 <= i < len(rels)):
            return jsonify({"error": "この関連は既に削除されています。"}), 400
        if action == "delete":
            extra = {"removed": rels.pop(i)}
        else:
            prev = rels[i].get("cardinality")
            rels[i]["cardinality"] = body.get("cardinality") or prev
            extra = {"updated": {**rels[i], "previous": prev}}
    else:
        return jsonify({"error": "不正な操作です。"}), 400

    catalog.save_meta(path, meta)
    profile = catalog.profile_db(path)
    return jsonify({"ok": True, "er": _er_payload(path, profile, catalog.load_meta(path)), **extra})


@bp.get("/api/catalog/table-info")
@login_required
def table_info():
    """ER図でテーブルをクリックしたときの中身（概要・列・実値・サンプル行）。

    描画用のペイロードには入れていない（全テーブル分を持つと重い）ので、
    開いたときに取りに来る。チャットの読み取り専用ER図からも使うので、
    管理者に限らずログイン済みなら見られる（describe_table でAIに渡している
    情報と同じ範囲）。
    """
    alias = request.args.get("db") or ""
    tname = request.args.get("table") or ""
    path = next((f for f in db.list_db_files() if db.alias_for(f) == alias), None)
    if path is None:
        return jsonify({"error": f"DB '{alias}' が見つかりません。"}), 404
    profile, meta = catalog.profile_db(path), catalog.load_meta(path)
    t = (profile.get("tables") or {}).get(tname)
    if t is None:
        return jsonify({"error": f"テーブル '{tname}' が見つかりません。"}), 404
    tmeta = (meta.get("tables") or {}).get(tname) or {}
    mcols = tmeta.get("columns") or {}
    pk = set(catalog.effective_pk(profile, meta, tname)[0])
    cols = []
    for c in t.get("columns") or []:
        cm = mcols.get(c["name"]) or {}
        stat = (t.get("col_stats") or {}).get(c["name"]) or {}
        if "values" in stat:
            actual = ", ".join(str(v[0]) if isinstance(v, (list, tuple)) else str(v)
                               for v in stat["values"][:8])
        elif "min" in stat:
            actual = f"{stat['min']} 〜 {stat['max']}"
        else:
            actual = ""
        cols.append({"name": c["name"], "type": c.get("type") or "",
                     "pk": c["name"] in pk,
                     "description": cm.get("description") or "",
                     "codes": cm.get("values") or {}, "actual": actual})
    return jsonify({
        "db": path.name, "alias": alias, "table": tname,
        "rows": t.get("row_count"),
        "description": tmeta.get("description") or "",
        "ai_draft": bool(tmeta.get("ai_draft")),
        "columns": cols,
        "glossary": catalog.table_glossary(meta, tname),
        "sample_columns": t.get("sample_columns") or [],
        "sample_rows": jsonable((t.get("sample_rows") or [])[:5]),
    })


@bp.get("/api/catalog/er-tables")
@admin_required
def er_tables():
    """「他DBのテーブルを追加」の一覧。いま開いているDB以外のテーブル。"""
    alias = db.alias_for(db.path_for(request.args.get("db") or ""))
    out = []
    for f in db.list_db_files():
        a = db.alias_for(f)
        if a == alias:
            continue
        prof = catalog.profile_db(f)
        meta = catalog.load_meta(f)
        out.append({
            "alias": a, "name": f.name, "title": meta.get("title") or "",
            "tables": [{"id": f"{a}.{t}", "table": t,
                        "rows": info.get("row_count")}
                       for t, info in (prof.get("tables") or {}).items()],
        })
    return jsonify({"dbs": out})


@bp.post("/api/catalog/er-external")
@admin_required
def er_external():
    """キャンバスに引き込む他DBのテーブルを足す・外す。

    関連が1本も無いDBともつなげるようにするための入口。
    関連から自動で引き込まれているものは、ここから外しても残る
    （線の行き先が消えてしまうため）。
    """
    body = request.json or {}
    path = db.path_for(body["db"])
    meta = catalog.load_meta(path)
    alias = db.alias_for(path)
    target = str(body.get("table") or "").strip()
    parts = target.split(".")
    if len(parts) != 2 or parts[0] == alias:
        return jsonify({"error": "他のDBのテーブルを「DB名.テーブル名」で指定してください。"}), 400

    current = [str(x) for x in (meta.get("er_external") or [])]
    if body.get("action") == "remove":
        current = [x for x in current if x != target]
    else:
        p = next((f for f in db.list_db_files() if db.alias_for(f) == parts[0]), None)
        if p is None or parts[1] not in (catalog.profile_db(p).get("tables") or {}):
            return jsonify({"error": f"{target} が見つかりません。"}), 400
        if target not in current:
            current.append(target)
    if current:
        meta["er_external"] = current
    else:
        meta.pop("er_external", None)
    catalog.save_meta(path, meta)
    profile = catalog.profile_db(path)
    return jsonify({"ok": True, "er": _er_payload(path, profile, catalog.load_meta(path))})


@bp.post("/api/catalog/layout")
@admin_required
def save_layout():
    body = request.json or {}
    path = db.path_for(body["db"])
    meta = catalog.load_meta(path)
    incoming = body.get("layout") or {}
    if not isinstance(incoming, dict):
        return jsonify({"error": "配置の形式が正しくありません。"}), 400
    clean = {}
    for k, v in incoming.items():
        # 1ノード = [x, y] の数値2つ。それ以外は受け付けない（保存すると以後ER図が読めなくなる）
        if not isinstance(v, (list, tuple)) or len(v) != 2:
            return jsonify({"error": f"配置の形式が正しくありません（{k}）。"}), 400
        try:
            clean[str(k)] = [int(round(float(v[0]))), int(round(float(v[1])))]
        except (TypeError, ValueError):
            return jsonify({"error": f"配置の座標が数値ではありません（{k}）。"}), 400
    meta["er_layout"] = {**(meta.get("er_layout") or {}), **clean}
    catalog.save_meta(path, meta)
    return jsonify({"ok": True})


@bp.post("/api/catalog/primary-key")
@admin_required
def primary_key():
    body = request.json or {}
    path = db.path_for(body["db"])
    profile, meta = catalog.profile_db(path), catalog.load_meta(path)
    tm = meta.setdefault("tables", {}).setdefault(body["table"], {})
    declared = catalog.declared_pk(profile, body["table"])
    cols = body.get("columns") or []
    if cols and cols != declared:
        tm["primary_key"] = cols
    else:
        tm.pop("primary_key", None)
    catalog.save_meta(path, meta)
    return jsonify({"ok": True, "er": _er_payload(path, profile, catalog.load_meta(path))})


# =============================================================================
# 保存系
# =============================================================================

@bp.post("/api/catalog/table")
@admin_required
def save_table():
    body = request.json or {}
    path = db.path_for(body["db"])
    meta = catalog.load_meta(path)
    tables = meta.setdefault("tables", {})
    tm = tables.setdefault(body["table"], {})
    tm["description"] = (body.get("description") or "").strip()
    cols = {}
    for name, c in (body.get("columns") or {}).items():
        entry = {}
        if (c.get("description") or "").strip():
            entry["description"] = c["description"].strip()
        if c.get("values"):
            entry["values"] = c["values"]
        if entry:
            cols[name] = entry
    if cols:
        tm["columns"] = cols
    else:
        tm.pop("columns", None)
    tm.pop("ai_draft", None)
    if not any(tm.get(k) for k in ("description", "columns", "primary_key", "glossary")):
        tables.pop(body["table"], None)
    catalog.save_meta(path, meta)
    return jsonify({"ok": True})


@bp.post("/api/catalog/glossary")
@admin_required
def save_glossary():
    body = request.json or {}
    path = db.path_for(body["db"])
    meta = catalog.load_meta(path)
    gl = {}
    for row in body.get("terms") or []:
        term = (row.get("term") or "").strip()
        desc = (row.get("description") or "").strip()
        sql = (row.get("sql") or "").strip()
        if term and (desc or sql):
            gl[term] = {"description": desc, "sql": sql}
    # 誰が何を変えたかを残す（チャットからの登録と同じ記録に揃える）
    before_gl = (catalog.table_glossary(meta, body["table"]) if body.get("table")
                 else catalog.db_glossary(meta))
    _log_glossary_diff(path.name, body.get("table") or None, before_gl, gl)
    if body.get("table"):
        catalog.set_table_glossary(meta, body["table"], gl)
    elif gl:
        meta["glossary"] = gl
    else:
        meta.pop("glossary", None)
    catalog.save_meta(path, meta)
    return jsonify({"ok": True})


def _sql_scope(sql: str, path: Path) -> list[dict]:
    """このSQLを実行するのに繋ぐべきDBを決める。

    例文も用語のSQL式も、チャットと同じように別DBのテーブルへ
    「demo_master.employees」の形で入ることがある（人事DBに社員の氏名は無く、
    マスタDB側にある、など）。編集中のDBだけを繋いで検証すると、
    実際には通るSQLが "no such table" で落ちてしまうので、
    式が名前を挙げているDBは一緒に繋ぐ。
    """
    alias = db.alias_for(path)
    scope = [{"path": str(path), "alias": alias}]
    for p in db.list_db_files():
        if p == path or len(scope) >= db.MAX_ATTACHED:
            continue
        a = db.alias_for(p)
        if a.lower() == alias.lower():
            continue
        if re.search(r'(?<![\w."])' + re.escape(a) + r'\s*\.', sql, re.IGNORECASE):
            scope.append({"path": str(p), "alias": a})
    return scope


def _entries_for(scope: list[dict], cache: dict) -> list[dict]:
    """結合定義を引くための材料（各DBのプロファイルとメタ）を揃える。

    「すべて検証」では同じDBを何度も見るので、1リクエストの間だけ控えておく。
    """
    entries = []
    for s in scope:
        if s["alias"] not in cache:
            p = Path(s["path"])
            cache[s["alias"]] = {"alias": s["alias"], "profile": catalog.profile_db(p),
                                 "meta": catalog.load_meta(p)}
        entries.append(cache[s["alias"]])
    return entries


def _referenced_tables(sql: str, entries: list[dict], own_alias: str) -> list[tuple]:
    """SQL式の中に出てくるテーブルを (エイリアス, テーブル名) で拾う。

    自DBの "attendances.overtime_min" という書き方と、DBをまたぐ
    "demo_master.employees.employee_id" という書き方の両方を見つける。
    長い名前から先に照合する（"emp" が "employees" に化けるのを防ぐ）。
    """
    found = []
    for e in entries:
        a = e["alias"]
        for t in sorted(e["profile"].get("tables") or {}, key=len, reverse=True):
            qualified = (r'(?<![\w."])' + re.escape(a) + r'\s*\.\s*'
                         + re.escape(t) + r'\s*\.')
            if re.search(qualified, sql, re.IGNORECASE):
                found.append((a, t))
            elif a == own_alias and re.search(
                    r'(?<![\w."])' + re.escape(t) + r'\s*\.', sql, re.IGNORECASE):
                found.append((a, t))
    return found


def _table_label(at: tuple, own_alias: str) -> str:
    """画面に出すテーブル名。別DBのものはどのDBか分かるようにする。"""
    return at[1] if at[0] == own_alias else f"{at[0]}.{at[1]}"


def _from_clause(tables: list[tuple], entries: list[dict]) -> tuple[str, bool]:
    """複数テーブルをつなぐ FROM句を組み立てる。

    tables: [(エイリアス, テーブル名), ...]
    カタログに結合定義があればそれで JOIN する。無ければ素直に並べる
    （直積になるが、SELECT専用・タイムアウトつきなので暴走はしない）。
    戻り値の2つ目は「全部つなげたか」。直積のときは件数の割合に意味が無いので、
    呼び出し側でその旨を添える。
    """
    def q(at):
        return f'{at[0]}."{at[1]}"'

    if len(tables) <= 1:
        return q(tables[0]), True

    edges = catalog.collect_edges(entries)
    joined, sql, all_linked = [tables[0]], q(tables[0]), True
    for t in tables[1:]:
        cond = None
        for e in edges:
            (fa, ft, fc), (ta, tt, tc) = e["from"], e["to"]
            pair = {(fa, ft), (ta, tt)}
            if t in pair and pair & set(joined) and (fa, ft) != (ta, tt):
                cond = f'{fa}."{ft}"."{fc}" = {ta}."{tt}"."{tc}"'
                break
        if cond:
            sql += f" JOIN {q(t)} ON {cond}"
        else:
            sql += f", {q(t)}"
            all_linked = False
        joined.append(t)
    return sql, all_linked


@bp.post("/api/catalog/glossary/verify")
@admin_required
def verify_glossary():
    """用語のSQL式を実データに当てて確かめる。

    テーブルを1つ選んでいればそのテーブルで、DB全体の用語なら式が触れている
    テーブルを式から読み取って組み立てる。結合定義があればJOINでつなぐ。
    """
    body = request.json or {}
    path = db.path_for(body["db"])
    alias = db.alias_for(path)
    picked = body.get("table")
    cache: dict = {}

    out = []
    for row in body.get("terms") or []:
        sql = (row.get("sql") or "").strip()
        term = row.get("term") or ""
        if not sql:
            out.append({"term": term, "verdict": "－", "detail": "SQL式が未入力"})
            continue

        # 式が別DBの名前を出していれば、そのDBも繋いだ上で確かめる
        scope = _sql_scope(sql, path)
        entries = _entries_for(scope, cache)
        # 置き場所のテーブルを土台にしつつ、式が名前を挙げているテーブルも足す。
        # 「MTBF = 稼働時間 ÷ アラーム件数」のように、1つの用語が
        # 隣のテーブルを見に行くことがあるため（置き場所だけでは列が足りない）。
        used = _referenced_tables(sql, entries, alias)
        if picked and (alias, picked) not in used:
            used.insert(0, (alias, picked))
        if not used:
            # どのテーブルにも触れていない式。定数などはそのまま評価できる
            try:
                _, rows, _ = db.run_select(f"SELECT {sql} AS v", scope, max_rows=1)
                out.append({"term": term, "verdict": "計算式",
                            "detail": f"計算結果: {rows[0][0]}"})
            except Exception as e:
                out.append({"term": term, "verdict": "エラー",
                            "detail": "テーブル名が見つかりません。"
                                      "「売上.金額」のようにテーブル名から書いてください。"
                                      f"（{str(e).splitlines()[0][:70]}）"})
            continue

        src, linked = _from_clause(used, entries)
        labels = [_table_label(t, alias) for t in used]
        note = f"／ 対象: {'、'.join(labels)}" if len(used) > 1 or not picked else ""
        if not linked:
            note += "（結合定義が無いため総当たりで数えています。"
            note += "「結合・ER図」で関連を登録すると正確になります）"
        try:
            _, rows, _ = db.run_select(
                f"SELECT COUNT(*) AS n, (SELECT COUNT(*) FROM {src}) AS total "
                f"FROM {src} WHERE {sql}", scope, max_rows=1)
            n, total = rows[0]
            pct = f"（{n / total * 100:.1f}%）" if total else ""
            out.append({"term": term, "verdict": "条件式",
                        "detail": f"該当 {n:,} 行 / 全 {total:,} 行{pct}{note}"})
            continue
        except Exception as first:
            err = str(first).splitlines()[0][:120]
        try:
            _, rows, _ = db.run_select(f"SELECT {sql} AS v FROM {src}", scope, max_rows=1)
            out.append({"term": term, "verdict": "計算式",
                        "detail": f"計算結果の例: {rows[0][0]}{note}"})
        except Exception:
            out.append({"term": term, "verdict": "エラー", "detail": err})
    return jsonify({"results": out})


@bp.post("/api/catalog/examples/verify")
@admin_required
def verify_examples():
    """例文のSQLが実際に通るか確かめる。

    例文は「正しいと確認済みの例」としてAIに渡すので、通らないSQLが混ざると
    そのまま間違いを教えることになる。保存前にここで気づけるようにする。
    """
    body = request.json or {}
    path = db.path_for(body["db"])
    out = []
    for row in body.get("examples") or []:
        sql = (row.get("sql") or "").strip()
        q = (row.get("q") or "").strip()
        if not sql:
            out.append({"q": q, "verdict": "－", "detail": "SQLが未入力"})
            continue
        # 例文はDBをまたぐことがある（人事の勤怠 × マスタの社員、など）。
        # チャットと同じように、式が名前を挙げているDBを全部繋いで確かめる。
        scope = _sql_scope(sql, path)
        others = [s["alias"] for s in scope[1:]]
        cross = (f"／ {'、'.join(others)} も参照しています"
                 "（チャットではこれらのDBも一緒に選ぶ必要があります）") if others else ""
        try:
            columns, rows, truncated = db.run_select(sql, scope, max_rows=5)
        except Exception as e:
            out.append({"q": q, "verdict": "エラー",
                        "detail": str(e).splitlines()[0][:160]})
            continue
        if not rows:
            out.append({"q": q, "verdict": "0行",
                        "detail": f"実行できましたが0行でした（列: {'、'.join(columns)}）。"
                                  f"抽出条件が厳しすぎないか確認してください。{cross}"})
        else:
            more = "以上" if truncated else ""
            out.append({"q": q, "verdict": "OK",
                        "detail": f"{len(rows)}{more}行 取得（列: {'、'.join(columns)}）{cross}"})
    return jsonify({"results": out})


def _home_db(sql: str, preferred: Path | None = None) -> str:
    """このツール定義を置くDBファイルを決める。

    ツールはDBを選ばずに作るが、定義の置き場（どの .meta.yaml か）は
    1つに決めないといけない。SQLが最初に名指ししているDB＝主に見ているDBに置く。
    そのDBを消せばツールも一緒に片づく（cleanup.py の巻き添え掃除に乗る）。
    """
    if preferred is not None:
        return Path(preferred).name
    allscope = [{"path": str(p), "alias": db.alias_for(p), "name": p.name,
                 "tables": list((catalog.profile_db(p).get("tables") or {}).keys())}
                for p in db.list_db_files()]
    hits = dbs_in_sql(sql, allscope)
    if hits:
        return hits[0]["name"]
    files = db.list_db_files()
    return files[0].name if files else ""


def _sample_params(tool: dict, given: dict | None = None) -> dict:
    """試し実行に使う値。画面で入れた値 → AIが添えた例 → 型ごとの既定値、の順に採る。

    例を使うのは、日本語だけで作ったツールを人が確かめられるようにするため。
    空の値で流すと 0行 になり、「SQLが通った」ことしか分からない。実在する値を
    入れて実際の行を見せれば、SQLを読まなくても正しさを判断できる。

    それでも0行になることはある（条件が厳しいだけかもしれない）ので、
    0行は失敗にせず、そのことを画面に出す。
    """
    out = {}
    for p in (tool.get("parameters") or []):
        name = str(p.get("name") or "").strip()
        if not name:
            continue
        for v in ((given or {}).get(name), p.get("example")):
            if v not in (None, ""):
                out[name] = v
                break
        else:
            t = p.get("type") or "string"
            out[name] = 0 if t in ("integer", "number", "boolean") else ""
    return out


@bp.post("/api/catalog/tool/try")
@admin_required
def try_tool():
    """ツールのSQLを実データで動かして、出てくる列と先頭の行を返す。

    SQLを読めない人にも「何が出るか」で正しさを判断してもらうための口。
    実行は run_select を通すので SELECT 以外は動かない。
    """
    body = request.json or {}
    path = db.path_for(body["db"])
    tool = body.get("tool") or {}
    errs = [e for e in custom_tools.validate(tool) if not e.startswith("'")]
    sql = str(tool.get("sql") or "").strip()
    if not sql:
        return jsonify({"ok": False, "error": "SQLがありません。"})
    try:
        params = custom_tools.coerce_params(tool, _sample_params(tool, body.get("values")))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)})

    scope = _sql_scope(sql, path)
    try:
        columns, rows, truncated = db.run_select(sql, scope, max_rows=8, params=params)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e).splitlines()[0][:200]})
    others = [s["alias"] for s in scope[1:]]
    return jsonify({
        "ok": True, "columns": columns,
        "rows": [[jsonable(v) for v in r] for r in rows],
        "truncated": truncated, "problems": errs,
        "cross": others,
        "note": ("実行できましたが0行でした。抽出条件やパラメータの値を見直してください。"
                 if not rows else ""),
    })


@bp.post("/api/catalog/tool/draft")
@admin_required
def draft_tool():
    """日本語の「やりたいこと」から、ツールの下書きをAIに起こさせる。

    起こしたらその場で実データに当てて確かめ、失敗したらエラーを添えて
    もう一度だけ書き直させる。通らないSQLをそのまま画面に出さないため。
    """
    body = request.json or {}
    # DBは指定させない。どのDBを使うかは、やりたいことを読んだAIが決める。
    # 特定のDBに限りたいときだけ db を渡せる。
    path = db.path_for(body["db"]) if body.get("db") else None
    purpose = str(body.get("purpose") or "").strip()
    if not purpose:
        return jsonify({"error": "何をするツールかを書いてください。"}), 400
    if not llm.is_configured():
        return jsonify({"error": "LLMが未設定です。env の OPENAI_* を設定してください。"}), 400

    wanted = [str(x).strip() for x in (body.get("params") or []) if str(x).strip()]
    render = body.get("render") or "table"
    # AIが付けた名前が不正・重複でも、保存で突き返されるのはユーザーには
    # 意味不明（名前を入力していないので）。ここで必ず有効な名前に直す。
    taken = [t.get("name") for t in custom_tools.collect_everywhere()]
    tried = []
    draft, last_err = None, None
    for attempt in range(2):          # 1回目でだめならエラーを見せて書き直させる
        try:
            draft = llm.draft_tool(path, purpose, wanted, render,
                                   previous=draft, error=last_err)
        except Exception as e:
            return jsonify({"error": f"下書きに失敗しました: {e}"}), 500
        draft["name"] = custom_tools.safe_name(draft.get("name") or purpose, taken)
        sql = draft.get("sql") or ""
        if not sql:
            last_err = "SQLが空でした。"
            tried.append(last_err)
            continue
        try:
            params = custom_tools.coerce_params(draft, _sample_params(draft))
            scope = (_sql_scope(sql, path) if path
                     else db.widen_scope(sql, []))
            columns, rows, _ = db.run_select(sql, scope, max_rows=8, params=params)
        except Exception as e:
            last_err = str(e).splitlines()[0][:200]
            tried.append(last_err)
            continue
        return jsonify({"ok": True, "tool": draft, "columns": columns,
                        "rows": [[jsonable(v) for v in r] for r in rows],
                        "home_db": _home_db(sql, path),
                        "attempts": attempt + 1, "tried": tried})

    # 2回とも通らなかった。下書きは返す（人が直せるように）
    return jsonify({"ok": False, "tool": draft, "error": last_err, "tried": tried})


@bp.post("/api/catalog/glossary/draft")
@admin_required
def draft_glossary():
    body = request.json or {}
    path = db.path_for(body["db"])
    terms = [{"term": r["term"], "description": r.get("description", "")}
             for r in (body.get("terms") or []) if r.get("term") and r.get("description")]
    try:
        drafted = llm.draft_glossary_sql(path, body.get("table"), terms)
    except Exception as e:
        return jsonify({"error": f"下書きに失敗しました: {e}"}), 500
    return jsonify({"ok": True, "drafted": drafted})


@bp.post("/api/catalog/draft-table")
@admin_required
def draft_table():
    body = request.json or {}
    path = db.path_for(body["db"])
    try:
        draft = llm.draft_table_meta(path, body["table"])
    except Exception as e:
        return jsonify({"error": f"AI下書きに失敗しました: {e}"}), 500
    return jsonify({"ok": True, "draft": draft})


@bp.post("/api/catalog/checks")
@admin_required
def save_checks():
    """検算ルールの保存。空になったらキーごと消す。"""
    body = request.json or {}
    path = db.path_for(body["db"])
    checks = verify.normalize(body.get("checks"))
    names = [c["name"] for c in checks]
    if len(names) != len(set(names)):
        dup = next(n for n in names if names.count(n) > 1)
        return jsonify({"error": f"「{dup}」という名前の検算ルールが複数あります。"
                                 "名前を変えて区別してください。"}), 400
    meta = catalog.load_meta(path)
    if checks:
        meta["checks"] = checks
    else:
        meta.pop("checks", None)
    catalog.save_meta(path, meta)
    verify.clear_cache()          # ルールが変わったので、古い検算結果は捨てる
    return jsonify({"ok": True, "checks": checks})


@bp.post("/api/catalog/checks/verify")
@admin_required
def verify_checks():
    """検算ルールをその場で実行して、左右の値と差を返す（保存前の内容でよい）。"""
    body = request.json or {}
    path = db.path_for(body["db"])
    out = []
    for raw in body.get("checks") or []:
        raw = raw or {}
        lsql = str((raw.get("left") or {}).get("sql") or "").strip()
        rsql = str((raw.get("right") or {}).get("sql") or "").strip()
        if not lsql or not rsql:
            out.append({"ok_run": False, "error": "左右の両方にSQLが必要です。"})
            continue
        check = verify.normalize([raw])
        if not check:
            out.append({"ok_run": False, "error": "ルールの形が正しくありません。"})
            continue
        # 検算のSQLは別DBを参照できる。名前を挙げているDBも繋いで実行する
        combined = " ".join([lsql, rsql, str(raw.get("drilldown") or "")])
        scope = _sql_scope(combined, path)
        res = verify.run_check(check[0], scope, use_cache=False)
        out.append({k: res[k] for k in
                    ("ok_run", "match", "left", "right", "diff", "pct", "error", "drill")})
    return jsonify({"results": out})


@bp.get("/api/catalog/usage")
@admin_required
def er_usage():
    """過去の分析で実際に使われた結合の回数（ER図に重ねる）。"""
    path = db.path_for(request.args.get("db") or "")
    return jsonify(sqlusage.usage_for(db.alias_for(path)))


def _log_glossary_diff(db_file: str, table, before: dict, after: dict) -> None:
    """用語集の一括保存を、用語ごとの差分にして履歴へ。"""
    user = getattr(g.user, "username", None)
    for term in after:
        if term not in before:
            catalog_history.add("glossary", "add", db_file, term, user=user,
                                table=table, after=after[term], source="catalog")
        elif before[term] != after[term]:
            catalog_history.add("glossary", "update", db_file, term, user=user,
                                table=table, before=before[term],
                                after=after[term], source="catalog")
    for term in before:
        if term not in after:
            catalog_history.add("glossary", "remove", db_file, term, user=user,
                                table=table, before=before[term], source="catalog")


@bp.post("/api/catalog/examples")
@admin_required
def save_examples():
    body = request.json or {}
    path = db.path_for(body["db"])
    meta = catalog.load_meta(path)
    incoming = [e for e in (body.get("examples") or [])
                if str(e.get("q", "")).strip() and str(e.get("sql", "")).strip()]
    new = catalog.dedupe_examples(incoming)

    # 差分をSQLをキーに取り、誰が何を変えたかを残す
    user = getattr(g.user, "username", None)
    old_by_sql = {e.get("sql"): e for e in (meta.get("examples") or [])}
    new_by_sql = {e.get("sql"): e for e in new}
    for s_, e_ in new_by_sql.items():
        if s_ not in old_by_sql:
            catalog_history.add("example", "add", path.name, e_.get("q", ""),
                                user=user, after=e_, source="catalog")
        elif old_by_sql[s_] != e_:
            catalog_history.add("example", "update", path.name, e_.get("q", ""),
                                user=user, before=old_by_sql[s_], after=e_,
                                source="catalog")
    for s_, e_ in old_by_sql.items():
        if s_ not in new_by_sql:
            catalog_history.add("example", "remove", path.name, e_.get("q", ""),
                                user=user, before=e_, source="catalog")

    meta["examples"] = new
    catalog.save_meta(path, meta)
    dropped = len(incoming) - len(meta["examples"])
    return jsonify({"ok": True, "dropped": dropped,
                    "examples": meta["examples"]})


@bp.post("/api/catalog/overview")
@admin_required
def save_overview():
    body = request.json or {}
    path = db.path_for(body["db"])
    meta = catalog.load_meta(path)
    meta["title"] = (body.get("title") or "").strip()
    # 説明は1欄（注意したい事実は ※ で始める行として同じ欄に書く）。旧 caveats は畳む
    meta["description"] = "\n".join(
        l.rstrip() for l in (body.get("description") or "").splitlines()).strip()
    meta.pop("caveats", None)
    catalog.save_meta(path, meta)
    return jsonify({"ok": True})


@bp.post("/api/catalog/tool")
@admin_required
def save_tool():
    """ユーザー定義ツールの追加・更新・削除。"""
    body = request.json or {}
    path = db.path_for(body["db"])
    meta = catalog.load_meta(path)
    items = list(meta.get("tools") or [])
    name = body.get("name")

    if body.get("action") == "delete":
        items = [t for t in items if t.get("name") != name]
    else:
        tool = body.get("tool") or {}
        # 既存の名前も見て検証する。見ていないと、新規作成で同名を付けたとき
        # 既存のツールを黙って上書きしてしまう（更新は original の名前だけ除く）。
        original = body.get("original") or ""
        others = {t.get("name") for t in items if t.get("name") != original}
        errors = custom_tools.validate(tool, others)
        if errors:
            return jsonify({"error": " / ".join(errors)}), 400
        items = [t for t in items if t.get("name") != (original or name)]
        items.append(tool)
    if items:
        meta["tools"] = items
    else:
        meta.pop("tools", None)
    catalog.save_meta(path, meta)
    return jsonify({"ok": True})


@bp.post("/api/catalog/builtin")
@admin_required
def save_builtin():
    body = request.json or {}
    path = db.path_for(body["db"])
    meta = catalog.load_meta(path)
    over = dict(meta.get("builtin_tools") or {})
    over[body["name"]] = {"enabled": bool(body.get("enabled", True)),
                          "description": (body.get("description") or "").strip()}
    if not over[body["name"]]["description"] and over[body["name"]]["enabled"]:
        over.pop(body["name"])
    meta["builtin_tools"] = over
    catalog.save_meta(path, meta)
    return jsonify({"ok": True})
