"""SQLiteだけでは書けない集計・統計を pandas で行う。

このアプリのSQLite(3.32)には次が無い:
  STDDEV / VARIANCE / MEDIAN / CORR / PERCENTILE / SQRT / POWER / PIVOT構文
そのため「相関」「中央値」「ばらつき」「クロス集計」はSQLでは実質書けない。
ここではSELECT結果(columns, rows)を受け取り、同じ形(columns, rows)で返す。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

AGG_FUNCS = {
    "sum": ("合計", "sum"),
    "mean": ("平均", "mean"),
    "count": ("件数", "count"),
    "median": ("中央値", "median"),
    "min": ("最小", "min"),
    "max": ("最大", "max"),
    "std": ("標準偏差", "std"),
    "nunique": ("種類数", "nunique"),
}
CORR_METHODS = ("pearson", "spearman")
OUTLIER_METHODS = ("iqr", "zscore")
MARGIN_NAME = "合計"


def _df(columns: list, rows: list) -> pd.DataFrame:
    return pd.DataFrame([list(r) for r in rows], columns=list(columns))


def _to_numeric(df: pd.DataFrame, cols) -> pd.DataFrame:
    for c in cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def numeric_columns(columns: list, rows: list) -> list:
    """数値として扱える列を推定する（8割以上が数値なら数値列とみなす）。"""
    df = _df(columns, rows)
    out = []
    for c in df.columns:
        s = pd.to_numeric(df[c], errors="coerce")
        if len(s) and s.notna().mean() >= 0.8:
            out.append(c)
    return out


def _clean(v):
    """JSONに載せられる形へ（NaN/Infとnumpy型を素の値にする）。"""
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating, float)):
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else round(f, 6)
    if isinstance(v, (np.bool_,)):
        return bool(v)
    return v


def _out(df: pd.DataFrame):
    """DataFrame を (columns, rows) に戻す。"""
    cols = [str(c) for c in df.columns]
    rows = [tuple(_clean(v) for v in r) for r in df.itertuples(index=False, name=None)]
    return cols, rows


# --- クロス集計 -----------------------------------------------------------------

def _sorted_by(pt: pd.DataFrame, rank_by: str) -> pd.DataFrame:
    """大きい順に並べ替える。rank_by が列名ならその列、それ以外は行の合計で。"""
    target = None
    for c in pt.columns:
        if str(c) == str(rank_by):
            target = c
            break
    key = pt[target] if target is not None else pt.sum(axis=1, numeric_only=True)
    return pt.loc[key.sort_values(ascending=False).index]


def _as_percent(pt: pd.DataFrame, mode: str) -> pd.DataFrame:
    """実数を構成比(%)に置き換える。合計が0の行や列は0のままにする。"""
    num = pt.select_dtypes("number")
    if mode == "row":
        denom = num.sum(axis=1).replace(0, np.nan)
        out = num.div(denom, axis=0)
    elif mode == "column":
        denom = num.sum(axis=0).replace(0, np.nan)
        out = num.div(denom, axis=1)
    else:
        total = num.to_numpy().sum()
        out = num / (total if total else np.nan)
    pt = pt.copy()
    pt[num.columns] = (out * 100).round(1).fillna(0.0)
    return pt


#: 構成比の取り方。クロス集計は「実数で見たい」より「割合で見たい」ことが多い。
PERCENT_MODES = {
    "row": "行内の構成比（行ごとに合計100%）",
    "column": "列内の構成比（列ごとに合計100%）",
    "total": "全体に対する構成比（表全体で100%）",
}


def pivot(columns: list, rows: list, index: list, cols: str | None, values: str,
          aggfunc: str = "sum", fill_value=0, margins: bool = False,
          percent: str | None = None, rank_by: str | None = None):
    """クロス集計表を作る。SQLiteにPIVOT構文が無いのでここで行う。

    index   : 行にする列（複数可）
    cols    : 列に展開する列（省略可。省略時は index ごとの集計表になる）
    values  : 集計する値の列
    percent : row / column / total を指定すると実数を構成比(%)に置き換える
    rank_by : 指定した列（または合計）の大きい順に並べ、順位の列を先頭に足す
    """
    if not index:
        raise ValueError("index（行にする列）を1つ以上指定してください。")
    if not values:
        raise ValueError("values（集計する値の列）を指定してください。")
    if aggfunc not in AGG_FUNCS:
        raise ValueError(f"aggfunc は {', '.join(AGG_FUNCS)} のいずれかです。")
    if percent and percent not in PERCENT_MODES:
        raise ValueError(f"percent は {', '.join(PERCENT_MODES)} のいずれかです。")

    df = _df(columns, rows)
    missing = [c for c in list(index) + ([cols] if cols else []) + [values]
               if c not in df.columns]
    if missing:
        raise ValueError(f"指定列が結果にありません: {missing} / 利用可能: {list(df.columns)}")

    if aggfunc not in ("count", "nunique"):
        _to_numeric(df, [values])

    pt = pd.pivot_table(
        df, index=list(index), columns=cols, values=values,
        aggfunc=AGG_FUNCS[aggfunc][1],
        fill_value=fill_value, margins=margins and not percent,
        margins_name=MARGIN_NAME, dropna=False, observed=False,
    )
    if isinstance(pt, pd.Series):
        pt = pt.to_frame(name=values)

    # 並べ替えは構成比にする前に行う（%にすると行内の大小が消えることがある）
    if rank_by:
        pt = _sorted_by(pt, rank_by)
    if percent:
        pt = _as_percent(pt, percent)

    pt = pt.reset_index()
    if rank_by:
        pt.insert(0, "順位", range(1, len(pt) + 1))
    # 列がMultiIndex（valuesとcolsの2段）になる場合があるので平坦化する
    flat = []
    for c in pt.columns:
        if isinstance(c, tuple):
            parts = [str(p) for p in c if str(p) != ""]
            flat.append(" / ".join(parts) if parts else values)
        else:
            flat.append(str(c))
    pt.columns = flat
    return _out(pt)


# --- 基本統計量 -----------------------------------------------------------------

_DESC_LABELS = {"count": "件数", "mean": "平均", "std": "標準偏差", "min": "最小",
                "25%": "25%", "50%": "中央値", "75%": "75%", "max": "最大"}


def describe(columns: list, rows: list, targets: list | None = None,
             group_by: str | None = None):
    """基本統計量（件数/平均/標準偏差/最小/四分位/中央値/最大）。"""
    df = _df(columns, rows)
    targets = list(targets or []) or numeric_columns(columns, rows)
    targets = [c for c in targets if c in df.columns and c != group_by]
    if not targets:
        raise ValueError("数値として集計できる列がありません。columns で対象列を指定してください。")
    _to_numeric(df, targets)

    if group_by:
        if group_by not in df.columns:
            raise ValueError(f"group_by の列 '{group_by}' が結果にありません。")
        out = []
        for key, g in df.groupby(group_by, dropna=False):
            d = g[targets].describe().T.reset_index().rename(columns={"index": "列"})
            d.insert(0, group_by, key)
            out.append(d)
        res = pd.concat(out, ignore_index=True)
    else:
        res = df[targets].describe().T.reset_index().rename(columns={"index": "列"})

    res = res.rename(columns=_DESC_LABELS)
    for c in res.columns:
        if c not in ("列", group_by):
            res[c] = pd.to_numeric(res[c], errors="coerce").round(3)
    return _out(res)


# --- 相関 -----------------------------------------------------------------------

def correlation(columns: list, rows: list, targets: list | None = None,
                method: str = "pearson"):
    """数値列どうしの相関行列。1列目が列名なので、そのままヒートマップにできる。"""
    if method not in CORR_METHODS:
        raise ValueError(f"method は {', '.join(CORR_METHODS)} のいずれかです。")
    df = _df(columns, rows)
    targets = list(targets or []) or numeric_columns(columns, rows)
    targets = [c for c in targets if c in df.columns]
    if len(targets) < 2:
        raise ValueError("相関には数値列が2つ以上必要です。columns で対象列を指定してください。")
    _to_numeric(df, targets)
    corr = df[targets].corr(method=method).round(3).reset_index()
    corr = corr.rename(columns={"index": "列"})
    return _out(corr)


def correlation_pairs(columns: list, rows: list, method: str = "pearson"):
    """相関の強い組み合わせを、強さ順のリストで返す（LLMへの説明用）。"""
    cols, mrows = correlation(columns, rows, None, method)
    names = cols[1:]
    pairs = []
    for i, r in enumerate(mrows):
        for j, v in enumerate(r[1:]):
            if j > i and v is not None:
                pairs.append({"a": names[i], "b": names[j], "corr": v})
    pairs.sort(key=lambda p: abs(p["corr"]), reverse=True)
    return pairs


# --- 外れ値 ---------------------------------------------------------------------

def outliers(columns: list, rows: list, target: str, method: str = "iqr",
             threshold: float = 1.5, limit: int = 200):
    """外れ値の行を抜き出す。戻り値: (columns, rows, 判定に使った情報)"""
    if method not in OUTLIER_METHODS:
        raise ValueError(f"method は {', '.join(OUTLIER_METHODS)} のいずれかです。")
    df = _df(columns, rows)
    if target not in df.columns:
        raise ValueError(f"target の列 '{target}' が結果にありません。利用可能: {list(df.columns)}")
    _to_numeric(df, [target])
    s = df[target].dropna()
    if s.empty:
        raise ValueError(f"'{target}' に数値がありません。")

    if method == "iqr":
        q1, q3 = float(s.quantile(0.25)), float(s.quantile(0.75))
        iqr = q3 - q1
        lo, hi = q1 - threshold * iqr, q3 + threshold * iqr
        info = {"方式": f"IQR×{threshold}", "Q1": round(q1, 3), "Q3": round(q3, 3),
                "下限": round(lo, 3), "上限": round(hi, 3)}
        mask = (df[target] < lo) | (df[target] > hi)
    else:
        mu, sd = float(s.mean()), float(s.std(ddof=0))
        lo, hi = mu - threshold * sd, mu + threshold * sd
        info = {"方式": f"Zスコア±{threshold}", "平均": round(mu, 3),
                "標準偏差": round(sd, 3), "下限": round(lo, 3), "上限": round(hi, 3)}
        mask = (df[target] < lo) | (df[target] > hi) if sd > 0 else pd.Series(False, index=df.index)

    hit = df[mask.fillna(False)].copy()
    info["全体件数"] = int(len(df))
    info["外れ値件数"] = int(len(hit))
    info["割合(%)"] = round(100.0 * len(hit) / len(df), 2) if len(df) else 0.0
    hit = hit.sort_values(target, ascending=False).head(limit)
    c, r = _out(hit)
    return c, r, info
