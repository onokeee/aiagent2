"""業務でよく聞かれる分析。SQLでは書きにくく、pandas なら素直に書けるもの。

advanced.py が統計の道具箱なのに対して、こちらは「現場の問い」に対応する。
  期間比較    先月と比べてどうか。落ちた原因はどの区分か
  ファネル    見積 → 受注 → 請求 → 入金 のどこで落ちているか
  コホート    いつ始めた人が、どれだけ続いているか
  併売        何と何が一緒に買われているか

戻り値の形は advanced.py と同じ {"title", "tables", "notes", "meta"}。
画面もLLMも同じ入れ物で受け取れるようにしてある。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# 表の作り方・数値の丸めは統計側と揃える（同じ見た目で出す）
from advanced import AnalysisError, _clean, _df, _out, _table


def _numeric(df: pd.DataFrame, col: str) -> pd.Series:
    return pd.to_numeric(df[col], errors="coerce")


def _pct(a: float, b: float) -> float | None:
    """b に対する a の割合(%)。分母が0のときは出さない。"""
    return round(a / b * 100, 1) if b else None


def _delta_note(cur: float, prev: float, unit: str = "") -> str:
    diff = cur - prev
    rate = f"{diff / prev * 100:+.1f}%" if prev else "—"
    return f"{prev:,.4g}{unit} → {cur:,.4g}{unit}（{diff:+,.4g}{unit} / {rate}）"


# =============================================================================
# 期間比較（前月比・前年同月比）と寄与度分解
# =============================================================================

def compare_periods(columns: list, rows: list, period_col: str, value_col: str,
                    dimension_col: str | None = None,
                    current: str | None = None, previous: str | None = None,
                    qty_col: str | None = None, top: int = 15) -> dict:
    """2つの期間を比べ、差がどこから来たのかまで分解する。

    「先月と比べて売上が5%落ちた」で終わらせず、
    「どの区分が押し下げたのか」「数量が減ったのか単価が下がったのか」まで出す。
    period_col の値は文字列として比べるので、'2026-01' でも '2026年1月' でもよい。
    """
    df = _df(columns, rows)
    for c in (period_col, value_col):
        if c not in df.columns:
            raise AnalysisError(f"列が見つかりません: {c}"
                                f"（ある列: {', '.join(map(str, df.columns))}）")
    df[value_col] = _numeric(df, value_col)
    df = df.dropna(subset=[value_col])
    if df.empty:
        raise AnalysisError(f"{value_col} に数値がありません。")

    periods = [str(p) for p in sorted(df[period_col].astype(str).unique())]
    if len(periods) < 2 and not (current and previous):
        raise AnalysisError(
            f"比べるには期間が2つ以上必要です（いま {len(periods)} 個: {'、'.join(periods)}）。"
            "SQL側で2期間ぶんのデータを取ってください。")
    cur = str(current) if current else periods[-1]
    prev = str(previous) if previous else periods[-2]
    for p in (cur, prev):
        if p not in periods:
            raise AnalysisError(f"期間 '{p}' がデータにありません（ある期間: {'、'.join(periods)}）。")

    df["_p"] = df[period_col].astype(str)
    cur_df = df[df["_p"] == cur]
    prev_df = df[df["_p"] == prev]
    cur_total = float(cur_df[value_col].sum())
    prev_total = float(prev_df[value_col].sum())
    diff_total = cur_total - prev_total

    tables = [_table("全体", ["項目", prev, cur, "差分", "増減率(%)"],
                     [[value_col, round(prev_total, 4), round(cur_total, 4),
                       round(diff_total, 4), _pct(diff_total, abs(prev_total))]])]
    notes = [f"{value_col}: {_delta_note(cur_total, prev_total)}"]

    meta = {"current": cur, "previous": prev,
            "current_total": _clean(cur_total), "previous_total": _clean(prev_total)}

    if dimension_col and dimension_col in df.columns:
        a = prev_df.groupby(dimension_col)[value_col].sum()
        b = cur_df.groupby(dimension_col)[value_col].sum()
        seg = pd.DataFrame({prev: a, cur: b}).fillna(0.0)
        seg["差分"] = seg[cur] - seg[prev]
        seg["増減率(%)"] = np.where(seg[prev] != 0,
                                   (seg["差分"] / seg[prev].abs() * 100).round(1), np.nan)
        # 寄与度 = その区分の差分が、全体の差分のうち何割を占めるか
        seg["寄与度(%)"] = (seg["差分"] / abs(diff_total) * 100).round(1) if diff_total else np.nan
        seg = seg.sort_values("差分")
        show = pd.concat([seg.head(top), seg.tail(top)]).drop_duplicates()
        show = show.sort_values("差分", ascending=False).reset_index()
        cols, rws = _out(show.round(4))
        tables.append(_table(f"{dimension_col}別の内訳（増減の大きい順）", cols, rws))

        down = seg[seg["差分"] < 0].head(3)
        up = seg[seg["差分"] > 0].tail(3).iloc[::-1]
        if len(down):
            notes.append("押し下げた区分: " + "、".join(
                f"{i}（{r['差分']:+,.4g}"
                + (f" / 全体の変化の{abs(r['寄与度(%)']):.0f}%" if diff_total else "") + "）"
                for i, r in down.iterrows()))
        if len(up):
            notes.append("押し上げた区分: " + "、".join(
                f"{i}（{r['差分']:+,.4g}"
                + (f" / 全体の変化の{abs(r['寄与度(%)']):.0f}%" if diff_total else "") + "）"
                for i, r in up.iterrows()))
        # 新しく出てきた・消えた区分は、増減率だけ見ていると見落とす
        gone = [str(i) for i in seg.index[(seg[prev] > 0) & (seg[cur] == 0)]][:5]
        born = [str(i) for i in seg.index[(seg[prev] == 0) & (seg[cur] > 0)]][:5]
        if gone:
            notes.append(f"{cur} で無くなった{dimension_col}: {'、'.join(gone)}")
        if born:
            notes.append(f"{cur} で新たに出た{dimension_col}: {'、'.join(born)}")

    if qty_col and qty_col in df.columns:
        # 金額の変化を「数量が動いたぶん」と「単価が動いたぶん」に割る
        df[qty_col] = _numeric(df, qty_col)
        q0 = float(prev_df[qty_col].sum())
        q1 = float(cur_df[qty_col].sum())
        p0 = prev_total / q0 if q0 else 0.0
        p1 = cur_total / q1 if q1 else 0.0
        vol = (q1 - q0) * p0                      # 数量要因（単価は前期のまま）
        price = (p1 - p0) * q1                    # 単価要因（数量は当期）
        tables.append(_table("増減の要因分解", ["要因", "金額", "全体の変化に占める割合(%)"],
                             [["数量が変わったぶん", round(vol, 4), _pct(vol, abs(diff_total))],
                              ["単価が変わったぶん", round(price, 4), _pct(price, abs(diff_total))],
                              ["合計", round(vol + price, 4), None]]))
        notes.append(f"数量 {q0:,.4g} → {q1:,.4g}、平均単価 {p0:,.4g} → {p1:,.4g}。"
                     f"変化の内訳は数量 {vol:+,.4g}、単価 {price:+,.4g}。"
                     + ("数量の影響が大きいです。" if abs(vol) > abs(price) else
                        "単価の影響が大きいです。"))
        meta.update({"volume_effect": _clean(vol), "price_effect": _clean(price)})

    notes.append(f"比較した期間: {prev} と {cur}。"
                 "期間の長さや営業日数が違うと単純比較はできません。"
                 "日数が違う場合は1日あたりに直して比べてください。")
    return {"title": f"{value_col} の期間比較（{prev} → {cur}）",
            "tables": tables, "notes": notes, "meta": meta}


# =============================================================================
# ファネル（段階ごとの通過と滞留）
# =============================================================================

def _passed(s: pd.Series) -> pd.Series:
    """その段階を通過したか。日付なら「入っていれば通過」、数値なら0より大。"""
    num = pd.to_numeric(s, errors="coerce")
    if num.notna().mean() >= 0.8:
        return num.fillna(0) > 0
    return s.notna() & (s.astype(str).str.strip() != "")


def funnel_analysis(columns: list, rows: list, steps: list,
                    labels: list | None = None, group_col: str | None = None,
                    date_steps: bool = True) -> dict:
    """段階ごとの通過数・転換率・離脱と、段階間の滞留日数を出す。

    1行 = 1案件。steps には段階を表す列を順に並べる
    （例: 見積日, 受注日, 請求日, 入金日）。値が入っていればその段階を通過した扱い。
    """
    df = _df(columns, rows)
    steps = [s for s in (steps or []) if s]
    if len(steps) < 2:
        raise AnalysisError("steps に段階の列を2つ以上、順番に並べて指定してください。")
    missing = [s for s in steps if s not in df.columns]
    if missing:
        raise AnalysisError(f"列が見つかりません: {', '.join(missing)}"
                            f"（ある列: {', '.join(map(str, df.columns))}）")
    names = list(labels or []) + steps[len(labels or []):]

    flags = pd.DataFrame({s: _passed(df[s]) for s in steps})
    total = len(df)
    counts = [int(flags[s].sum()) for s in steps]

    frows = []
    for i, s in enumerate(steps):
        prev = counts[i - 1] if i else counts[0]
        frows.append([names[i], counts[i],
                      _pct(counts[i], counts[0]),
                      _pct(counts[i], prev) if i else None,
                      (prev - counts[i]) if i else 0])
    tables = [_table("段階ごとの通過",
                     ["段階", "件数", "最初からの通過率(%)", "直前からの転換率(%)", "離脱数"],
                     frows)]

    notes = [f"対象 {total:,} 件。{names[0]} {counts[0]:,} 件から "
             f"{names[-1]} {counts[-1]:,} 件まで、"
             f"通過率は {_pct(counts[-1], counts[0])}% です。"]
    # いちばん漏れている段階を名指しする。ここが改善の的になる
    drops = [(counts[i - 1] - counts[i], i) for i in range(1, len(steps))]
    if drops:
        worst = max(drops)
        if worst[0] > 0:
            i = worst[1]
            notes.append(f"最も落ちているのは {names[i - 1]} → {names[i]} で、"
                         f"{worst[0]:,} 件（{100 - (_pct(counts[i], counts[i - 1]) or 0):.1f}%）が"
                         "先へ進んでいません。")

    # 段階間の滞留日数。日付として読めるときだけ出す
    if date_steps:
        lag_rows = []
        for i in range(1, len(steps)):
            a = pd.to_datetime(df[steps[i - 1]], errors="coerce")
            b = pd.to_datetime(df[steps[i]], errors="coerce")
            days = (b - a).dt.total_seconds() / 86400
            days = days[days.notna() & (days >= 0)]
            if len(days) >= 3:
                lag_rows.append([f"{names[i - 1]} → {names[i]}", len(days),
                                 round(float(days.mean()), 1),
                                 round(float(days.median()), 1),
                                 round(float(days.quantile(0.9)), 1)])
        if lag_rows:
            tables.append(_table("段階間の日数", ["区間", "件数", "平均", "中央値", "90%点"],
                                 lag_rows))
            slow = max(lag_rows, key=lambda r: r[3])
            notes.append(f"最も時間がかかるのは {slow[0]} で、中央値 {slow[3]} 日"
                         f"（1割は {slow[4]} 日以上）。")

    if group_col and group_col in df.columns:
        grows = []
        for name, sub in df.groupby(group_col):
            f = pd.DataFrame({s: _passed(sub[s]) for s in steps})
            first, last = int(f[steps[0]].sum()), int(f[steps[-1]].sum())
            grows.append([str(name), first, last, _pct(last, first)])
        grows.sort(key=lambda r: (r[3] is None, r[3]))
        tables.append(_table(f"{group_col}別の通過率",
                             [group_col, names[0], names[-1], "通過率(%)"], grows))
        if len(grows) >= 2 and grows[0][3] is not None and grows[-1][3] is not None:
            notes.append(f"通過率が最も低いのは {grows[0][0]}（{grows[0][3]}%）、"
                         f"最も高いのは {grows[-1][0]}（{grows[-1][3]}%）。")

    return {"title": "ファネル分析", "tables": tables, "notes": notes,
            "meta": {"steps": names, "counts": counts, "total": total}}


# =============================================================================
# コホート（いつ始めた人が、どれだけ続いているか）
# =============================================================================

def cohort_analysis(columns: list, rows: list, id_col: str, period_col: str,
                    value_col: str | None = None, max_periods: int = 12) -> dict:
    """初回の期でグループ分けし、その後どれだけ残っているかを見る。

    期の並びはデータに出てくる値の昇順で決める。'2026-01' でも '第1四半期' でも、
    並べたときに正しい順になっていれば動く。
    """
    df = _df(columns, rows)
    for c in (id_col, period_col):
        if c not in df.columns:
            raise AnalysisError(f"列が見つかりません: {c}"
                                f"（ある列: {', '.join(map(str, df.columns))}）")
    df = df[df[id_col].notna() & df[period_col].notna()].copy()
    if df.empty:
        raise AnalysisError("対象データがありません。")
    df["_p"] = df[period_col].astype(str)

    order = {p: i for i, p in enumerate(sorted(df["_p"].unique()))}
    if len(order) < 2:
        raise AnalysisError(f"期が1つしかありません（{list(order)}）。"
                            "複数の期にまたがるデータを取ってください。")
    df["_i"] = df["_p"].map(order)
    first = df.groupby(id_col)["_i"].min().rename("_c")
    df = df.join(first, on=id_col)
    df["経過"] = df["_i"] - df["_c"]
    max_periods = max(1, min(int(max_periods or 12), len(order)))
    df = df[df["経過"] < max_periods]

    rev = {i: p for p, i in order.items()}
    people = df.pivot_table(index="_c", columns="経過", values=id_col,
                            aggfunc="nunique", fill_value=0)
    size = people[0] if 0 in people.columns else people.max(axis=1)

    keep = people.div(size, axis=0).mul(100).round(1)
    keep.index = [f"{rev[i]}（{int(size[i])}人)" for i in keep.index]
    keep.columns = [f"+{c}期" for c in keep.columns]
    k = keep.reset_index().rename(columns={"index": "コホート", "_c": "コホート"})
    cols, rws = _out(k)
    tables = [_table("継続率(%)", cols, rws)]

    p = people.copy()
    p.index = [f"{rev[i]}" for i in p.index]
    p.columns = [f"+{c}期" for c in p.columns]
    c2, r2 = _out(p.reset_index().rename(columns={"index": "コホート", "_c": "コホート"}))
    tables.append(_table("人数", c2, r2))

    notes = []
    if len(keep.columns) > 1:
        avg = keep.iloc[:, 1].mean()
        notes.append(f"初回の次の期に残っているのは平均 {avg:.1f}% です。")
    if len(keep.columns) > 3:
        avg3 = keep.iloc[:, 3].mean()
        notes.append(f"3期あとに残っているのは平均 {avg3:.1f}%。"
                     + ("落ち方が急なので、初期の定着に手を打つ余地があります。"
                        if avg3 < 30 else "比較的よく定着しています。"))
    # 新しいコホートほど良くなっているか（施策の効果が出ているか）
    if len(keep) >= 3 and len(keep.columns) > 1:
        early, late = keep.iloc[0, 1], keep.iloc[-1, 1]
        if abs(early - late) >= 5:
            notes.append(f"最初のコホート {early:.1f}% に対し、直近は {late:.1f}%。"
                         + ("改善しています。" if late > early else
                            "悪化しています。獲得の質か初期対応を確かめてください。"))

    if value_col and value_col in df.columns:
        df[value_col] = _numeric(df, value_col)
        amt = df.pivot_table(index="_c", columns="経過", values=value_col,
                             aggfunc="sum", fill_value=0).round(2)
        amt.index = [f"{rev[i]}" for i in amt.index]
        amt.columns = [f"+{c}期" for c in amt.columns]
        c3, r3 = _out(amt.reset_index().rename(columns={"index": "コホート", "_c": "コホート"}))
        tables.append(_table(f"{value_col}の合計", c3, r3))
        per = df.groupby("_c")[value_col].sum() / size
        notes.append("1人あたりの累計 " + value_col + ": " + "、".join(
            f"{rev[i]} {v:,.4g}" for i, v in per.items()))

    notes.append("直近のコホートは経過期間が短いぶん、右側のマスが空きます。"
                 "同じ経過期数どうし（縦ではなく列で）比べてください。")
    return {"title": "コホート分析（継続率）", "tables": tables, "notes": notes,
            "meta": {"cohorts": len(keep), "periods": len(keep.columns)}}


# =============================================================================
# 併売（何と何が一緒に買われているか）
# =============================================================================

def market_basket(columns: list, rows: list, transaction_col: str, item_col: str,
                  min_support: float = 1.0, top: int = 25,
                  max_items: int = 60) -> dict:
    """同じ伝票に一緒に入っている品目の組み合わせを見つける。

    リフト値は「たまたま一緒になる確率」に対して何倍かを表す。
    1.0 を大きく超える組み合わせが、置き場所や提案の手がかりになる。
    """
    df = _df(columns, rows)
    for c in (transaction_col, item_col):
        if c not in df.columns:
            raise AnalysisError(f"列が見つかりません: {c}"
                                f"（ある列: {', '.join(map(str, df.columns))}）")
    d = df[[transaction_col, item_col]].dropna().astype(str).drop_duplicates()
    n_tx = d[transaction_col].nunique()
    if n_tx < 10:
        raise AnalysisError(f"伝票が {n_tx} 件しかありません。10件以上必要です。")

    freq = d[item_col].value_counts()
    # 組み合わせの数は品目数の2乗で増える。よく出るものだけに絞って現実的な時間に収める
    keep = list(freq.head(max(2, int(max_items))).index)
    d = d[d[item_col].isin(keep)]
    cut = len(freq) - len(keep)

    baskets = d.groupby(transaction_col)[item_col].apply(set)
    baskets = baskets[baskets.map(len) >= 2]
    if baskets.empty:
        raise AnalysisError("2品目以上入っている伝票がありません。"
                            "1伝票1明細のデータになっていないか確認してください。")

    pair_count: dict = {}
    for items in baskets:
        picked = sorted(items)
        for i, a in enumerate(picked):
            for b in picked[i + 1:]:
                pair_count[(a, b)] = pair_count.get((a, b), 0) + 1

    out = []
    for (a, b), c in pair_count.items():
        support = c / n_tx * 100
        if support < float(min_support or 0):
            continue
        ca, cb = int(freq[a]), int(freq[b])
        conf_ab = c / ca * 100 if ca else 0.0
        conf_ba = c / cb * 100 if cb else 0.0
        lift = (c / n_tx) / ((ca / n_tx) * (cb / n_tx)) if ca and cb else 0.0
        out.append([a, b, c, round(support, 2), round(conf_ab, 1), round(conf_ba, 1),
                    round(lift, 2)])
    if not out:
        raise AnalysisError(f"支持度 {min_support}% 以上の組み合わせがありませんでした。"
                            "min_support を下げてください。")
    out.sort(key=lambda r: r[6], reverse=True)
    shown = out[: max(1, int(top))]

    tables = [_table("よく一緒に買われる組み合わせ",
                     ["品目A", "品目B", "同時件数", "支持度(%)",
                      "AならBも(%)", "BならAも(%)", "リフト"], shown)]
    tables.append(_table("よく出る品目", ["品目", "伝票数", "出現率(%)"],
                        [[i, int(c), round(c / n_tx * 100, 1)]
                         for i, c in freq.head(15).items()]))

    notes = [f"対象 {n_tx:,} 伝票、うち2品目以上入っているのは {len(baskets):,} 伝票です。"]
    if cut > 0:
        notes.append(f"品目が多いため、出現の多い上位 {len(keep)} 品目に絞って計算しました"
                     f"（{cut} 品目を除外）。")
    best = shown[0]
    notes.append(f"最も結びつきが強いのは「{best[0]}」と「{best[1]}」で、リフト {best[6]}倍。"
                 f"{best[0]}を買った人の {best[4]}% が{best[1]}も買っています。")
    notes.append("リフトは「たまたま一緒になる確率」に対する倍率です。1.0前後なら関係なし、"
                 "2.0を超えると強い結びつきと見ます。ただし件数が少ない組は偶然でも"
                 "大きな値になるので、同時件数も併せて見てください。")
    return {"title": "併売分析", "tables": tables, "notes": notes,
            "meta": {"transactions": int(n_tx), "pairs": len(out)}}
