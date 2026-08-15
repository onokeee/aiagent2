"""Flask アプリ本体（アプリファクトリ）。

画面まわりだけをここに置き、業務ロジックは既存モジュールをそのまま使う。
  db / catalog / llm / tools / custom_tools / charts / exports / excel / analysis
  auth / chats / importer / jobs / scheduler

Streamlit 版との違いは「状態の持ち方」だけ。
  Streamlit: st.session_state（プロセス内）
  Flask    : サーバ側セッション + 会話の実体は chats.py（ファイル）
"""
from __future__ import annotations

import os
import secrets

from flask import Flask

import auth
import config
import scheduler

_SECRET_FILE = config.BASE_DIR / ".flask_secret"


def _secret_key() -> str:
    """セッション署名鍵。再起動でログアウトさせないためファイルに保持する。"""
    env = os.getenv("FLASK_SECRET_KEY", "").strip()
    if env:
        return env
    if _SECRET_FILE.exists():
        return _SECRET_FILE.read_text(encoding="utf-8").strip()
    key = secrets.token_urlsafe(48)
    _SECRET_FILE.write_text(key, encoding="utf-8")
    try:
        os.chmod(_SECRET_FILE, 0o600)
    except OSError:
        pass
    return key


def _warn_if_no_admin() -> None:
    """管理者が1人も居ない設定なら、起動時に知らせる。

    認証APIがグループを返さない構成では、管理者になれるのは env の ADMIN_PASS で
    入る admin だけになる。これを設定し忘れると、カタログ・取り込み・モデル・メールの
    画面に誰も入れないまま動き続ける。気づけるのは「設定を直したいとき」なので、
    起動時に言う。
    """
    if auth.admin_enabled():
        return
    try:
        provider = auth.get_provider()
    except auth.AuthError:
        return
    if provider.name == "local":
        has_admin = any(config.AUTH_ADMIN_GROUP in (u.get("groups") or [])
                        for u in (auth.load_users_file().get("users") or []))
        if has_admin:
            return
        how = "manage_users.py add <ユーザー名> --admin で管理者を作るか、"
    else:
        # 認証APIがグループを返さないなら、ここに来た時点で管理者は現れない
        how = f"認証APIが '{config.AUTH_ADMIN_GROUP}' グループを返すようにするか、"
    print(f"[auth] 警告: 管理者が1人も居ません。{how}"
          "env の ADMIN_PASS を設定してください。"
          "このままではデータカタログ・データ取り込み・モデル設定・メール設定を"
          "誰も開けません（チャットは使えます）。")


def create_app() -> Flask:
    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.config.update(
        SECRET_KEY=_secret_key(),
        MAX_CONTENT_LENGTH=64 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        JSON_AS_ASCII=False,
        # テンプレートだけ自動リロードするとPython側と食い違うので、
        # 開発時も両方まとめて再起動する運用にする（既定はOFF）
        TEMPLATES_AUTO_RELOAD=bool(os.getenv("FLASK_DEBUG")),
    )

    from . import (api_bp, auth_bp, catalog_bp, chat_bp, import_bp, mail_bp,
                   models_bp)
    app.register_blueprint(auth_bp.bp)
    app.register_blueprint(chat_bp.bp)
    app.register_blueprint(catalog_bp.bp)
    app.register_blueprint(import_bp.bp)
    app.register_blueprint(mail_bp.bp)
    app.register_blueprint(models_bp.bp)
    app.register_blueprint(api_bp.bp)

    from .helpers import inject_globals, load_user_into_context
    app.before_request(load_user_into_context)
    app.context_processor(inject_globals)

    _warn_if_no_admin()

    @app.after_request
    def _no_html_cache(res):
        """画面のHTMLはキャッシュさせない。

        中身はログイン中の人・選択中のDB・カタログの現状で毎回変わるので、
        取っておいても正しくない。既定ではキャッシュ指定が付かず、ブラウザが
        独自の判断で古い画面を出すため、直したはずの表示が変わらないことがある。
        静的ファイル（CSS/JS）は ETag で更新を見ているのでそのまま。
        """
        if res.mimetype == "text/html":
            res.headers["Cache-Control"] = "no-store"
        return res

    # 定期取り込みの裏スレッド。Streamlit版と同じく cron 不要。
    # Flask では起動時に1回通るので、誰かがページを開くのを待たずに動き出す。
    scheduler.start()
    return app
