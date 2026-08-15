"""細々したAPI: 生成ファイルのダウンロードと plotly.js の配信。"""
from __future__ import annotations

from pathlib import Path

from flask import Blueprint, Response, abort, g, send_file

from . import filestore
from .helpers import login_required

bp = Blueprint("api", __name__)


@bp.get("/api/file/<token>")
@login_required
def download(token: str):
    item = filestore.get(token, g.user.username)
    if item is None:
        abort(404)
    from io import BytesIO
    return send_file(BytesIO(item["data"]), mimetype=item["mime"],
                     as_attachment=True, download_name=item["filename"])


@bp.get("/vendor/plotly.min.js")
def plotly_js():
    """plotly パッケージ同梱のJSをそのまま配る（CDNに出ない・オフラインで動く）。"""
    import plotly
    p = Path(plotly.__file__).parent / "package_data" / "plotly.min.js"
    if not p.exists():
        abort(404)
    return Response(p.read_bytes(), mimetype="application/javascript",
                    headers={"Cache-Control": "public, max-age=604800"})
