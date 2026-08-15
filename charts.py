"""SELECT結果からグラフを組み立てる。

チャット画面とユーザー定義ツールの両方がここを通るので、
対応するグラフ種別を増やすときはこのファイルだけを直せばよい。

種別の追加手順:
  1. CHART_SPECS に (説明, 必要な指定, 分類) を足す
  2. _BUILDERS に組み立て関数を足す
validate() と画面の説明文は CHART_SPECS から自動で作られる。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# 列の指定をリストで受け取るもの（存在チェックの仕方が違う）
LIST_FIELDS = ("path", "dimensions")

# 種別 -> (日本語の説明, 必要な指定, 分類)
CHART_SPECS: dict[str, tuple[str, tuple, str]] = {
    # --- 比較 ---------------------------------------------------------------
    "bar":         ("棒。カテゴリ別の比較", ("x", "y"), "比較"),
    "hbar":        ("横棒。項目名が長いときや順位表に", ("x", "y"), "比較"),
    "stacked_bar": ("積み上げ棒。内訳つきの比較", ("x", "y"), "比較"),
    "percent_bar": ("100%積み上げ棒。構成比の比較", ("x", "y"), "比較"),
    "lollipop":    ("ロリポップ。棒より軽く順位を見せる", ("x", "y"), "比較"),
    "dumbbell":    ("ダンベル。2時点の差を1行で比べる", ("x", "y", "y2"), "比較"),
    "pareto":      ("パレート図。棒＋累積比率で重点を見つける", ("x", "y"), "比較"),
    "pyramid":     ("人口ピラミッド。左右に分けた横棒", ("x", "y", "color"), "比較"),
    "marimekko":   ("マリメッコ。幅も高さも意味を持つ積み上げ", ("x", "y", "size"), "比較"),
    "radar":       ("レーダー。複数指標のバランス", ("x", "y"), "比較"),
    "polar_bar":   ("極座標の棒。方位や時間帯の分布", ("x", "y"), "比較"),
    "bump":        ("バンプ。順位の入れ替わりを追う", ("x", "y", "color"), "比較"),
    # --- 推移 ---------------------------------------------------------------
    "line":        ("折れ線。時系列や推移", ("x", "y"), "推移"),
    "step":        ("階段。在庫や料金など段階的に変わる値", ("x", "y"), "推移"),
    "area":        ("面。積み上げの推移", ("x", "y"), "推移"),
    "area_percent": ("100%面。構成比の推移", ("x", "y"), "推移"),
    "range_area":  ("幅つき折れ線。予測の上下限や信頼区間", ("x", "y", "lower", "upper"), "推移"),
    "slope":       ("スロープ。2時点の順位・水準の変化", ("x", "y", "color"), "推移"),
    "candlestick": ("ローソク足。始値・高値・安値・終値", ("x", "open", "high", "low", "close"), "推移"),
    "ohlc":        ("OHLC。ローソク足の棒型", ("x", "open", "high", "low", "close"), "推移"),
    "gantt":       ("ガントチャート。作業や期間の並び", ("x", "start", "end"), "推移"),
    "calendar":    ("カレンダーヒートマップ。日ごとの多寡", ("x", "y"), "推移"),
    "control_chart": ("管理図。平均±3σを外れた点を見つける", ("x", "y"), "推移"),
    # --- 構成 ---------------------------------------------------------------
    "pie":         ("円。構成比", ("x", "y"), "構成"),
    "donut":       ("ドーナツ。構成比（中央に合計）", ("x", "y"), "構成"),
    "treemap":     ("ツリーマップ。階層つき構成比", ("path", "y"), "構成"),
    "sunburst":    ("サンバースト。階層つき構成比（円形）", ("path", "y"), "構成"),
    "icicle":      ("アイシクル。階層を短冊で並べる", ("path", "y"), "構成"),
    "funnel":      ("ファネル。段階ごとの減少", ("x", "y"), "構成"),
    "waterfall":   ("ウォーターフォール。増減の内訳", ("x", "y"), "構成"),
    "sankey":      ("サンキー。流れと量（どこからどこへ）", ("source", "target", "y"), "構成"),
    # --- 分布 ---------------------------------------------------------------
    "histogram":   ("ヒストグラム。1つの数値の分布", ("x",), "分布"),
    "density":     ("密度曲線。ヒストグラムをなめらかに", ("x",), "分布"),
    "ecdf":        ("累積分布。「〇〇以下が何%か」を読む", ("x",), "分布"),
    "box":         ("箱ひげ。カテゴリ別のばらつき", ("y",), "分布"),
    "violin":      ("バイオリン。分布の形まで見る", ("y",), "分布"),
    "strip":       ("ストリップ。個々の点を並べる", ("y",), "分布"),
    "ridgeline":   ("リッジライン。群ごとの分布を重ねる", ("x", "color"), "分布"),
    "qq":          ("Q-Qプロット。正規分布からのズレ", ("x",), "分布"),
    # --- 関係 ---------------------------------------------------------------
    "scatter":     ("散布。2つの数値の相関", ("x", "y"), "関係"),
    "bubble":      ("バブル。散布＋大きさで3指標", ("x", "y", "size"), "関係"),
    "histogram2d": ("2次元ヒストグラム。点が多すぎるときの散布図", ("x", "y"), "関係"),
    "contour":     ("等高線。2変数の密度", ("x", "y"), "関係"),
    "heatmap":     ("ヒートマップ。2軸の集計をマス目で", ("x", "y"), "関係"),
    "matrix":      ("行列ヒートマップ。集計済みのクロス表や相関行列をそのまま色で", (), "関係"),
    "scatter_matrix": ("散布図行列。数値列を総当たりで見る", ("dimensions",), "関係"),
    "parallel_coordinates": ("平行座標。多変量の傾向を線で追う", ("dimensions",), "関係"),
    "parallel_categories": ("平行カテゴリ。区分の組み合わせの多さ", ("dimensions",), "関係"),
    "scatter3d":   ("3D散布。3つの数値の関係", ("x", "y", "z"), "関係"),
    "surface":     ("3D曲面。集計済みのクロス表を立体で", (), "関係"),
    "network":     ("ネットワーク。つながりの図", ("source", "target"), "関係"),
    # --- 指標 ---------------------------------------------------------------
    "indicator":   ("数値の大写し。KPIを1つ見せる", ("value",), "指標"),
    "gauge":       ("ゲージ。目標に対する達成度", ("value",), "指標"),
    "bullet":      ("ブレット。実績と目標を並べる", ("value",), "指標"),
}

CHART_TYPES = tuple(CHART_SPECS)


def type_help(category: str | None = None) -> str:
    """LLMに見せる一覧。分類を指定するとその分だけ返す。"""
    items = [(k, v) for k, v in CHART_SPECS.items()
             if category is None or v[2] == category]
    return " / ".join(f"{k}={v[0]}" for k, v in items)


def types_in(category: str) -> list[str]:
    return [k for k, v in CHART_SPECS.items() if v[2] == category]


def required_fields(chart_type: str) -> tuple:
    spec = CHART_SPECS.get(chart_type)
    return spec[1] if spec else ("x", "y")


def validate(item: dict, columns: list) -> list[str]:
    """指定された列が結果に存在するか検証し、問題点を返す。"""
    ct = item.get("chart_type") or "bar"
    errs = []
    if ct not in CHART_SPECS:
        return [f"未対応のグラフ種別です: {ct} / 使えるのは {', '.join(CHART_TYPES)}"]
    for f in required_fields(ct):
        v = item.get(f)
        if f in LIST_FIELDS:
            cols = list(v or [])
            if not cols:
                errs.append(f"{ct} には {f}（列名のリスト）が必要です。")
            errs += [f"{f} の列 '{c}' が結果にありません。利用可能: {columns}"
                     for c in cols if c not in columns]
        elif not v:
            errs.append(f"{ct} には {f} の指定が必要です。")
        elif v not in columns:
            errs.append(f"指定列 '{v}' が結果にありません。利用可能: {columns}")
    # 任意指定も、指定されていれば存在チェック
    for f in ("color", "size", "text", "y2", "z", "lower", "upper", "target", "facet"):
        v = item.get(f)
        if not v:
            continue
        # target だけは列名ではなく目標値（数値）で来ることがある
        if f == "target" and isinstance(v, (int, float)) and not isinstance(v, bool):
            continue
        if v not in columns:
            errs.append(f"指定列 '{v}' が結果にありません。利用可能: {columns}")
    return errs


# =============================================================================
# 下ごしらえ
# =============================================================================

def _scale(name):
    """色スケール名を色のリストに直す。

    名前のまま渡すと、周辺分布つきのグラフで plotly が文字列を1文字ずつ
    色として読み、"Blues" が 'B' 扱いになって落ちる。
    """
    return getattr(px.colors.sequential, str(name or "Blues"), None) or "Blues"


def _numeric(df: pd.DataFrame, *cols):
    for c in cols:
        if c and c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(df[c])
    return df


def _num_series(df: pd.DataFrame, col) -> pd.Series:
    """必ず数値のSeriesにする（欠損は落とさず NaN のまま）。"""
    return pd.to_numeric(df[col], errors="coerce")


class _Ctx:
    """組み立て関数に渡す、よく使う値の詰め合わせ。"""

    def __init__(self, item: dict):
        self.item = item
        self.df = pd.DataFrame(item["rows"], columns=item["columns"])
        self.x, self.y = item.get("x"), item.get("y")
        self.title = item.get("title", "")
        for f in ("color", "size", "text", "y2", "z", "lower", "upper",
                  "target", "facet", "source", "start", "end",
                  "open", "high", "low", "close", "value"):
            setattr(self, f, item.get(f) if item.get(f) in self.df.columns else None)
        # target は列名でなく数値で来ることもある（目標値）
        self.target_value = item.get("target")
        self.path = [c for c in (item.get("path") or []) if c in self.df.columns]
        self.dimensions = [c for c in (item.get("dimensions") or []) if c in self.df.columns]
        _numeric(self.df, self.y, self.size, self.y2, self.z,
                 self.lower, self.upper, self.open, self.high, self.low, self.close)

    def get(self, key, default=None):
        return self.item.get(key, default)


# =============================================================================
# 比較
# =============================================================================

def _bar(c, orientation=None, barmode=None):
    return px.bar(c.df, x=c.x, y=c.y, color=c.color, text=c.text, title=c.title,
                  barmode=barmode or c.get("barmode") or "group",
                  orientation=orientation or c.get("orientation") or "v",
                  facet_col=c.facet)


def _hbar(c):
    # 横棒は「値が大きいものを上」に。並べ替えないと読みにくい
    d = c.df.sort_values(c.y) if c.y in c.df.columns else c.df
    return px.bar(d, x=c.y, y=c.x, color=c.color, text=c.text, title=c.title,
                  orientation="h", barmode=c.get("barmode") or "group")


def _stacked_bar(c):
    return _bar(c, barmode="stack")


def _percent_bar(c):
    d = c.df.copy()
    total = d.groupby(c.x)[c.y].transform("sum")
    d["_割合"] = _num_series(d, c.y) / total.replace(0, np.nan) * 100
    fig = px.bar(d, x=c.x, y="_割合", color=c.color, title=c.title, barmode="stack",
                 text=d["_割合"].round(1).astype(str) + "%")
    fig.update_yaxes(title_text="構成比(%)", range=[0, 100])
    return fig


def _lollipop(c):
    d = c.df.sort_values(c.y)
    fig = go.Figure()
    for _, r in d.iterrows():
        fig.add_shape(type="line", x0=0, x1=r[c.y], y0=r[c.x], y1=r[c.x],
                      line=dict(color="#9DC3E6", width=2))
    fig.add_trace(go.Scatter(x=d[c.y], y=d[c.x].astype(str), mode="markers",
                             marker=dict(size=12, color="#1F4E79"), name=c.y))
    fig.update_layout(title=c.title, xaxis_title=c.y, yaxis_title=c.x)
    return fig


def _dumbbell(c):
    d = c.df
    fig = go.Figure()
    for _, r in d.iterrows():
        fig.add_shape(type="line", x0=r[c.y], x1=r[c.y2], y0=r[c.x], y1=r[c.x],
                      line=dict(color="#BFBFBF", width=3))
    fig.add_trace(go.Scatter(x=d[c.y], y=d[c.x].astype(str), mode="markers",
                             name=str(c.y), marker=dict(size=12, color="#9DC3E6")))
    fig.add_trace(go.Scatter(x=d[c.y2], y=d[c.x].astype(str), mode="markers",
                             name=str(c.y2), marker=dict(size=12, color="#1F4E79")))
    fig.update_layout(title=c.title, xaxis_title="値", yaxis_title=c.x)
    return fig


def _pareto(c):
    d = c.df.copy()
    d[c.y] = _num_series(d, c.y)
    d = d.dropna(subset=[c.y]).sort_values(c.y, ascending=False)
    total = d[c.y].sum() or 1
    d["_累積"] = d[c.y].cumsum() / total * 100
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Bar(x=d[c.x].astype(str), y=d[c.y], name=str(c.y),
                         marker_color="#2E75B6"), secondary_y=False)
    fig.add_trace(go.Scatter(x=d[c.x].astype(str), y=d["_累積"], name="累積構成比",
                             mode="lines+markers", line=dict(color="#C55A11")),
                  secondary_y=True)
    fig.add_hline(y=80, line_dash="dot", line_color="#C55A11", secondary_y=True,
                  annotation_text="80%")
    fig.update_yaxes(title_text=str(c.y), secondary_y=False)
    fig.update_yaxes(title_text="累積構成比(%)", range=[0, 105], secondary_y=True)
    fig.update_layout(title=c.title)
    return fig


def _pyramid(c):
    """人口ピラミッド。color の2種類を左右に振り分ける。"""
    d = c.df.copy()
    d[c.y] = _num_series(d, c.y)
    groups = list(pd.unique(d[c.color].dropna()))[:2]
    if len(groups) < 2:
        raise ValueError(f"人口ピラミッドには color 列に2種類の値が必要です"
                         f"（いま: {groups}）。")
    left, right = groups
    fig = go.Figure()
    dl, dr = d[d[c.color] == left], d[d[c.color] == right]
    fig.add_trace(go.Bar(y=dl[c.x].astype(str), x=-dl[c.y], name=str(left),
                         orientation="h", marker_color="#2E75B6"))
    fig.add_trace(go.Bar(y=dr[c.x].astype(str), x=dr[c.y], name=str(right),
                         orientation="h", marker_color="#F4B183"))
    fig.update_layout(title=c.title, barmode="overlay", bargap=0.1,
                      xaxis=dict(title=str(c.y),
                                 tickvals=None, ticktext=None))
    fig.update_xaxes(tickformat="~s")
    return fig


def _marimekko(c):
    """幅=size、高さ=y の積み上げ。x ごとの規模と内訳を同時に見せる。"""
    d = c.df.copy()
    d[c.y] = _num_series(d, c.y)
    d[c.size] = _num_series(d, c.size)
    widths = d.groupby(c.x, sort=False)[c.size].max()
    total_w = widths.sum() or 1
    fig = go.Figure()
    keys = list(widths.index)
    lefts, acc = {}, 0.0
    for k in keys:
        lefts[k] = acc
        acc += float(widths[k]) / total_w * 100
    groups = list(pd.unique(d[c.color].dropna())) if c.color else [None]
    for gi, g in enumerate(groups):
        sub = d if g is None else d[d[c.color] == g]
        xs, ys, ws = [], [], []
        for k in keys:
            row = sub[sub[c.x] == k]
            if row.empty:
                continue
            w = float(widths[k]) / total_w * 100
            xs.append(lefts[k] + w / 2)
            ws.append(w)
            ys.append(float(row[c.y].iloc[0]))
        fig.add_trace(go.Bar(x=xs, y=ys, width=ws, name=str(g) if g is not None else str(c.y),
                             marker_color=px.colors.qualitative.Set2[gi % 8]))
    fig.update_layout(title=c.title, barmode="stack", bargap=0,
                      xaxis_title=f"{c.x}（幅 = {c.size}）", yaxis_title=str(c.y))
    return fig


def _radar(c):
    fig = px.line_polar(c.df, r=c.y, theta=c.x, color=c.color, line_close=True,
                        title=c.title)
    fig.update_traces(fill="toself", opacity=0.5)
    return fig


def _polar_bar(c):
    return px.bar_polar(c.df, r=c.y, theta=c.x, color=c.color, title=c.title)


def _bump(c):
    """順位の推移。値が小さいほど上位なので、y軸を反転する。"""
    d = c.df.copy()
    d[c.y] = _num_series(d, c.y)
    fig = px.line(d, x=c.x, y=c.y, color=c.color, markers=True, title=c.title,
                  text=c.text)
    fig.update_traces(marker=dict(size=11))
    fig.update_yaxes(autorange="reversed", title_text=f"{c.y}（上が上位）",
                     dtick=1)
    return fig


# =============================================================================
# 推移
# =============================================================================

def _line(c):
    return px.line(c.df, x=c.x, y=c.y, color=c.color, text=c.text, title=c.title,
                   markers=True, facet_col=c.facet)


def _step(c):
    fig = px.line(c.df, x=c.x, y=c.y, color=c.color, title=c.title, markers=True)
    fig.update_traces(line_shape="hv")
    return fig


def _area(c):
    return px.area(c.df, x=c.x, y=c.y, color=c.color, title=c.title)


def _area_percent(c):
    d = c.df.copy()
    d[c.y] = _num_series(d, c.y)
    total = d.groupby(c.x)[c.y].transform("sum")
    d["_割合"] = d[c.y] / total.replace(0, np.nan) * 100
    fig = px.area(d, x=c.x, y="_割合", color=c.color, title=c.title)
    fig.update_yaxes(title_text="構成比(%)", range=[0, 100])
    return fig


def _range_area(c):
    d = c.df
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=d[c.x], y=d[c.upper], mode="lines", name="上限",
                             line=dict(width=0), showlegend=False))
    fig.add_trace(go.Scatter(x=d[c.x], y=d[c.lower], mode="lines", name="幅（95%）",
                             line=dict(width=0), fill="tonexty",
                             fillcolor="rgba(46,117,182,.18)"))
    fig.add_trace(go.Scatter(x=d[c.x], y=d[c.y], mode="lines+markers", name=str(c.y),
                             line=dict(color="#1F4E79", width=2)))
    fig.update_layout(title=c.title, xaxis_title=str(c.x), yaxis_title=str(c.y))
    return fig


def _slope(c):
    d = c.df.copy()
    d[c.y] = _num_series(d, c.y)
    fig = px.line(d, x=c.x, y=c.y, color=c.color, markers=True, title=c.title)
    fig.update_traces(line=dict(width=2))
    # 端に系列名を出す（凡例を目で追わなくて済む）
    first = str(d[c.x].iloc[0])
    for name, g in d.groupby(c.color):
        head = g[g[c.x].astype(str) == first]
        if len(head):
            fig.add_annotation(x=head[c.x].iloc[0], y=head[c.y].iloc[0], text=str(name),
                               xanchor="right", showarrow=False, xshift=-6, font_size=11)
    fig.update_layout(showlegend=False)
    return fig


def _candlestick(c):
    return go.Figure(go.Candlestick(
        x=c.df[c.x], open=c.df[c.open], high=c.df[c.high],
        low=c.df[c.low], close=c.df[c.close])).update_layout(
            title=c.title, xaxis_rangeslider_visible=False)


def _ohlc(c):
    return go.Figure(go.Ohlc(
        x=c.df[c.x], open=c.df[c.open], high=c.df[c.high],
        low=c.df[c.low], close=c.df[c.close])).update_layout(
            title=c.title, xaxis_rangeslider_visible=False)


def _gantt(c):
    fig = px.timeline(c.df, x_start=c.start, x_end=c.end, y=c.x, color=c.color,
                      text=c.text, title=c.title)
    fig.update_yaxes(autorange="reversed")     # 上から順に並べる
    return fig


def _calendar(c):
    """日付ごとの値を、週×曜日のマス目にする。"""
    d = c.df.copy()
    d[c.x] = pd.to_datetime(d[c.x], errors="coerce")
    d[c.y] = _num_series(d, c.y)
    d = d.dropna(subset=[c.x])
    if d.empty:
        raise ValueError(f"{c.x} を日付として読めませんでした。")
    d["_日付"] = d[c.x].dt.normalize()
    d = d.groupby("_日付", as_index=False)[c.y].sum().rename(columns={c.y: "_値"})
    d["_週"] = d["_日付"].dt.isocalendar().week.astype(int)
    d["_年"] = d["_日付"].dt.isocalendar().year.astype(int)
    d["_通週"] = (d["_年"] - d["_年"].min()) * 53 + d["_週"]
    names = ["月", "火", "水", "木", "金", "土", "日"]
    d["_曜日"] = d["_日付"].dt.weekday
    pivot = d.pivot_table(index="_曜日", columns="_通週", values="_値", aggfunc="sum")
    pivot = pivot.reindex(range(7))
    labels = (d.groupby("_通週")["_日付"].min().dt.strftime("%m/%d")
              .reindex(pivot.columns).tolist())
    fig = px.imshow(pivot.to_numpy(), x=labels, y=names, aspect="auto",
                    color_continuous_scale=_scale(c.get("colorscale")),
                    title=c.title, labels=dict(color=str(c.y)))
    fig.update_xaxes(title_text="週（週初の日付）", side="top")
    return fig


def _control_chart(c):
    """管理図。平均と±3σを引き、外れた点を赤くする。"""
    d = c.df.copy()
    d[c.y] = _num_series(d, c.y)
    d = d.dropna(subset=[c.y])
    m, sd = d[c.y].mean(), d[c.y].std(ddof=1)
    ucl, lcl = m + 3 * sd, m - 3 * sd
    out = (d[c.y] > ucl) | (d[c.y] < lcl)
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=d[c.x], y=d[c.y], mode="lines+markers", name=str(c.y),
                             line=dict(color="#2E75B6"),
                             marker=dict(size=8,
                                         color=np.where(out, "#B02A2A", "#2E75B6"))))
    for val, name, dash in ((m, "平均", "solid"), (ucl, "上方管理限界(+3σ)", "dash"),
                            (lcl, "下方管理限界(-3σ)", "dash")):
        fig.add_hline(y=val, line_dash=dash, line_color="#7F7F7F",
                      annotation_text=f"{name} {val:,.4g}", annotation_position="right")
    fig.update_layout(title=c.title or "管理図", xaxis_title=str(c.x),
                      yaxis_title=str(c.y))
    return fig


# =============================================================================
# 構成
# =============================================================================

def _pie(c):
    return px.pie(c.df, names=c.x, values=c.y, color=c.color, title=c.title,
                  hole=0.45 if c.get("chart_type") == "donut" else 0)


def _treemap(c):
    return px.treemap(c.df, path=c.path, values=c.y, color=c.color, title=c.title)


def _sunburst(c):
    return px.sunburst(c.df, path=c.path, values=c.y, color=c.color, title=c.title)


def _icicle(c):
    return px.icicle(c.df, path=c.path, values=c.y, color=c.color, title=c.title)


def _funnel(c):
    # plotly は x=値 / y=段階 なので入れ替える（x に段階、y に値を受け取る仕様）
    return px.funnel(c.df, x=c.y, y=c.x, color=c.color, title=c.title)


def _waterfall(c):
    d = c.df
    measure = ["relative"] * len(d)
    # 「合計」「total」で終わる行は合計として扱う
    for i, v in enumerate(d[c.x].astype(str)):
        if v.strip() in ("合計", "計", "total", "Total", "TOTAL"):
            measure[i] = "total"
    fig = go.Figure(go.Waterfall(
        x=d[c.x].astype(str), y=_num_series(d, c.y), measure=measure,
        text=d[c.y], textposition="outside"))
    fig.update_layout(title=c.title, waterfallgap=0.3)
    return fig


def _sankey(c):
    d = c.df.copy()
    d[c.y] = _num_series(d, c.y)
    labels = list(dict.fromkeys(d[c.source].astype(str).tolist()
                                + d[c.target].astype(str).tolist()))
    idx = {v: i for i, v in enumerate(labels)}
    fig = go.Figure(go.Sankey(
        node=dict(label=labels, pad=16, thickness=16,
                  line=dict(color="#BFBFBF", width=0.5)),
        link=dict(source=[idx[str(v)] for v in d[c.source]],
                  target=[idx[str(v)] for v in d[c.target]],
                  value=d[c.y].fillna(0).tolist())))
    fig.update_layout(title=c.title, font_size=12)
    return fig


# =============================================================================
# 分布
# =============================================================================

def _histogram(c):
    return px.histogram(c.df, x=c.x, color=c.color, title=c.title,
                        nbins=int(c.get("nbins")) if c.get("nbins") else None,
                        facet_col=c.facet, marginal=c.get("marginal"))


def _density(c):
    """ヒストグラム＋カーネル密度推定の曲線。"""
    from scipy import stats as sstats
    d = c.df.copy()
    d[c.x] = pd.to_numeric(d[c.x], errors="coerce")
    d = d.dropna(subset=[c.x])
    if len(d) < 3:
        raise ValueError("密度曲線には3行以上の数値が必要です。")
    fig = px.histogram(d, x=c.x, color=c.color, histnorm="probability density",
                       opacity=0.55, nbins=int(c.get("nbins") or 30), title=c.title)
    groups = [(None, d)] if not c.color else list(d.groupby(c.color))
    xs = np.linspace(d[c.x].min(), d[c.x].max(), 200)
    for name, g in groups:
        if g[c.x].nunique() < 2:
            continue
        try:
            kde = sstats.gaussian_kde(g[c.x].to_numpy())
        except np.linalg.LinAlgError:
            continue
        fig.add_trace(go.Scatter(x=xs, y=kde(xs), mode="lines",
                                 name=f"{name} 密度" if name is not None else "密度",
                                 line=dict(width=2)))
    fig.update_layout(bargap=0.02)
    return fig


def _ecdf(c):
    return px.ecdf(c.df, x=c.x, color=c.color, title=c.title, markers=False)


def _box(c):
    return px.box(c.df, x=c.x, y=c.y, color=c.color, title=c.title, points="outliers",
                  facet_col=c.facet)


def _violin(c):
    return px.violin(c.df, x=c.x, y=c.y, color=c.color, title=c.title, box=True,
                     points=False)


def _strip(c):
    return px.strip(c.df, x=c.x, y=c.y, color=c.color, title=c.title)


def _ridgeline(c):
    """群ごとの分布を少しずつずらして重ねる。"""
    d = c.df.copy()
    d[c.x] = pd.to_numeric(d[c.x], errors="coerce")
    d = d.dropna(subset=[c.x])
    fig = go.Figure()
    for name, g in d.groupby(c.color):
        fig.add_trace(go.Violin(x=g[c.x], name=str(name), side="positive",
                                width=2.2, points=False, meanline_visible=True,
                                orientation="h"))
    fig.update_layout(title=c.title, violingap=0, violinmode="overlay",
                      xaxis_title=str(c.x), showlegend=False)
    return fig


def _qq(c):
    """正規Q-Qプロット。点が直線に乗るほど正規分布に近い。"""
    from scipy import stats as sstats
    s = pd.to_numeric(c.df[c.x], errors="coerce").dropna().sort_values()
    if len(s) < 3:
        raise ValueError("Q-Qプロットには3行以上の数値が必要です。")
    theo = sstats.norm.ppf((np.arange(1, len(s) + 1) - 0.5) / len(s))
    theo = theo * s.std(ddof=1) + s.mean()
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=theo, y=s, mode="markers", name="実測",
                             marker=dict(color="#2E75B6", size=7)))
    lo, hi = float(min(theo.min(), s.min())), float(max(theo.max(), s.max()))
    fig.add_trace(go.Scatter(x=[lo, hi], y=[lo, hi], mode="lines", name="正規分布の直線",
                             line=dict(color="#C55A11", dash="dash")))
    fig.update_layout(title=c.title or f"{c.x} のQ-Qプロット",
                      xaxis_title="正規分布ならこうなる", yaxis_title="実測")
    return fig


# =============================================================================
# 関係
# =============================================================================

def _scatter(c):
    return px.scatter(c.df, x=c.x, y=c.y, color=c.color, text=c.text, title=c.title,
                      facet_col=c.facet,
                      trendline="ols" if c.get("trendline") else None)


def _bubble(c):
    return px.scatter(c.df, x=c.x, y=c.y, color=c.color, size=c.size, text=c.text,
                      title=c.title, size_max=50)


def _histogram2d(c):
    return px.density_heatmap(c.df, x=c.x, y=c.y, title=c.title,
                              nbinsx=int(c.get("nbins") or 30),
                              nbinsy=int(c.get("nbins") or 30),
                              color_continuous_scale=_scale(c.get("colorscale")),
                              marginal_x="histogram", marginal_y="histogram")


def _contour(c):
    fig = px.density_contour(c.df, x=c.x, y=c.y, color=c.color, title=c.title)
    fig.update_traces(contours_coloring="fill", contours_showlabels=True)
    return fig


def _heatmap(c):
    if c.color:
        return px.density_heatmap(c.df, x=c.x, y=c.y, z=c.color, histfunc="sum",
                                  title=c.title, text_auto=True,
                                  color_continuous_scale=_scale(c.get("colorscale")))
    return px.density_heatmap(c.df, x=c.x, y=c.y, histfunc="count", title=c.title,
                              text_auto=True,
                              color_continuous_scale=_scale(c.get("colorscale")))


def _matrix(c):
    """集計済みの表をそのまま行列として塗る。"""
    label = c.x if c.x in c.df.columns else c.df.columns[0]
    m = c.df.set_index(label)
    m = m.apply(lambda s: pd.to_numeric(s, errors="coerce")).dropna(axis=1, how="all")
    fig = px.imshow(m, text_auto=True, aspect="auto", title=c.title,
                    color_continuous_scale=_scale(c.get("colorscale")))
    fig.update_xaxes(side="top")
    return fig


def _scatter_matrix(c):
    d = c.df.copy()
    for col in c.dimensions:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    fig = px.scatter_matrix(d, dimensions=c.dimensions, color=c.color, title=c.title)
    fig.update_traces(diagonal_visible=False, showupperhalf=False,
                      marker=dict(size=4, opacity=0.6))
    return fig


def _parallel_coordinates(c):
    d = c.df.copy()
    for col in c.dimensions:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    d = d.dropna(subset=c.dimensions)
    color = c.color if (c.color and pd.api.types.is_numeric_dtype(
        pd.to_numeric(d[c.color], errors="coerce"))) else None
    if color:
        d[color] = pd.to_numeric(d[color], errors="coerce")
    return px.parallel_coordinates(d, dimensions=c.dimensions, color=color,
                                   title=c.title,
                                   color_continuous_scale=_scale(c.get("colorscale")))


def _parallel_categories(c):
    return px.parallel_categories(c.df, dimensions=c.dimensions, title=c.title,
                                  color=(pd.to_numeric(c.df[c.color], errors="coerce")
                                         if c.color else None))


def _scatter3d(c):
    return px.scatter_3d(c.df, x=c.x, y=c.y, z=c.z, color=c.color, size=c.size,
                         text=c.text, title=c.title)


def _surface(c):
    """集計済みのクロス表を立体にする（1列目が行ラベル）。"""
    label = c.x if c.x in c.df.columns else c.df.columns[0]
    m = c.df.set_index(label)
    m = m.apply(lambda s: pd.to_numeric(s, errors="coerce")).dropna(axis=1, how="all")
    fig = go.Figure(go.Surface(z=m.to_numpy(), x=list(m.columns),
                               y=[str(i) for i in m.index],
                               colorscale=_scale(c.get("colorscale"))))
    fig.update_layout(title=c.title, scene=dict(
        xaxis_title="列", yaxis_title=str(label), zaxis_title="値"))
    return fig


def _network(c):
    """つながりの図。円周上にノードを並べ、関係を線で結ぶ。"""
    d = c.df
    nodes = list(dict.fromkeys(d[c.source].astype(str).tolist()
                               + d[c.target].astype(str).tolist()))
    n = len(nodes)
    if not n:
        raise ValueError("つながりが1件もありません。")
    ang = {v: 2 * np.pi * i / n for i, v in enumerate(nodes)}
    pos = {v: (np.cos(a), np.sin(a)) for v, a in ang.items()}
    weights = _num_series(d, c.y) if c.y in d.columns else pd.Series([1] * len(d))
    wmax = float(weights.max() or 1)
    edge_x, edge_y = [], []
    for (_, r), w in zip(d.iterrows(), weights):
        x0, y0 = pos[str(r[c.source])]
        x1, y1 = pos[str(r[c.target])]
        edge_x += [x0, x1, None]
        edge_y += [y0, y1, None]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=edge_x, y=edge_y, mode="lines", hoverinfo="skip",
                             line=dict(color="rgba(120,140,170,.45)", width=1.5),
                             showlegend=False))
    deg = pd.Series(d[c.source].astype(str).tolist()
                    + d[c.target].astype(str).tolist()).value_counts()
    fig.add_trace(go.Scatter(
        x=[pos[v][0] for v in nodes], y=[pos[v][1] for v in nodes],
        mode="markers+text", text=nodes, textposition="top center",
        marker=dict(size=[12 + 26 * deg.get(v, 1) / max(deg.max(), 1) for v in nodes],
                    color="#2E75B6"),
        hovertext=[f"{v}: {deg.get(v, 0)}件" for v in nodes], hoverinfo="text",
        showlegend=False))
    fig.update_layout(title=c.title, xaxis=dict(visible=False),
                      yaxis=dict(visible=False, scaleanchor="x"),
                      plot_bgcolor="rgba(0,0,0,0)")
    if wmax:
        fig.update_layout(margin=dict(l=20, r=20, t=50, b=20))
    return fig


# =============================================================================
# 指標
# =============================================================================

def _indicator_value(c) -> float:
    s = pd.to_numeric(c.df[c.value], errors="coerce").dropna()
    if s.empty:
        raise ValueError(f"{c.value} に数値がありません。")
    mode = (c.get("agg") or "sum").lower()
    return float({"sum": s.sum, "mean": s.mean, "max": s.max,
                  "min": s.min, "last": lambda: s.iloc[-1]}.get(mode, s.sum)())


def _target_of(c, value: float):
    if c.target and c.target in c.df.columns:
        t = pd.to_numeric(c.df[c.target], errors="coerce").dropna()
        return float(t.sum()) if len(t) else None
    try:
        return float(c.target_value) if c.target_value is not None else None
    except (TypeError, ValueError):
        return None


def _indicator(c):
    v = _indicator_value(c)
    t = _target_of(c, v)
    fig = go.Figure(go.Indicator(
        mode="number+delta" if t else "number", value=v,
        number=dict(valueformat=c.get("valueformat") or ",.4~f",
                    suffix=c.get("suffix") or ""),
        delta=dict(reference=t, relative=True, valueformat=".1%") if t else None,
        title=dict(text=c.title or str(c.value))))
    return fig


def _gauge(c):
    v = _indicator_value(c)
    t = _target_of(c, v)
    top = float(c.get("max") or (max(v, t or 0) * 1.25) or 1)
    fig = go.Figure(go.Indicator(
        mode="gauge+number+delta" if t else "gauge+number", value=v,
        number=dict(valueformat=c.get("valueformat") or ",.4~f",
                    suffix=c.get("suffix") or ""),
        delta=dict(reference=t, relative=True, valueformat=".1%") if t else None,
        title=dict(text=c.title or str(c.value)),
        gauge=dict(axis=dict(range=[0, top]), bar=dict(color="#2E75B6"),
                   steps=[dict(range=[0, top * 0.5], color="#F2F6FB"),
                          dict(range=[top * 0.5, top * 0.8], color="#D9E2F3")],
                   threshold=(dict(line=dict(color="#C55A11", width=3), value=t)
                              if t else None))))
    return fig


def _bullet(c):
    v = _indicator_value(c)
    t = _target_of(c, v)
    top = float(c.get("max") or (max(v, t or 0) * 1.25) or 1)
    fig = go.Figure(go.Indicator(
        mode="number+gauge+delta" if t else "number+gauge", value=v,
        delta=dict(reference=t) if t else None,
        number=dict(valueformat=c.get("valueformat") or ",.4~f"),
        title=dict(text=c.title or str(c.value)),
        gauge=dict(shape="bullet", axis=dict(range=[0, top]),
                   bar=dict(color="#1F4E79", thickness=0.6),
                   steps=[dict(range=[0, top * 0.6], color="#F2F6FB"),
                          dict(range=[top * 0.6, top * 0.85], color="#D9E2F3")],
                   threshold=(dict(line=dict(color="#C55A11", width=3), value=t)
                              if t else None))))
    fig.update_layout(height=190)
    return fig


# =============================================================================
# 組み立ての振り分け
# =============================================================================

_BUILDERS = {
    "bar": _bar, "hbar": _hbar, "stacked_bar": _stacked_bar,
    "percent_bar": _percent_bar, "lollipop": _lollipop, "dumbbell": _dumbbell,
    "pareto": _pareto, "pyramid": _pyramid, "marimekko": _marimekko,
    "radar": _radar, "polar_bar": _polar_bar, "bump": _bump,
    "line": _line, "step": _step, "area": _area, "area_percent": _area_percent,
    "range_area": _range_area, "slope": _slope, "candlestick": _candlestick,
    "ohlc": _ohlc, "gantt": _gantt, "calendar": _calendar,
    "control_chart": _control_chart,
    "pie": _pie, "donut": _pie, "treemap": _treemap, "sunburst": _sunburst,
    "icicle": _icicle, "funnel": _funnel, "waterfall": _waterfall, "sankey": _sankey,
    "histogram": _histogram, "density": _density, "ecdf": _ecdf, "box": _box,
    "violin": _violin, "strip": _strip, "ridgeline": _ridgeline, "qq": _qq,
    "scatter": _scatter, "bubble": _bubble, "histogram2d": _histogram2d,
    "contour": _contour, "heatmap": _heatmap, "matrix": _matrix,
    "scatter_matrix": _scatter_matrix,
    "parallel_coordinates": _parallel_coordinates,
    "parallel_categories": _parallel_categories,
    "scatter3d": _scatter3d, "surface": _surface, "network": _network,
    "indicator": _indicator, "gauge": _gauge, "bullet": _bullet,
}


def build_figure(item: dict):
    """render アイテム（kind="chart"）から plotly の figure を作る。"""
    ct = item.get("chart_type", "bar")
    builder = _BUILDERS.get(ct)
    if builder is None:
        raise ValueError(f"未対応のグラフ種別です: {ct} / "
                         f"使えるのは {', '.join(CHART_TYPES)}")
    fig = builder(_Ctx(item))
    fig.update_layout(margin=dict(l=55, r=20, t=50, b=50))
    return fig


def build_dual_figure(item: dict):
    """棒(左軸)+折れ線(右軸)の2軸グラフ。"""
    df = pd.DataFrame(item["rows"], columns=item["columns"])
    x = item["x"]
    bar_y = item.get("bar_y") or []
    line_y = item.get("line_y") or []
    _numeric(df, *bar_y, *line_y)

    fig = make_subplots(specs=[[{"secondary_y": True}]])
    for col in bar_y:
        fig.add_trace(go.Bar(x=df[x], y=df[col], name=col), secondary_y=False)
    for col in line_y:
        fig.add_trace(go.Scatter(x=df[x], y=df[col], name=col, mode="lines+markers"),
                      secondary_y=True)
    fig.update_layout(title=item.get("title", ""), barmode="group",
                      legend=dict(orientation="h", yanchor="bottom", y=1.02))
    fig.update_xaxes(title_text=x)
    fig.update_yaxes(title_text=item.get("left_title") or "（左軸）", secondary_y=False)
    fig.update_yaxes(title_text=item.get("right_title") or "（右軸）", secondary_y=True)
    return fig
