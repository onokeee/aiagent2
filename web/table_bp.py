"""テーブル全体を見る画面。

サンプル行（先頭数行）だけでは「本当にこのテーブルでよいか」が分からないので、
中身を1ページずつ辿れる読み取り専用のビューアを別タブで開けるようにする。

・読むのは db.connect_ro（読み取り専用接続）だけ。書き込みの経路は持たない。
・行数が多いテーブルでも落ちないよう、常にサーバ側で LIMIT/OFFSET を付けて返す。
・絞り込みは全列を文字として LIKE する素朴なもの。値はプレースホルダで渡す
  （列名は実在する列名と照合してからでないと SQL に入れない）。
"""
from __future__ import annotations

from flask import Blueprint, jsonify, render_template, request

import catalog
import db

from .helpers import jsonable, login_required

bp = Blueprint("tableview", __name__)

PAGE_SIZES = (50, 100, 200, 500)
MAX_LIMIT = 500


def _qi(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _resolve(db_name: str, table: str):
    """DBファイルとテーブル名を確かめる。

    db は 'sales.db'（ファイル名）でも 'sales'（エイリアス）でも受ける。
    ER図やチャットからはエイリアスで来るため。
    """
    files = db.list_db_files()
    path = next((f for f in files if f.name == db_name), None)
    if path is None:
        path = next((f for f in files if db.alias_for(f) == db_name), None)
    if path is None:
        return None, None, f"DB '{db_name}' が見つかりません。"
    names = list((catalog.profile_db(path).get("tables") or {}).keys())
    if table not in names:
        return path, None, f"テーブル '{table}' が {path.name} にありません。"
    return path, table, None


@bp.get("/table")
@login_required
def index():
    """別タブで開くビューア本体。中身は table.js が API から取ってくる。"""
    db_name = request.args.get("db") or ""
    table = request.args.get("table") or ""
    path, tname, err = _resolve(db_name, table)
    meta = catalog.load_meta(path) if path else {}
    tmeta = ((meta.get("tables") or {}).get(tname) or {}) if tname else {}
    return render_template(
        "table.html",
        nav="tableview",
        db_file=path.name if path else db_name,
        alias=db.alias_for(path) if path else db_name,
        db_title=meta.get("title") or "",
        table=tname or table,
        description=tmeta.get("description") or "",
        error=err or "",
        page_sizes=list(PAGE_SIZES),
    )


@bp.get("/api/table/rows")
@login_required
def rows():
    """1ページぶんの行。offset/limit・絞り込み・並べ替えはすべてサーバ側で行う。"""
    path, table, err = _resolve(request.args.get("db") or "", request.args.get("table") or "")
    if err:
        return jsonify({"error": err}), 404

    try:
        offset = max(0, int(request.args.get("offset") or 0))
        limit = int(request.args.get("limit") or 100)
    except ValueError:
        return jsonify({"error": "表示位置の指定が正しくありません。"}), 400
    limit = max(1, min(MAX_LIMIT, limit))
    q = (request.args.get("q") or "").strip()
    sort = request.args.get("sort") or ""
    desc = (request.args.get("dir") or "asc").lower() == "desc"

    conn = db.connect_ro(path)
    try:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({_qi(table)})")]
        if sort and sort not in cols:        # 実在しない列名は SQL に入れない
            sort = ""
        where, params = "", []
        if q:
            # 全列を文字として見て部分一致。数値列も CAST して同じ扱いにする
            where = " WHERE " + " OR ".join(
                f"CAST({_qi(c)} AS TEXT) LIKE ? ESCAPE '\\'" for c in cols)
            like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            params = [like] * len(cols)

        total = conn.execute(f"SELECT COUNT(*) FROM {_qi(table)}").fetchone()[0]
        matched = (conn.execute(f"SELECT COUNT(*) FROM {_qi(table)}{where}", params).fetchone()[0]
                   if q else total)
        order = f" ORDER BY {_qi(sort)} {'DESC' if desc else 'ASC'}" if sort else ""
        cur = conn.execute(
            f"SELECT * FROM {_qi(table)}{where}{order} LIMIT ? OFFSET ?", [*params, limit, offset])
        data = [list(r) for r in cur.fetchall()]
    except Exception as e:                   # 壊れたDB・読めないテーブルでも画面は保つ
        return jsonify({"error": f"読み取りに失敗しました: {e}"}), 400
    finally:
        conn.close()

    return jsonify({"ok": True, "columns": cols, "rows": jsonable(data),
                    "total": total, "matched": matched,
                    "offset": offset, "limit": limit,
                    "sort": sort, "dir": "desc" if desc else "asc"})
