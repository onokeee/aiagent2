"""チャット画面とエージェントループ。

Streamlit 版との違いは状態の置き場所だけで、流れは同じ。
  質問 → LLM → tool_calls があれば実行 → 結果を返して再度LLM → 最終回答

会話の実体は chats.py（ユーザーごとのファイル）に置く。
対象データとモデルの選択は prefs.py に置く（ログアウトしても残す）。
セッションに持つのは「いまどの会話を開いているか」だけ。
"""
from __future__ import annotations

import json
from pathlib import Path

from flask import (Blueprint, Response, g, jsonify, render_template, request,
                   session, stream_with_context)

import catalog
import chats
import config
import custom_tools
import db
import llm
import mailer
import models
import prefs
import tools
import verify

from . import filestore
from .helpers import (admin_required, build_scope, dbs_in_sql, login_required,
                      render_item_for_web, scope_starters, tables_in_sql)

bp = Blueprint("chat", __name__)

TOOL_LABELS = {
    "run_sql_query": "SQL実行 (SELECT)",
    "plot_chart": "グラフ描画",
    "plot_dual_axis": "2軸グラフ描画 (棒+折れ線)",
    "plot_comparison": "グラフ描画（比較）",
    "plot_trend": "グラフ描画（推移）",
    "plot_composition": "グラフ描画（構成）",
    "plot_distribution": "グラフ描画（分布）",
    "plot_relationship": "グラフ描画（関係）",
    "plot_kpi": "グラフ描画（指標）",
    "pivot_table": "クロス集計",
    "analyze_stats": "統計分析",
    "export_excel": "Excel作成",
    "export_csv": "CSV作成",
    "export_text": "テキスト作成",
    "export_pptx": "PowerPoint作成",
    "describe_table": "テーブル詳細の確認",
    "hypothesis_test": "仮説検定",
    "regression": "回帰分析",
    "distribution_analysis": "分布の分析",
    "forecast": "予測",
    "timeseries_analysis": "時系列分析",
    "monte_carlo_simulation": "モンテカルロ・シミュレーション",
    "scenario_analysis": "シナリオ分析",
    "bootstrap_estimate": "信頼区間の推定",
    "clustering": "クラスタ分析",
    "abc_analysis": "ABC分析",
    "find_mail_recipients": "宛先の検索",
    "compose_email": "メールの下書き",
    "analyze_usage": "利用状況の分析",
}


# =============================================================================
# 画面
# =============================================================================

@bp.get("/")
@login_required
def index():
    files = []
    for f in db.list_db_files():
        meta = catalog.load_meta(f)
        prof = catalog.profile_db(f)
        tmeta = meta.get("tables") or {}
        # サイドバーで名前にマウスを乗せたときに出す説明。カタログに書いた内容が
        # そのままAIの理解になるので、選ぶ側にも同じ説明が見えている方がよい。
        tables = [{"name": t,
                   "description": (tmeta.get(t) or {}).get("description") or "",
                   "rows": info.get("row_count"),
                   "columns": len(info.get("columns") or [])}
                  for t, info in prof["tables"].items()]
        files.append({"name": f.name, "title": meta.get("title") or "",
                      "description": meta.get("description") or "",
                      "caveats": meta.get("caveats") or [],
                      "tables": tables})
    return render_template(
        "chat.html",
        db_files=files,
        chat_id=session.get("chat_id"),
        history=chats.list_chats(g.user),
        starters=scope_starters(build_scope({f.name: [] for f in db.list_db_files()})),
        llm_ready=llm.is_configured(),
        placeholder=config.APP_INPUT_PLACEHOLDER,
        auto_download=config.AUTO_DOWNLOAD,
    )


# =============================================================================
# モデルの選択と画像
# =============================================================================

@bp.get("/api/models")
@login_required
def list_models():
    return jsonify(models.status(g.user,
                                 refresh=request.args.get("refresh") == "1"))


@bp.post("/api/models")
@login_required
def choose_model():
    try:
        models.choose(g.user, (request.json or {}).get("model", ""))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, **models.status(g.user)})


@bp.post("/api/chat/image")
@login_required
def upload_image():
    """画像を1枚受け取り、送信待ちとして預かる。

    ここではLLMに送らない。実際に送るのは、その画像を付けて質問したとき。
    """
    f = request.files.get("file")
    if f is None:
        return jsonify({"error": "画像が選ばれていません。"}), 400
    if not models.is_vision(models.current(g.user)):
        return jsonify({"error": "いま選ばれているモデルは画像を扱えません。"
                                 "画像に対応したモデルに切り替えてください。"}), 400
    mime = (f.mimetype or "").lower()
    if mime not in llm.IMAGE_MIMES:
        return jsonify({"error": f"この形式は送れません（{mime or '不明'}）。"
                                 "PNG / JPEG / GIF / WebP を使ってください。"}), 400
    data = f.read()
    limit = int(config.IMAGE_MAX_MB * 1024 * 1024)
    if len(data) > limit:
        return jsonify({"error": f"画像が大きすぎます（{len(data) / 1024 / 1024:.1f}MB）。"
                                 f"{config.IMAGE_MAX_MB:.0f}MB以下にしてください。"}), 400
    if not data:
        return jsonify({"error": "中身が空の画像です。"}), 400

    token = filestore.put(data, f.filename or "image.png", mime, g.user.username)
    return jsonify({"ok": True, "token": token, "filename": f.filename or "image.png",
                    "mime": mime, "size": len(data),
                    "url": f"/api/file/{token}"})


def _images_from(tokens: list) -> tuple[list, list]:
    """預かった画像を、LLMに渡せる形（base64）にする。

    戻り値は (LLM用, 画面表示用)。
    """
    import base64
    send, show = [], []
    for t in (tokens or [])[: config.IMAGE_MAX_COUNT]:
        item = filestore.get(str(t), g.user.username)
        if item is None:
            continue
        send.append({"mime": item["mime"],
                     "b64": base64.b64encode(item["data"]).decode("ascii")})
        show.append({"filename": item["filename"], "mime": item["mime"],
                     "size": len(item["data"]), "url": f"/api/file/{t}"})
    return send, show


# =============================================================================
# 会話の読み書き
# =============================================================================

def _load_current() -> dict:
    """いま開いている会話。無ければ新規の空会話。"""
    cid = session.get("chat_id")
    if cid:
        chat = chats.load_chat(g.user, cid)
        if chat:
            return chat
    return {"id": None, "title": "", "created_at": "", "messages": [], "render_log": []}


def _persist(chat: dict) -> dict:
    # 新しい会話は、何か話すまでファイルを作らない。
    # 既にある会話は空になっても保存する（巻き戻しで全部消したときに、
    # 保存済みの古いやり取りが復活してしまうため）。
    if not chat["render_log"] and not chat.get("id"):
        return chat
    if not chat.get("id"):
        chat["id"] = chats.new_id()
    # db_names は「この会話で実際にSQLが触ったDB」。開いて続きを聞いたときに
    # 同じDBをスコープへ残すために使う（_auto_scope 参照）。
    used = set(chat.get("db_names") or [])
    for i in chat["render_log"]:
        if i.get("kind") == "sql" and i.get("sql"):
            used |= set(db.dbs_named_in(str(i["sql"])))
    chat["db_names"] = sorted(used)
    saved = chats.save_chat(
        g.user, chat["id"], chat["messages"], chat["render_log"],
        db_names=chat["db_names"], tables={},
        title=chat.get("title") or "", created_at=chat.get("created_at") or "")
    session["chat_id"] = chat["id"]
    chat["title"], chat["created_at"] = saved["title"], saved["created_at"]
    return chat


def _count_turns(render_log: list[dict]) -> int:
    """ユーザーの発言が何回あったか。"""
    return sum(1 for i in render_log
               if i.get("role") == "user" and i.get("kind") == "text")


def _split_at_turn(chat: dict, turn: int) -> tuple[list, list, str]:
    """指定の発言の直前までを切り出す。

    画面(render_log)とLLMの会話(messages)は別物なので、
    「何回目のユーザー発言か」を共通の目盛りにして両方を同じ位置で切る。
    戻り値は (切り詰めた messages, 切り詰めた render_log, もとの発言内容)。
    """
    seen, cut_log, original = 0, None, ""
    for i, item in enumerate(chat.get("render_log") or []):
        if item.get("role") == "user" and item.get("kind") == "text":
            if seen == turn:
                cut_log, original = i, item.get("content", "")
                break
            seen += 1
    if cut_log is None:
        raise ValueError(f"{turn + 1}番目の発言が見つかりません。")

    seen, cut_msg = 0, None
    for i, m in enumerate(chat.get("messages") or []):
        if m.get("role") == "user":
            if seen == turn:
                cut_msg = i
                break
            seen += 1
    if cut_msg is None:
        raise ValueError(f"{turn + 1}番目の発言が会話履歴にありません。")
    return chat["messages"][:cut_msg], chat["render_log"][:cut_log], original


def _web_log(render_log: list[dict], start: int = 0) -> list[dict]:
    """保存形式 → ブラウザ表示用。ファイルはダウンロードURLに差し替える。

    ユーザーの発言には通し番号(turn)を振る。巻き戻しのとき、
    画面のどの吹き出しが messages の何番目に当たるかを、これで対応付ける。
    """
    out = []
    turn = _count_turns(render_log[:start])
    for item in render_log[start:]:
        w = render_item_for_web(item)
        if item.get("role") == "user" and item.get("kind") == "text":
            w["turn"] = turn
            turn += 1
        # 中身(bytes)を持つアイテムは、種類を問わずダウンロードURLに置き換える
        if item.get("data"):
            token = filestore.put(item["data"], item.get("filename", "download"),
                                  item.get("mime", "application/octet-stream"),
                                  g.user.username)
            w["url"] = f"/api/file/{token}"
        if item.get("kind") == "sql":
            w["label"] = TOOL_LABELS.get(item.get("tool"), item.get("tool"))
        out.append(w)
    return out


@bp.get("/api/history")
@login_required
def history():
    return jsonify({"chats": [{**c, "label": chats.label(c)} for c in chats.list_chats(g.user)],
                    "current": session.get("chat_id")})


@bp.post("/api/chat/open")
@login_required
def open_chat():
    cid = request.json.get("id")
    if not cid:
        session.pop("chat_id", None)
        return jsonify({"ok": True, "items": []})
    chat = chats.load_chat(g.user, cid)
    if chat is None:
        return jsonify({"error": "この会話は見つかりませんでした。"}), 404
    session["chat_id"] = cid
    return jsonify({"ok": True, "items": _web_log(chat.get("render_log") or []),
                    "title": chat.get("title", "")})


@bp.post("/api/chat/delete")
@login_required
def delete_chat():
    cid = request.json.get("id")
    chats.delete_chat(g.user, cid)
    if session.get("chat_id") == cid:
        session.pop("chat_id", None)
    return jsonify({"ok": True})


@bp.post("/api/chat/rename")
@login_required
def rename_chat():
    chats.rename_chat(g.user, request.json.get("id"), request.json.get("title") or "")
    return jsonify({"ok": True})


# =============================================================================
# エージェントループ
# =============================================================================

def _msg_to_dict(m) -> dict:
    d = {"role": "assistant", "content": m.content}
    if m.tool_calls:
        d["tool_calls"] = [{"id": tc.id, "type": "function",
                            "function": {"name": tc.function.name,
                                         "arguments": tc.function.arguments}}
                           for tc in m.tool_calls]
    return d


def _extract_calls(m) -> list[dict]:
    if not m.tool_calls:
        return []
    return [{"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments}
            for tc in m.tool_calls]


def _call_previews(calls: list[dict], scope: list[dict], question: str) -> list[dict]:
    """実行『前』に見せる内容（生成SQLなど）。"""
    out = []
    for c in calls:
        try:
            args = json.loads(c["arguments"]) if c["arguments"] else {}
        except json.JSONDecodeError:
            args = {}
        custom = next((t for t in custom_tools.collect_everywhere(scope)
                       if t.get("name") == c["name"]), None)
        # 触れているテーブルを添える。画面ではカタログの該当テーブルへのリンクになり、
        # 「列の意味が分からない」と言われた場所から、そのまま説明を書きに行ける。
        if c["name"] in tools.SQL_TOOLS and "sql" in args:
            out.append({"role": "assistant", "kind": "sql", "tool": c["name"],
                        "sql": args["sql"], "purpose": args.get("purpose", ""),
                        "question": question,
                        "tables": tables_in_sql(args["sql"], scope)})
        elif custom is not None:
            binds = ", ".join(f"{k}={v!r}" for k, v in args.items()) or "（引数なし）"
            sql = tools.render_sql(custom)
            out.append({"role": "assistant", "kind": "sql", "tool": c["name"],
                        "sql": sql,
                        "purpose": f"{custom.get('description', '')[:60]} / 引数: {binds}",
                        "question": question,
                        "tables": tables_in_sql(sql, scope)})
        elif c["name"] == "describe_table":
            alias, table = args.get("db"), args.get("table")
            owner = next((s for s in scope if s.get("alias") == alias), None)
            out.append({"role": "assistant", "kind": "text",
                        "content": f"🛠 テーブル詳細を確認: `{alias}.{table}`",
                        "tables": ([{"db": owner["name"], "table": table}]
                                   if owner and table else [])})
    return out


class _Guard:
    """同じ失敗を繰り返させないための見張り。

    LLMは、直せない指摘を受けると同じ引数のまま呼び直すことがある。
    そのまま通すと上限まで同じエラーが並び、ユーザーには何も残らない。
    2回目以降は実行せずに「同じ呼び出しです」と返し、
    それでも繰り返すならその質問を打ち切る。
    """

    LIMIT = 2                     # 同じ呼び出しが何回来たら打ち切るか

    def __init__(self):
        self.failed: dict[tuple, str] = {}    # 失敗した呼び出し -> 理由
        self.repeats = 0

    @staticmethod
    def key(call: dict) -> tuple:
        return (call["name"], (call.get("arguments") or "").strip())

    def known_failure(self, call: dict) -> str | None:
        return self.failed.get(self.key(call))

    def note(self, call: dict, res: dict) -> None:
        if not res.get("ok"):
            try:
                why = json.loads(res["llm_content"]).get("error", "")
            except (ValueError, TypeError):
                why = ""
            self.failed[self.key(call)] = why or "同じ内容で失敗しました。"

    def repeated(self, call: dict) -> str:
        """2回目以降の同じ呼び出しに返す、LLM向けの差し戻し文。"""
        self.repeats += 1
        why = self.failed.get(self.key(call), "")
        return json.dumps({
            "error": "同じツールを同じ引数で呼び直しています。実行しませんでした。",
            "previous_error": why,
            "hint": "引数を直してから呼ぶこと。直せないなら、そのツールは諦めて"
                    "別の方法（表だけで示す・SQLを見直す・ユーザーに確認する）に切り替える。"
                    "同じ呼び出しをもう一度行ってはいけない。",
        }, ensure_ascii=False)

    @property
    def stuck(self) -> bool:
        return self.repeats >= self.LIMIT


def _stop_note(reason: str) -> dict:
    return {"role": "assistant", "kind": "text", "content": reason}


def _is_admin() -> bool:
    """管理者専用ツールを渡してよい相手か。

    「データ取り込み」画面が管理者専用なので、AI経由でも同じ線を引く。
    そうしないと、画面では見られない中身がチャットからは見える、という
    抜け道ができる。
    """
    return bool(getattr(g.get("user"), "is_admin", False))


def _advance(chat: dict, scope: list[dict], question: str) -> None:
    """最終回答が出るまで回す。

    実行するSQLは _call_previews で毎回チャットに出るので、
    何が走ったかは後からでも追える。
    """
    guard = _Guard()
    for _ in range(config.MAX_AGENT_STEPS):
        try:
            msg = llm.chat(chat["messages"], tools.build_tools(scope, admin=_is_admin()),
                           model=models.current(g.user))
        except Exception as e:
            chat["render_log"].append({"role": "assistant", "kind": "error",
                                       "message": f"LLM呼び出しに失敗しました: {e}"})
            return

        chat["messages"].append(_msg_to_dict(msg))
        if msg.content:
            chat["render_log"].append({"role": "assistant", "kind": "text",
                                       "content": msg.content})

        calls = _extract_calls(msg)
        if not calls:
            return                             # 最終回答

        fresh = [c for c in calls if guard.known_failure(c) is None]
        chat["render_log"].extend(_call_previews(fresh, scope, question))
        _execute(chat, calls, scope, guard)
        if guard.stuck:
            chat["render_log"].append(_stop_note(_STUCK_MESSAGE))
            return

    chat["render_log"].append(_stop_note(
        f"（ツールの呼び出しが{config.MAX_AGENT_STEPS}回に達したので、"
        "ここで一区切りにしました。続きが必要なら、"
        "「続けて」と送るか、質問を分けてください。）"))


_STUCK_MESSAGE = ("（同じ操作の失敗が続いたため、ここで止めました。"
                  "上のエラーに出ている列名や条件を指定し直すか、"
                  "質問を「まず集計だけ」「次にグラフ」のように分けて試してください。）")


def _merge_alerts(content: str, alerts: list[dict]) -> str:
    """検算の不一致をツール結果に混ぜて、LLMに気づかせる。"""
    notes = [verify.llm_note(a) for a in alerts]
    try:
        data = json.loads(content)
        if isinstance(data, dict):
            data["verification_warnings"] = notes
            return json.dumps(data, ensure_ascii=False, default=str)
    except (ValueError, TypeError):
        pass
    return content + "\n\n【検算の不一致】" + json.dumps(notes, ensure_ascii=False, default=str)


def _fresh_alerts(chat: dict, alerts: list[dict]) -> list[dict]:
    """この会話でまだ見せていない検算だけを残す。

    同じデータ・同じルールの警告を質問のたびに繰り返すと、読まれなくなる。
    データが変わる（=キーの版が変わる）と、また1回だけ出る。
    """
    seen = {i.get("verify_key") for i in chat["render_log"] if i.get("verify_key")}
    return [a for a in alerts if a["key"] not in seen]


def _execute(chat: dict, calls: list[dict], scope: list[dict],
             guard: "_Guard | None" = None) -> None:
    for c in calls:
        if guard is not None and guard.known_failure(c) is not None:
            # 同じ失敗の繰り返し。実行せずに差し戻す（時間もお金も使わない）
            chat["messages"].append({"role": "tool", "tool_call_id": c["id"],
                                     "content": guard.repeated(c)})
            continue
        res = tools.dispatch(c["name"], c["arguments"], scope, scope, admin=_is_admin())
        if guard is not None:
            guard.note(c, res)

        # 相互検証。数字が食い違っていたら、回答の前に画面とLLMの両方へ
        content = res["llm_content"]
        alerts = _fresh_alerts(chat, res.get("verify_alerts") or [])
        if alerts:
            content = _merge_alerts(content, alerts)
        chat["messages"].append({"role": "tool", "tool_call_id": c["id"],
                                 "content": content})
        if res.get("render"):
            chat["render_log"].append(dict(res["render"]))
        for a in alerts:
            chat["render_log"].append(verify.render_item(a))


def _reply(chat: dict, before: int, replace: bool = False):
    _persist(chat)
    return jsonify({
        "ok": True,
        # replace=True のときは画面をいったん空にして全部描き直してもらう
        "items": _web_log(chat["render_log"], 0 if replace else before),
        "replace": replace,
        "chat_id": chat.get("id"),
        "title": chat.get("title", ""),
    })


class _TurnError(Exception):
    """送信を始められないときの理由。画面にそのまま出せる文言を持つ。"""

    def __init__(self, message: str, status: int = 400, **extra):
        super().__init__(message)
        self.payload = {"error": message, **extra}
        self.status = status


@bp.errorhandler(_TurnError)
def _turn_error(e: _TurnError):
    return jsonify(e.payload), e.status


def _auto_scope(question: str, chat: dict) -> list[dict]:
    """質問に合わせて対象DBを決める。利用者はDBを選ばない。

    3つを合わせる:
      ルーターが選んだDB … 質問と各DBの要約を突き合わせた前段の判定（llm.route_dbs）。
                           無関係なDBのカタログを本番のプロンプトに入れないための要。
      この会話で使ったDB … 「それをグラフに」のような続きの質問はルーターに手がかりが
                           無いので、実際にSQLが触ったDBは残し続ける。
      判定できないとき   … 全DB。ルーターの不調で答えられなくなるのがいちばん悪い。
    """
    all_names = [f.name for f in db.list_db_files()]
    history = [i.get("content") or "" for i in (chat.get("render_log") or [])
               if i.get("role") == "user" and i.get("kind") == "text"]
    routed = llm.route_dbs(question, history)
    names = set(routed if routed else all_names)
    names |= set(chat.get("db_names") or [])          # この会話で実際に使ったDB
    return build_scope({n: [] for n in all_names if n in names})


def _begin_turn():
    """/send と /stream に共通する前処理。

    質問を検証し、スコープを確定し、会話にユーザーの発言を積むところまで。
    始められないときは _TurnError を投げる（呼び出し側で分岐を書かずに済む）。
    """
    text = (request.json.get("text") or "").strip()
    if not text:
        raise _TurnError("質問を入力してください。")
    if not llm.is_configured():
        raise _TurnError("LLMが未設定です。env の OPENAI_* を設定してください。")

    chat = _load_current()
    scope = _auto_scope(text, chat)
    if not scope:
        raise _TurnError("data/ に分析できるDBがありません。"
                         "「データ取り込み」からDBを作成してください。")

    images, show = _images_from((request.json or {}).get("images"))
    if images and not models.is_vision(models.current(g.user)):
        raise _TurnError("いま選ばれているモデルは画像を扱えません。")
    if not chat["messages"] or chat["messages"][0].get("role") != "system":
        chat["messages"].insert(0, {"role": "system", "content": ""})
    chat["messages"][0] = {"role": "system",
                           "content": llm.build_system_prompt(scope, admin=_is_admin())}
    chat["messages"].append(llm.user_message(text, images))
    # 質問の時刻はここで入れる。保存は応答が終わってからなので、
    # 保存時に付けると「聞いた時刻」ではなく「答え終わった時刻」になってしまう。
    chat["render_log"].append({"role": "user", "kind": "text", "content": text,
                               "at": chats.now(),
                               **({"images": show} if show else {})})
    return chat, scope, text


@bp.post("/api/chat/send")
@login_required
def send():
    chat, scope, text = _begin_turn()
    before = len(chat["render_log"]) - 1
    _advance(chat, scope, text)
    return _reply(chat, before)


# =============================================================================
# ストリーミング送信
#
# 通常の /api/chat/send は、ツールを何回か呼んで最終回答が出るまで待ってから
# まとめて返す。待ち時間が長く、届いた瞬間に画面がいちばん下へ飛ぶ。
# こちらは、起きたことをその都度 Server-Sent Events で流す。
# =============================================================================

def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def _stream_advance(chat: dict, scope: list[dict], question: str):
    """_advance のストリーミング版。起きたことを逐次 yield する。"""
    guard = _Guard()
    for _ in range(config.MAX_AGENT_STEPS):
        msg = None
        try:
            for kind, payload in llm.chat_stream(
                    chat["messages"], tools.build_tools(scope, admin=_is_admin()),
                    model=models.current(g.user)):
                if kind == "text":
                    yield _sse("delta", {"text": payload})
                else:
                    msg = payload
        except Exception as e:
            item = {"role": "assistant", "kind": "error",
                    "message": f"LLM呼び出しに失敗しました: {e}"}
            chat["render_log"].append(item)
            yield _sse("item", _web_log([item])[0])
            return
        if msg is None:
            return

        chat["messages"].append(_msg_to_dict(msg))
        if msg.content:
            chat["render_log"].append({"role": "assistant", "kind": "text",
                                       "content": msg.content})
        calls = _extract_calls(msg)
        if not calls:
            yield _sse("text_end", {})
            return                                  # 最終回答

        yield _sse("text_end", {})
        fresh = [c for c in calls if guard.known_failure(c) is None]
        previews = _call_previews(fresh, scope, question)
        chat["render_log"].extend(previews)
        for p in _web_log(previews):
            yield _sse("item", p)

        for c in calls:
            if guard.known_failure(c) is not None:
                _execute(chat, [c], scope, guard)      # 実行せず差し戻すだけ
                continue
            yield _sse("running", {"name": c["name"],
                                   "label": TOOL_LABELS.get(c["name"], c["name"])})
            before = len(chat["render_log"])
            _execute(chat, [c], scope, guard)
            for item in _web_log(chat["render_log"], before):
                yield _sse("item", item)

        if guard.stuck:
            item = _stop_note(_STUCK_MESSAGE)
            chat["render_log"].append(item)
            yield _sse("item", _web_log([item])[0])
            return

    item = {"role": "assistant", "kind": "text",
            "content": f"（ツールの呼び出しが{config.MAX_AGENT_STEPS}回に達したので、"
                       "ここで一区切りにしました。続きが必要なら、"
                       "「続けて」と送るか、質問を分けてください。）"}
    chat["render_log"].append(item)
    yield _sse("item", _web_log([item])[0])


@bp.post("/api/chat/stream")
@login_required
def stream():
    """1問1答をSSEで流す。イベントの種類:

        delta     … 回答の文字（少しずつ）
        text_end  … ひとまとまりの回答が終わった
        item      … 表・グラフ・ファイルなどの描画アイテム
        running   … ツールを実行し始めた
        end       … 終わり（保存後の会話ID・タイトルを載せる）
    """
    chat, scope, text = _begin_turn()
    # 会話IDはここで確定させてセッションに入れる。
    # 応答を流し始めるとセッションに書けなくなるので、あとから入れても消える
    # （次の質問が別の会話として始まってしまう）。
    if not chat.get("id"):
        chat["id"] = chats.new_id()
    session["chat_id"] = chat["id"]

    def generate():
        try:
            yield from _stream_advance(chat, scope, text)
        except Exception as e:                       # 途中で落ちても接続は閉じる
            yield _sse("item", {"role": "assistant", "kind": "error",
                                "message": f"処理中にエラーが発生しました: {e}"})
        finally:
            _persist(chat)
            yield _sse("end", {"chat_id": chat.get("id"), "title": chat.get("title", "")})

    return Response(stream_with_context(generate()),
                    mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})   # nginx等でのバッファ抑止


@bp.post("/api/chat/rewind")
@login_required
def rewind():
    """指定の発言まで巻き戻して、そこからやり直す。

    text を送ると、その発言を書き換えたうえで会話を続ける。
    text が空なら巻き戻すだけ（それ以降を消して、入力欄に戻す）。
    どちらも、その発言より後のやり取りは消える。
    """
    body = request.json or {}
    try:
        turn = int(body.get("turn"))
    except (TypeError, ValueError):
        return jsonify({"error": "巻き戻す位置が指定されていません。"}), 400
    text = (body.get("text") or "").strip()

    chat = _load_current()
    try:
        messages, render_log, original = _split_at_turn(chat, turn)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    dropped = len(chat["render_log"]) - len(render_log)
    chat["messages"], chat["render_log"] = messages, render_log

    if not text:
        _persist(chat)
        return jsonify({"ok": True, "replace": True,
                        "items": _web_log(chat["render_log"]),
                        "restored": original, "dropped": dropped,
                        "chat_id": chat.get("id"), "title": chat.get("title", "")})

    if not llm.is_configured():
        return jsonify({"error": "LLMが未設定です。env の OPENAI_* を設定してください。"}), 400
    scope = _auto_scope(text, chat)
    if not scope:
        return jsonify({"error": "data/ に分析できるDBがありません。"}), 400

    # やり直しなので、カタログの現状に合わせてシステムプロンプトも入れ直す
    if not chat["messages"] or chat["messages"][0].get("role") != "system":
        chat["messages"].insert(0, {"role": "system", "content": ""})
    chat["messages"][0] = {"role": "system",
                           "content": llm.build_system_prompt(scope, admin=_is_admin())}
    chat["messages"].append({"role": "user", "content": text})
    chat["render_log"].append({"role": "user", "kind": "text", "content": text,
                               "at": chats.now()})

    _advance(chat, scope, text)
    return _reply(chat, 0, replace=True)


# =============================================================================
# メール送信
#
# 送信はここだけ。LLMは compose_email で下書きを作るところまでしかできず、
# 実際に外へ出るのはユーザーが画面の「送信」を押したときだけにしてある。
# 宛先の間違いは取り消せないため、AIの判断だけで外部に何かを出さない。
# =============================================================================

@bp.get("/api/mail/status")
@admin_required
def mail_status():
    """送信サーバの状態と送信ログ。設定情報なので管理者のみ。"""
    return jsonify({**mailer.status(), "log": mailer.sent_log(20)})


def _attachments_for(chat: dict, names: list) -> tuple[list, list]:
    """この会話で作ったファイルから、名前が一致する添付を集める。

    'all' が指定されたら直近に作ったものを全部付ける。
    戻り値は (添付, 見つからなかった名前)。
    """
    made = [i for i in (chat.get("render_log") or [])
            if i.get("kind") == "file" and i.get("data")]
    wanted = [str(n) for n in (names or [])]
    if not wanted:
        return [], []
    if any(w.lower() == "all" for w in wanted):
        picked = made[-5:]
        return [{"filename": i.get("filename"), "mime": i.get("mime"),
                 "data": i["data"]} for i in picked], []

    out, missing = [], []
    for w in wanted:
        hit = next((i for i in reversed(made)
                    if (i.get("filename") or "").lower() == w.lower()), None)
        if hit is None:                      # 部分一致でも拾う（拡張子の付け忘れなど）
            hit = next((i for i in reversed(made)
                        if w.lower() in (i.get("filename") or "").lower()), None)
        if hit is None:
            missing.append(w)
        else:
            out.append({"filename": hit.get("filename"), "mime": hit.get("mime"),
                        "data": hit["data"]})
    return out, missing


@bp.post("/api/mail/preview")
@login_required
def mail_preview():
    """送信前の最終確認。実物と同じ組み立てをして中身を返す。"""
    draft = (request.json or {}).get("draft") or {}
    chat = _load_current()
    files, missing = _attachments_for(chat, draft.get("attach_filenames"))
    view = mailer.preview(draft, files)
    view["missing_attachments"] = missing
    return jsonify(view)


@bp.post("/api/mail/send")
@login_required
def mail_send():
    """実際に送る。押したのがユーザー本人であることが唯一の前提。"""
    body = request.json or {}
    draft = body.get("draft") or {}
    if not body.get("confirm"):
        return jsonify({"error": "確認されていません。"}), 400
    chat = _load_current()
    files, missing = _attachments_for(chat, draft.get("attach_filenames"))
    if missing:
        return jsonify({"error": f"添付ファイルが見つかりません: {', '.join(missing)}"}), 400
    try:
        record = mailer.send(draft, files, user=g.user.username)
    except mailer.MailError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"送信に失敗しました: {e}"}), 500

    chat["render_log"].append({
        "role": "assistant", "kind": "text",
        "content": ("📤 " + record["message"]
                    + f"（件名: {record['subject']} / 宛先: {', '.join(record['to'])}"
                    + (f" / 添付: {', '.join(record['attachments'])}"
                       if record["attachments"] else "") + "）")})
    _persist(chat)
    return jsonify({"ok": True, "record": record})


@bp.post("/api/mail/test")
@admin_required
def mail_test():
    """SMTPの疎通確認だけ（メールは送らない）。"""
    return jsonify(mailer.test_connection())


@bp.post("/api/chat/save-example")
@admin_required
def save_example():
    """正しかったSQLをカタログの例文へ還流する。

    チャット画面から呼ぶがカタログを書き換えるので、カタログ画面と同じく管理者のみ。
    """
    scope = build_scope({f.name: [] for f in db.list_db_files()})
    q = (request.json.get("question") or "").strip()
    sql = (request.json.get("sql") or "").strip()
    if not q or not sql:
        return jsonify({"error": "質問とSQLの両方が必要です。"}), 400

    # 例文はDBごとのファイルに残すので、保存先を1つに決める必要がある。
    # 複数のDBを選んでいても、SQLがどのDBを見ているかで決められる。
    # DBをまたぐ例文（人事の勤怠 × マスタの社員、など）は珍しくないため、
    # 「1つだけ選んでいるとき」に限ると保存できる場面が狭くなりすぎる。
    hits = dbs_in_sql(sql, scope)
    target = hits[0] if hits else (scope[0] if len(scope) == 1 else None)
    if target is None:
        return jsonify({"error": "このSQLがどのDBのものか判断できませんでした。"
                                 "対象データでDBを1つだけ選んでから保存してください。"}), 400

    p = Path(target["path"])
    meta = catalog.load_meta(p)
    examples = meta.get("examples") or []

    # 同じSQLが既にあるなら足さない。例文は毎回プロンプトに載るので、
    # 言い回し違いで同じSQLが並ぶとトークンを食うだけで精度は上がらない。
    same = catalog.find_example(examples, sql)
    if same is not None:
        return jsonify({"ok": True, "added": False,
                        "message": f"{p.name} に同じSQLの例文が既にあります"
                                   f"（「{same['q']}」）。登録は増やしませんでした。"})
    if len(examples) >= catalog.EXAMPLES_MAX:
        return jsonify({"error": f"{p.name} の例文は{catalog.EXAMPLES_MAX}件までです。"
                                 "データカタログの「質問とSQLの例文」で古いものを"
                                 "整理してください。"}), 400

    meta["examples"] = catalog.dedupe_examples([*examples, {"q": q, "sql": sql}])
    catalog.save_meta(p, meta)
    others = [s["name"] for s in hits[1:]]
    message = f"{p.name} の例文に追加しました。"
    if others:
        # DBをまたぐ例文は、チャットで両方を選んでいないと再現しない
        message += (f"（{'、'.join(others)} も参照しています。"
                    "使うときはこれらのDBも一緒に選んでください）")
    return jsonify({"ok": True, "added": True, "message": message})
