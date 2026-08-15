"""グラフを画像（PNG）にする。Word や PowerPoint に貼るために使う。

plotly の画像化は Chrome を裏で動かす（kaleido）。社内サーバに Chrome が
入っていないこともあるので、失敗したら None を返し、呼び出し側は
表や説明文だけで文書を作れるようにしてある。文書作成そのものは止めない。

画像は1文書内で何度も作るため、同じ図は使い回す（同じ処理を2回しない）。
"""
from __future__ import annotations

import hashlib
import threading

import config

_lock = threading.Lock()
_cache: dict[str, bytes] = {}
_MAX_CACHE = 40
# 一度失敗したら、その実行中は再挑戦しない（1枚あたり数秒待たされるため）
_broken: list[str] = []


def available() -> bool:
    """画像化できる環境かどうか（1回だけ実際に試して覚える）。"""
    if _broken:
        return False
    if _cache:
        return True
    import plotly.graph_objects as go
    return render(go.Figure(), width=80, height=60) is not None


def why_unavailable() -> str:
    return _broken[0] if _broken else ""


def render(fig, width: int | None = None, height: int | None = None,
           scale: float | None = None) -> bytes | None:
    """plotly の figure を PNG のバイト列にする。できなければ None。"""
    if _broken:
        return None
    w = int(width or config.REPORT_IMAGE_WIDTH)
    h = int(height or config.REPORT_IMAGE_HEIGHT)
    s = float(scale or config.REPORT_IMAGE_SCALE)
    try:
        key = hashlib.sha1(
            (fig.to_json() + f"|{w}x{h}@{s}").encode("utf-8")).hexdigest()
    except Exception:
        key = None
    if key:
        with _lock:
            hit = _cache.get(key)
        if hit is not None:
            return hit
    try:
        data = fig.to_image(format="png", width=w, height=h, scale=s)
    except Exception as e:
        msg = str(e).splitlines()[0][:200]
        _broken.append(f"グラフを画像にできませんでした（{type(e).__name__}: {msg}）。"
                       "文書には表と説明だけを入れます。"
                       "画像も入れたい場合は、サーバに Chrome/Chromium を用意して"
                       "kaleido が使える状態にしてください。")
        print(f"[figures] 画像化を無効にしました: {msg}")
        return None
    if key:
        with _lock:
            _cache[key] = data
            while len(_cache) > _MAX_CACHE:
                _cache.pop(next(iter(_cache)))
    return data


def for_print(fig, *, width=None, height=None):
    """紙・スライド向けに見た目を整えてから画像にする。

    画面はマウスで拡大できるが、紙とスライドはできない。
    文字を大きめに、余白を詰め、目盛りに桁区切りを入れる。
    """
    fig = _polish(fig)
    return render(fig, width=width, height=height)


def _polish(fig):
    """印刷向けの体裁に整える（元の figure は壊さない）。"""
    import copy
    fig = copy.deepcopy(fig)
    fig.update_layout(
        template="plotly_white",
        font=dict(family=config.REPORT_FONT_JA + ", sans-serif", size=15,
                  color="#1F1F1F"),
        title=dict(font=dict(size=17)),
        margin=dict(l=70, r=30, t=50, b=60),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0,
                    font=dict(size=13)),
        paper_bgcolor="white", plot_bgcolor="white",
        colorway=["#1F4E79", "#F4B183", "#70AD47", "#C55A11", "#7F7F7F",
                  "#2E75B6", "#A9D18E", "#FFD966", "#9DC3E6", "#BFBFBF"],
    )
    fig.update_xaxes(showgrid=False, linecolor="#BFBFBF", ticks="outside",
                     tickfont=dict(size=13))
    fig.update_yaxes(gridcolor="#E8E8E8", zerolinecolor="#BFBFBF",
                     tickfont=dict(size=13), tickformat=",")
    return fig
