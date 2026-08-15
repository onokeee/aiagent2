"""メールの宛先探しと下書き。送信そのものは画面のボタンからだけ。"""
from __future__ import annotations


import mailer
from .common import _err, _json


def _find_mail_recipients(args: dict, scope: list[dict]) -> dict:
    res = mailer.find_recipients(scope, args.get("query") or "",
                                 limit=int(args.get("limit") or 50),
                                 table=args.get("table"))
    cands = res["candidates"]
    return {
        "ok": res["ok"],
        "llm_content": _json({
            "status": "recipients", "message": res["message"],
            "sources": res["sources"],
            "count": len(cands),
            "candidates": [{k: c[k] for k in ("email", "name", "dept", "source")}
                           for c in cands[:50]],
            "note": "ここに出たアドレスだけを宛先に使うこと。推測で作らない。",
        }),
        "render": {"role": "assistant", "kind": "table",
                   "columns": ["メールアドレス", "氏名", "部署", "出所"],
                   "rows": [[c["email"], c["name"], c["dept"], c["source"]]
                            for c in cands]} if cands else
                  {"role": "assistant", "kind": "text", "content": res["message"]},
    }


def _compose_email(args: dict, scope: list[dict]) -> dict:
    to = list(args.get("to") or [])
    matched = []
    if args.get("to_query"):
        res = mailer.find_recipients(scope, args["to_query"], limit=50)
        matched = res["candidates"]
        to += [c["email"] for c in matched if c["valid"]]
        if not matched:
            return _err(f"「{args['to_query']}」に一致する宛先が見つかりませんでした。"
                        "find_mail_recipients で候補を確認してください。")
    to = list(dict.fromkeys(a for a in to if a))
    draft = {"to": to, "cc": args.get("cc") or [], "bcc": args.get("bcc") or [],
             "subject": args.get("subject") or "", "body": args.get("body") or "",
             "reply_to": args.get("reply_to") or "",
             "attach_filenames": list(args.get("attach_filenames") or [])}
    view = mailer.preview(draft)
    # 添付は会話ログから web 側が解決する。ここでは名前だけ持たせる。
    return {
        "ok": not view["errors"],
        "llm_content": _json({
            "status": "mail_draft" if not view["errors"] else "mail_draft_invalid",
            "to": view["to"], "cc": view["cc"], "bcc_count": len(view["bcc"]),
            "subject": view["subject"], "body_lines": view["body_lines"],
            "attach_filenames": draft["attach_filenames"],
            "matched_from_db": [{"email": c["email"], "name": c["name"],
                                 "dept": c["dept"]} for c in matched[:20]],
            "problems": view["errors"],
            "note": ("下書きを画面に出した。送信するかはユーザーが画面のボタンで決める。"
                     "こちらから送信することはできないので、"
                     "『内容を確認して送信ボタンを押してください』と伝えること。"),
        }),
        "render": {"role": "assistant", "kind": "mail_draft", "draft": draft,
                   "preview": view},
    }

HANDLERS = {
    "find_mail_recipients": _find_mail_recipients,
    "compose_email": _compose_email,
}

SQL_TOOLS: set[str] = set()
