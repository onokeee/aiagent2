"""テーブル・DBを消したときの後片付け。

消すこと自体は DROP TABLE とファイル移動で済む。面倒なのはその後で、
参照は方々に散らばっている。

  ・そのDBの .meta.yaml  … 説明・関連・用語・例文・検算ルール・ER図の配置
  ・他のDBの .meta.yaml  … DBをまたぐ関連、ER図に借りたテーブル
  ・定期取り込みの設定    … 残すと、消したテーブルが次の実行で復活する
  ・利用者ごとの選択      … 「対象データ」に消えたDBが残り続ける

放っておくと、AIには存在しないテーブルの説明が渡り続け、例文の検証は
「no such table」で落ちる。掃除をここに集めて、消し忘れが出ないようにする。

消す前の「何が巻き添えになるか」も同じ規則で数える（table_impact / db_impact）。
数えるだけの関数は何も書き換えない。
"""
from __future__ import annotations

import re
from pathlib import Path

import catalog
import config
import db
import jobs
import prefs
import verify


# =============================================================================
# SQLがそのテーブルを触っているか
# =============================================================================

def uses_table(sql: str, table: str, alias: str | None = None) -> bool:
    """SQL文字列がそのテーブル名を参照しているか。

    "orders" と "demo_sales.orders" の両方を見る。素の名前だけを探すと
    DB名で修飾された書き方（例文はたいていこちら）を取りこぼし、
    修飾ありだけを探すと単一DBの例文を取りこぼす。
    構文解析まではしない。掃除の判定なので、取りこぼすより拾いすぎるほうがまし。
    ただし何を消したかは呼び出し側で必ず報告すること。
    """
    if not table:
        return False
    text = str(sql or "")
    t = r'"?' + re.escape(table) + r'"?(?![\w])'
    if re.search(r'(?<![\w."])' + t, text, re.IGNORECASE):
        return True
    if alias and re.search(r'(?<![\w."])' + re.escape(alias) + r'\s*\.\s*' + t,
                           text, re.IGNORECASE):
        return True
    return False


def _rel_text(rel: dict) -> str:
    return f"{rel.get('from', '')} → {rel.get('to', '')}"


def _ep_hits(rel: dict, own_alias: str, alias: str, table: str | None) -> bool:
    """関連の端点が (alias, table) を指しているか。table=None ならDB丸ごと。"""
    for key in ("from", "to"):
        ep = catalog.parse_endpoint(rel.get(key, ""), own_alias)
        if ep and ep[0] == alias and (table is None or ep[1] == table):
            return True
    return False


# =============================================================================
# メタの掃除（1ファイルぶん）
# =============================================================================

def _scrub_meta(meta: dict, own_alias: str, alias: str, table: str | None) -> dict:
    """meta から (alias, table) への参照を落とす。落としたものを返す。

    meta はその場で書き換える。table=None なら、そのDBへの参照すべて。
    自分自身のDB（own_alias == alias）かどうかで消す範囲が変わる:
      自分   … テーブルの説明・用語・例文・検算も消す
      他所   … 関連とER図の置き場所だけ（例文は他DBのテーブルを引くこともある）
    """
    hit: dict = {"relationships": [], "glossary": [], "examples": [],
                 "checks": [], "tables": [], "er_external": [], "er_layout": []}
    mine = own_alias == alias

    rels = meta.get("relationships") or []
    keep = [r for r in rels if not _ep_hits(r, own_alias, alias, table)]
    if len(keep) != len(rels):
        hit["relationships"] = [_rel_text(r) for r in rels
                                if _ep_hits(r, own_alias, alias, table)]
        meta["relationships"] = keep
    if not meta.get("relationships"):
        meta.pop("relationships", None)

    # ER図に借りているテーブル（"alias.table" の並び）
    ext = [str(x) for x in (meta.get("er_external") or [])]
    gone = [x for x in ext
            if x.split(".")[0] == alias and (table is None or x.split(".")[-1] == table)]
    if gone:
        hit["er_external"] = gone
        left = [x for x in ext if x not in gone]
        if left:
            meta["er_external"] = left
        else:
            meta.pop("er_external", None)

    # ER図の配置（"alias.table": [x, y]）
    layout = meta.get("er_layout") or {}
    lgone = [k for k in layout
             if str(k).split(".")[0] == alias
             and (table is None or str(k).split(".")[-1] == table)]
    if lgone:
        hit["er_layout"] = lgone
        for k in lgone:
            layout.pop(k, None)
        if not layout:
            meta.pop("er_layout", None)

    if not mine:
        return hit

    # --- ここから先は自分のDBのときだけ -------------------------------------
    if table is None:
        return hit                      # DBごと消えるのでファイルごと処分される

    tables = meta.get("tables") or {}
    if table in tables:
        hit["tables"] = [table]
        tables.pop(table, None)
        if not tables:
            meta.pop("tables", None)

    # DB全体の用語。SQL式がそのテーブルを引いているものだけ消す。
    # 説明文だけの用語は、文中にテーブル名が出てきても残す（文章なので）
    gl = meta.get("glossary") or {}
    gone_terms = [t for t, v in gl.items()
                  if isinstance(v, dict) and uses_table(v.get("sql"), table, alias)]
    if gone_terms:
        hit["glossary"] = gone_terms
        for t in gone_terms:
            gl.pop(t, None)
        if not gl:
            meta.pop("glossary", None)

    exs = meta.get("examples") or []
    left = [e for e in exs if not uses_table(e.get("sql"), table, alias)]
    if len(left) != len(exs):
        hit["examples"] = [str(e.get("q") or e.get("sql") or "")[:60]
                           for e in exs if uses_table(e.get("sql"), table, alias)]
        meta["examples"] = left
        if not left:
            meta.pop("examples", None)

    cks = verify.normalize(meta.get("checks"))
    def ck_hits(c):
        return any(uses_table(s, table, alias) for s in
                   (c["left"]["sql"], c["right"]["sql"], c.get("drilldown") or ""))
    left = [c for c in cks if not ck_hits(c)]
    if len(left) != len(cks):
        hit["checks"] = [c["name"] for c in cks if ck_hits(c)]
        meta["checks"] = left
        if not left:
            meta.pop("checks", None)

    return hit


def _merge(into: dict, where: str, hit: dict) -> None:
    """掃除の結果を「どのDBで何を消したか」の形で積む。"""
    for key, items in hit.items():
        for it in items:
            into.setdefault(key, []).append({"db": where, "text": it})


# =============================================================================
# 下見（消す前に見せる。何も書き換えない）
# =============================================================================

def _walk(alias: str, table: str | None, apply: bool, skip: Path | None = None) -> dict:
    """全DBのメタを見て、(alias, table) への参照を数える／消す。

    apply=False なら保存しない（load_meta は毎回ファイルから読み直すので、
    その場の dict を書き換えても他に影響しない）。
    skip はそのDB自身のファイル（消すので触らない）。
    """
    found: dict = {}
    for f in db.list_db_files():
        if skip is not None and f == skip:
            continue
        own = db.alias_for(f)
        meta = catalog.load_meta(f)
        hit = _scrub_meta(meta, own, alias, table)
        if any(hit.values()):
            _merge(found, own, hit)
            if apply:
                catalog.save_meta(f, meta)
    return found


def _jobs_for(db_name: str, table: str | None) -> list[dict]:
    return [j for j in jobs.list_jobs()
            if j.get("db_file") == db_name and (table is None or j.get("table") == table)]


def _job_text(j: dict) -> str:
    """定期取り込みの1行分。何がどの間隔で入ってくる設定かが分かればよい。"""
    label = jobs.interval_label(j.get("interval_minutes"))
    name = j.get("name") or j.get("table") or "（無題）"
    stopped = "・停止中" if j.get("enabled") is False else ""
    return f"{name}（{j.get('table')} / {label}{stopped}）"


def table_impact(path: Path, table: str) -> dict:
    """テーブルを消したときに巻き添えになるもの。数えるだけ。"""
    alias = db.alias_for(path)
    out = _walk(alias, table, apply=False)
    out["jobs"] = [{"id": j.get("id"), "name": j.get("name") or j.get("table"),
                    "text": _job_text(j)}
                   for j in _jobs_for(path.name, table)]
    return out


def db_impact(path: Path) -> dict:
    """DBを消したときに巻き添えになるもの。数えるだけ。"""
    alias = db.alias_for(path)
    out = _walk(alias, None, apply=False, skip=path)
    out["jobs"] = [{"id": j.get("id"), "name": j.get("name") or j.get("table"),
                    "text": _job_text(j)}
                   for j in _jobs_for(path.name, None)]
    out["users"] = [{"db": path.name, "text": u} for u in _users_selecting(path.name)]
    try:
        profile = catalog.profile_db(path)
        out["own_tables"] = [{"db": alias,
                              "text": f"{t}（{(info.get('row_count') or 0):,}行）"}
                             for t, info in (profile.get("tables") or {}).items()]
    except Exception as e:
        out["own_tables"] = [{"db": alias, "text": f"（読めませんでした: {e}）"}]
    return out


def _users_selecting(db_name: str) -> list[str]:
    """「対象データ」にそのDBを入れている利用者。"""
    root = config.USER_META_DIR
    if not root.exists():
        return []
    out = []
    for d in sorted(root.iterdir()):
        if not (d / "prefs.yaml").is_file():
            continue
        if db_name in prefs.get_selection(d.name):
            out.append(d.name)
    return out


# =============================================================================
# 実際に消す
# =============================================================================

def clean_table(path: Path, table: str, drop_jobs: bool = True) -> dict:
    """テーブルを消したあとの掃除。DROP TABLE 自体は importer 側で済ませておく。"""
    alias = db.alias_for(path)
    done = _walk(alias, table, apply=True)
    done["jobs"] = []
    if drop_jobs:
        for j in _jobs_for(path.name, table):
            if jobs.delete_job(j.get("id")):
                done["jobs"].append({"db": path.name,
                                     "text": j.get("name") or j.get("table")})
    catalog.forget(path)
    return done


def delete_db(path: Path, drop_jobs: bool = True) -> dict:
    """DBを消す。ファイルごと削除するので元には戻せない。

    押す前に、巻き添えになるものの一覧とファイル名の入力で二重に確認している。
    """
    if path.parent.resolve() != config.DATA_DIR.resolve():
        raise ValueError("data/ の外は操作できません。")
    if not path.is_file():
        raise FileNotFoundError(f"DBが見つかりません: {path.name}")

    alias = db.alias_for(path)

    # 先にファイルを消す。順番が逆だと、削除に失敗したときに
    # 他のDBの関連だけが消えて、DBは残るという半端な状態になる。
    removed = []
    try:
        for p in (path, Path(f"{path}.meta.yaml")):
            if p.is_file():
                p.unlink()
                removed.append(p.name)
    except OSError as e:
        raise ValueError(f"{path.name} を削除できませんでした（{e}）。"
                         "このDBを使っている処理が終わってから、もう一度試してください。") from e

    done = _walk(alias, None, apply=True, skip=path)

    done["jobs"] = []
    if drop_jobs:
        for j in _jobs_for(path.name, None):
            if jobs.delete_job(j.get("id")):
                done["jobs"].append({"db": path.name,
                                     "text": j.get("name") or j.get("table")})

    # 利用者の「対象データ」から外す（残すと、次に開いたとき選択が壊れて見える）
    done["users"] = []
    for u in _users_selecting(path.name):
        sel = prefs.get_selection(u)
        sel.pop(path.name, None)
        prefs.set_selection(u, sel)
        done["users"].append({"db": path.name, "text": u})

    catalog.forget(path)
    done["removed"] = [{"db": path.name, "text": n} for n in removed]
    print(f"[cleanup] {path.name} を削除しました（{'、'.join(removed)}）")
    return done


#: 画面に出すときの見出し。キーの順にそのまま並べる。
LABELS = {
    "own_tables": "テーブルと中のデータ",
    "tables": "テーブルの説明",
    "relationships": "関連（ER図の線）",
    "glossary": "業務用語",
    "examples": "質問とSQLの例文",
    "checks": "検算ルール",
    "er_external": "ER図に借りているテーブル",
    "er_layout": "ER図の配置",
    "jobs": "定期取り込みの設定",
    "users": "利用者の「対象データ」の選択",
    "removed": "削除したファイル",
}


def summarize(impact: dict) -> list[dict]:
    """{キー: [{db, text}]} を画面用の並びにする。空の項目は落とす。"""
    return [{"key": k, "label": LABELS.get(k, k), "items": impact[k]}
            for k in LABELS if impact.get(k)]
