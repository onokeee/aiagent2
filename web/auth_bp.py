"""ログイン / ログアウト。認証の中身は auth.py のプロバイダに任せる。"""
from __future__ import annotations

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

import auth

from .helpers import login_user, logout_user

bp = Blueprint("auth", __name__)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.get("user") is not None:
        return redirect(url_for("chat.index"))

    setup_needed = False
    try:
        provider = auth.get_provider()
        # 常設の管理者で入れるなら、ユーザー未登録でも詰まらない
        if (provider.name == "local" and not auth.admin_enabled()
                and not (auth.load_users_file().get("users") or [])):
            setup_needed = True
    except auth.AuthError as e:
        return render_template("login.html", fatal=str(e))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if not username or not password:
            flash("ユーザー名とパスワードを入力してください。", "warning")
        else:
            try:
                user = auth.authenticate(username, password)
            except auth.AuthError as e:
                flash(f"認証できませんでした: {e}", "error")
            else:
                if user is None:
                    flash("ユーザー名またはパスワードが違います。", "error")
                else:
                    login_user(user)
                    nxt = request.args.get("next") or url_for("chat.index")
                    return redirect(nxt if nxt.startswith("/") else url_for("chat.index"))

    return render_template("login.html", setup_needed=setup_needed)


@bp.post("/logout")
def logout():
    logout_user()
    return redirect(url_for("auth.login"))
