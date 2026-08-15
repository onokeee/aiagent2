"""画面まわりの共通処理: ログイン状態・スコープ・描画用の変換。"""
from __future__ import annotations

import functools
import json
import re
from pathlib import Path

from flask import g, jsonify, redirect, render_template, request, session, url_for

import auth
import catalog
import config
import db

_USER_KEY = "user"


# --- ログイン -----------------------------------------------------------------

def load_user_into_context():
    """毎リクエストの冒頭でログイン中のユーザーを復元する。"""
    data = session.get(_USER_KEY)
    g.user = auth.User(**data) if data else None


def login_user(user: auth.User) -> None:
    session[_USER_KEY] = {"username": user.username, "display_name": user.display_name,
                          "groups": list(user.groups), "is_admin": user.is_admin}
    session.permanent = False


def logout_user() -> None:
    session.clear()


def login_required(view):
    @functools.wraps(view)
    def wrapped(*a, **kw):
        if g.get("user") is None:
            if request.path.startswith("/api/"):
                return jsonify({"error": "ログインしてください。"}), 401
            return redirect(url_for("auth.login", next=request.path))
        return view(*a, **kw)
    return wrapped


def admin_required(view):
    """管理者だけが通れる。ログインしていなければログイン画面へ。

    データカタログ・データ取り込み・メール設定は、間違えると全員に影響が出る
    （AIの回答の土台、DBの中身、送信先）ので、閲覧も含めて管理者に限る。
    画面側でメニューを隠すだけでは、URLを直に叩かれると素通りしてしまう。
    """
    @functools.wraps(view)
    def wrapped(*a, **kw):
        user = g.get("user")
        if user is None:
            if request.path.startswith("/api/"):
                return jsonify({"error": "ログインしてください。"}), 401
            return redirect(url_for("auth.login", next=request.path))
        if not user.is_admin:
            if request.path.startswith("/api/"):
                return jsonify({"error": "この操作は管理者のみです。"}), 403
            return render_template("403.html"), 403
        return view(*a, **kw)
    return wrapped


def inject_globals() -> dict:
    """全テンプレートで使う値。"""
    return {"user": g.get("user"), "app_title": config.APP_TITLE,
            "nav": request.endpoint or ""}


# --- 分析スコープ（どのDBのどのテーブルを見るか） --------------------------------

def db_files() -> list[Path]:
    return db.list_db_files()


def build_scope(selection: dict) -> list[dict]:
    """{DBファイル名: [テーブル名, ...]} から scope を組み立てる。

    scope は tools/llm がそのまま受け取る形式。
    """
    files = {f.name: f for f in db_files()}
    chosen = [files[n] for n in selection if n in files]
    aliases = db.aliases_for(chosen)
    scope = []
    for f, alias in zip(chosen, aliases):
        prof = catalog.profile_db(f)
        available = list(prof["tables"].keys())
        want = [t for t in (selection.get(f.name) or available) if t in available]
        scope.append({"path": str(f), "alias": alias, "name": f.name,
                      "tables": want or available, "meta": catalog.load_meta(f)})
    return scope


def dbs_in_sql(sql: str, scope: list[dict]) -> list[dict]:
    """SQLが名前を挙げているDBを、SQLに出てくる順で返す。

    例文の保存先を決めるのに使う。例文はDBごとのファイルに残すので、
    複数のDBを選んでいても「このSQLはどのDBのものか」を決める必要がある。
    DBをまたぐSQLでは、主となるFROM句のDB（最初に出てくるもの）が先頭に来る。
    """
    def first_hit(pattern: str) -> int | None:
        m = re.search(pattern, sql, re.IGNORECASE)
        return m.start() if m else None

    # まずは「エイリアス.テーブル」の形で探す
    found = []
    for s in scope:
        alias = s.get("alias") or ""
        pos = first_hit(r'(?<![\w."])' + re.escape(alias) + r'\s*\.') if alias else None
        if pos is not None:
            found.append((pos, s))
    if found:
        found.sort(key=lambda t: t[0])
        return [s for _, s in found]

    # 修飾されていないSQL（DBを1つしか選んでいないときにAIが書く形）。
    # テーブル名で当てにいく。列名と紛れることがあるので、あくまで最後の手段。
    hits = []
    for s in scope:
        for t in (s.get("tables") or []):
            pos = first_hit(r'(?<![\w."])' + re.escape(t) + r'(?![\w"])')
            if pos is not None:
                hits.append((pos, s))
                break
    hits.sort(key=lambda t: t[0])
    out = []
    for _, s in hits:
        if s not in out:
            out.append(s)
    return out


def tables_in_sql(sql: str, scope: list[dict], limit: int = 6) -> list[dict]:
    """SQLが触れているテーブルを {db, table} で返す。

    チャットからデータカタログの該当テーブルへ飛ぶリンクを作るのに使う。
    「この列が何なのか分からない」とAIが言ったときに、その場で説明を
    書きに行けるようにするためのもの。
    """
    flat = str(sql or "").replace('"', "")       # "orders" のような引用符を外して見る
    out, seen = [], set()
    for s in scope:
        alias = str(s.get("alias") or "")
        for t in (s.get("tables") or []):
            name = str(t)
            qualified = (alias and re.search(
                r'(?<![\w.])' + re.escape(alias) + r'\s*\.\s*' + re.escape(name) + r'(?![\w])',
                flat, re.IGNORECASE))
            bare = re.search(r'(?<![\w.])' + re.escape(name) + r'(?![\w])',
                             flat, re.IGNORECASE)
            if not (qualified or bare):
                continue
            key = (s.get("name"), name)
            if key not in seen:
                seen.add(key)
                out.append({"db": s.get("name"), "table": name})
    return out[:limit]

#: 最初の画面に出す例文の上限。多すぎると選べない。
_EXAMPLE_LIMIT = 6


def scope_starters(scope: list[dict]) -> dict:
    """まだ何も話していない画面に出す「取っ掛かり」。

    例文はカタログ（各DBの .meta.yaml の examples）から取る。固定の例文を
    持たないのは、DB構成が変われば必ず嘘になるため。チャットの
    「この質問とSQLを例文として保存」で貯まるので、使うほど増えていく。

    未登録のDBでは代わりに選択中のテーブル名を出す。空のままだと
    「何を聞けるのか」の手がかりが無くなるため。
    """
    examples, tables = [], []
    for s in scope:
        for ex in (s.get("meta", {}).get("examples") or []):
            q = str(ex.get("q") or "").strip()
            if q and q not in examples:
                examples.append(q)
        tables.extend(s.get("tables") or [])
    return {"examples": examples[:_EXAMPLE_LIMIT], "tables": tables[:12]}


# --- 描画用アイテムの変換 --------------------------------------------------------

def jsonable(value):
    """JSONに載らない値（bytes / datetime など）を落として文字列にする。"""
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def render_item_for_web(item: dict) -> dict:
    """tools.dispatch が返す描画アイテムを、ブラウザに渡せる形へ。

    - グラフは plotly の JSON にしてクライアントで描く
    - ファイルは中身をサーバ側に預け、ダウンロードURLだけ渡す
    """
    import charts

    kind = item.get("kind")
    out = {k: jsonable(v) for k, v in item.items() if k not in ("data", "sheets")}

    if kind in ("chart", "chart_dual"):
        try:
            fig = (charts.build_dual_figure(item) if kind == "chart_dual"
                   else charts.build_figure(item))
            out["figure"] = json.loads(fig.to_json())
            out["kind"] = "chart"
        except Exception as e:
            out["kind"] = "error"
            out["message"] = f"グラフを描けませんでした: {e}"
    elif kind == "report_doc":
        # 節ごとのグラフはここで figure に変換する（画面はそのまま描くだけ）
        out["sections"] = []
        for s in item.get("sections") or []:
            sec = {k: v for k, v in s.items() if k != "chart"}
            if s.get("chart"):
                try:
                    fig = charts.build_figure(s["chart"])
                    sec["figure"] = json.loads(fig.to_json())
                except Exception as e:
                    sec["chart_error"] = f"グラフを描けませんでした: {e}"
            out["sections"].append(jsonable(sec))
    elif kind == "file":
        sheets = item.get("sheets") or []
        out["sheets"] = [{"name": s.get("name"), "columns": jsonable(s.get("columns")),
                          "rows": jsonable((s.get("rows") or [])[:20]),
                          "total": len(s.get("rows") or [])} for s in sheets]
    return out
