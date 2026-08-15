"""メール設定の画面。

送信サーバ（ホスト・ポート・タイムアウト）と、誰から誰に送ってよいかを決める。
env の値は初期値として使い、画面から保存したものが優先される。

暗号化と認証は社内リレー前提（なし）なので画面には出さない。必要な環境では
env の SMTP_SECURITY / SMTP_USER / SMTP_PASSWORD で指定する。
閲覧・変更ともに管理者のみ。
"""
from __future__ import annotations

from flask import Blueprint, g, jsonify, render_template, request

import mailer

from .helpers import admin_required

bp = Blueprint("mail", __name__)


@bp.get("/mail")
@admin_required
def index():
    return render_template("mail.html", status=mailer.status(),
                           log=mailer.sent_log(20))


@bp.get("/api/mail/settings")
@admin_required
def get_settings():
    return jsonify({**mailer.status(), "editable": True,
                    "log": mailer.sent_log(20)})


@bp.post("/api/mail/settings")
@admin_required
def post_settings():
    """送信サーバ・差出人・宛先の許可リストを保存する。"""
    try:
        mailer.save_settings(request.json or {}, user=g.user.username)
    except mailer.MailError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, **mailer.status()})
