"""モデル設定の画面（管理者のみ）。

チャット画面のプルダウンに出す候補・既定のモデル・画像を扱えるモデルの
判定キーワードを決める。ここで候補から外したモデルは、既にそれを選んで
いた利用者も使えなくなり、既定のモデルに戻る。
"""
from __future__ import annotations

from flask import Blueprint, g, jsonify, render_template, request

import db
import models

from .helpers import admin_required, build_scope

bp = Blueprint("models", __name__)


def _scope():
    """文脈の使用量を測るための基準。

    上限は全員に効くので、見せる数字は「いちばん重いとき」＝全DBを選んだ場合に
    そろえる。管理者本人の選択で測ると、人によって見える数字が変わってしまう。
    """
    return build_scope({f.name: [] for f in db.list_db_files()})


@bp.get("/models")
@admin_required
def index():
    return render_template("models.html", status=models.admin_status(scope=_scope()))


@bp.get("/api/models/admin")
@admin_required
def get_admin():
    return jsonify(models.admin_status(refresh=request.args.get("refresh") == "1",
                                       scope=_scope()))


@bp.post("/api/models/admin")
@admin_required
def post_admin():
    try:
        models.save_admin(request.json or {}, user=g.user.username)
        return jsonify({"ok": True, **models.admin_status(scope=_scope())})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
