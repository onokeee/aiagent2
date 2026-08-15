"""業務でよく聞かれる分析のツール。

business.py（期間比較・ファネル・コホート・併売）と
advanced.py（異常検知・生存時間）をツールとして公開する。
データ品質チェックだけは、SQLを受け取るのではなくDBそのものを見に行くので
ここに実処理を置く。
"""
from __future__ import annotations

import advanced
import business
import catalog
import db
from .common import _analysis_tool, _err, _report_result

_compare_periods = _analysis_tool(lambda a, c, r: business.compare_periods(
    c, r, a.get("period_col"), a.get("value_col"),
    dimension_col=a.get("dimension_col"), current=a.get("current"),
    previous=a.get("previous"), qty_col=a.get("qty_col")))


_funnel_analysis = _analysis_tool(lambda a, c, r: business.funnel_analysis(
    c, r, a.get("steps") or [], labels=a.get("labels"),
    group_col=a.get("group_col")))


_cohort_analysis = _analysis_tool(lambda a, c, r: business.cohort_analysis(
    c, r, a.get("id_col"), a.get("period_col"), value_col=a.get("value_col"),
    max_periods=int(a.get("max_periods") or 12)))


_market_basket = _analysis_tool(lambda a, c, r: business.market_basket(
    c, r, a.get("transaction_col"), a.get("item_col"),
    min_support=float(a.get("min_support") or 1.0), top=int(a.get("top") or 25)))


_detect_anomalies = _analysis_tool(lambda a, c, r: advanced.detect_anomalies(
    c, r, a.get("time_col"), a.get("value_col"),
    window=int(a.get("window") or 7), threshold=float(a.get("threshold") or 3.0),
    season_length=int(a["season_length"]) if a.get("season_length") else None,
    changepoints=a.get("changepoints", True)))


_survival_analysis = _analysis_tool(lambda a, c, r: advanced.survival_analysis(
    c, r, a.get("duration_col"), event_col=a.get("event_col"),
    group_col=a.get("group_col")))


# =============================================================================
# データ品質チェック（DBを直接見る）
# =============================================================================

#: 1回のチェックで見るテーブルの上限。全部見ると時間がかかりすぎる。
_MAX_TABLES = 12
#: これより大きいテーブルでは、重い集計（種類数）を省く。
_HEAVY_ROWS = 200_000


def _q(alias: str, table: str) -> str:
    return f'{alias}."{table}"'


def _column_stats(scope: list[dict], alias: str, table: str, cols: list[dict],
                  rowcount: int | None) -> tuple:
    """1テーブルぶんの欠損・種類数を1本のSQLで数える。"""
    heavy = (rowcount or 0) <= _HEAVY_ROWS
    parts = ["COUNT(*) AS n"]
    for i, c in enumerate(cols):
        name = c["name"].replace('"', '""')
        parts.append(f'SUM(CASE WHEN "{name}" IS NULL THEN 1 ELSE 0 END) AS nul{i}')
        if heavy:
            parts.append(f'COUNT(DISTINCT "{name}") AS uni{i}')
        if str(c.get("type") or "").upper().startswith(("TEXT", "VARCHAR", "CHAR")):
            parts.append(f"SUM(CASE WHEN TRIM(\"{name}\") = '' THEN 1 ELSE 0 END) AS emp{i}")
        else:
            parts.append(f"0 AS emp{i}")
    sql = f"SELECT {', '.join(parts)} FROM {_q(alias, table)}"
    _, rows, _ = db.run_select(sql, scope, max_rows=1)
    got = rows[0]
    n = int(got[0] or 0)
    out, pos = [], 1
    for c in cols:
        nul = int(got[pos] or 0)
        pos += 1
        uni = int(got[pos] or 0) if heavy else None
        pos += 1 if heavy else 0
        emp = int(got[pos] or 0)
        pos += 1
        out.append({"column": c["name"], "type": c.get("type") or "",
                    "nulls": nul, "empty": emp, "unique": uni})
    return n, out


def _pk_duplicates(scope: list[dict], alias: str, table: str, pk: list) -> int | None:
    """主キーが重複している組み合わせの数。"""
    if not pk:
        return None
    keys = ", ".join(f'"{c}"' for c in pk)
    sql = (f"SELECT COUNT(*) FROM (SELECT {keys} FROM {_q(alias, table)} "
           f"GROUP BY {keys} HAVING COUNT(*) > 1)")
    try:
        _, rows, _ = db.run_select(sql, scope, max_rows=1)
        return int(rows[0][0] or 0)
    except Exception:
        return None


def _orphans(scope: list[dict], child: tuple, parent: tuple) -> int | None:
    """親に居ない子（孤立した外部キー）の件数。"""
    ca, ct, cc = child
    pa, pt, pc = parent
    sql = (f'SELECT COUNT(*) FROM {_q(ca, ct)} c WHERE c."{cc}" IS NOT NULL '
           f'AND NOT EXISTS (SELECT 1 FROM {_q(pa, pt)} p WHERE p."{pc}" = c."{cc}")')
    try:
        _, rows, _ = db.run_select(sql, scope, max_rows=1)
        return int(rows[0][0] or 0)
    except Exception:
        return None


def _date_range(scope: list[dict], alias: str, table: str, col: str) -> tuple | None:
    """日付列の最小・最大。データがいつまで入っているかを見る。"""
    try:
        _, rows, _ = db.run_select(
            f'SELECT MIN("{col}"), MAX("{col}") FROM {_q(alias, table)}',
            scope, max_rows=1)
        return rows[0][0], rows[0][1]
    except Exception:
        return None


def _looks_like_date(col: dict, sample: str = "") -> bool:
    name = str(col.get("name") or "").lower()
    return (str(col.get("type") or "").upper().startswith("DATE")
            or any(k in name for k in ("date", "日", "_at", "time", "月")))


def _data_quality(args: dict, scope: list[dict]) -> dict:
    """選択中のDBを見て、分析の前に気づいておくべき異常を洗い出す。"""
    if not scope:
        return _err("対象のDBがありません。")

    # テーブル名は 'stocks' でも 'demo_inventory.stocks' でもよい
    # （プロンプトが『DB名.テーブル名』で書くよう求めているので、後者で来ることが多い）
    want = [str(t) for t in (args.get("tables") or [])]
    def _wanted(alias, tname):
        return (not want) or tname in want or f"{alias}.{tname}" in want
    issues, tbl_rows, col_rows, ref_rows = [], [], [], []
    checked = 0

    # 結合定義はDBをまたぐので、スコープ全体で一度だけ組み立てる
    try:
        entries = [{"alias": e["alias"], "profile": catalog.profile_db(e["path"]),
                    "meta": e.get("meta") or catalog.load_meta(e["path"])} for e in scope]
        edges = catalog.collect_edges(entries)
    except Exception:
        edges = []

    for s in scope:
        alias = s["alias"]
        try:
            profile = catalog.profile_db(s["path"])
        except Exception as e:
            issues.append(("高", f"{alias}: プロファイルを読めませんでした（{e}）"))
            continue
        meta = s.get("meta") or catalog.load_meta(s["path"])
        allowed = set(s.get("tables") or profile.get("tables") or {})

        for tname, t in (profile.get("tables") or {}).items():
            if tname not in allowed:
                continue
            if not _wanted(alias, tname):
                continue
            if checked >= _MAX_TABLES:
                break
            checked += 1
            cols = t.get("columns") or []
            try:
                n, stats = _column_stats(scope, alias, tname, cols, t.get("row_count"))
            except Exception as e:
                issues.append(("中", f"{alias}.{tname}: 集計できませんでした（{e}）"))
                continue

            pk, pk_src = catalog.effective_pk(profile, meta, tname)
            dup = _pk_duplicates(scope, alias, tname, pk)
            tbl_rows.append([f"{alias}.{tname}", n, len(cols),
                             "、".join(pk) if pk else "（無し）",
                             dup if dup is not None else "—"])
            if n == 0:
                issues.append(("高", f"{alias}.{tname} は0行です。取り込みが済んでいない可能性があります。"))
                continue
            if dup:
                issues.append(("高", f"{alias}.{tname} は主キー（{'、'.join(pk)}）が "
                                     f"{dup} 組で重複しています。件数や金額が二重に数えられます。"))
            if not pk:
                issues.append(("中", f"{alias}.{tname} に主キーがありません。"
                                     "重複を検出できないので、カタログで指定してください。"))

            for st in stats:
                nul_pct = round(st["nulls"] / n * 100, 1) if n else 0.0
                emp_pct = round(st["empty"] / n * 100, 1) if n else 0.0
                col_rows.append([f"{alias}.{tname}", st["column"], st["type"],
                                 nul_pct, emp_pct,
                                 st["unique"] if st["unique"] is not None else "—"])
                if st["nulls"] == n:
                    issues.append(("中", f"{alias}.{tname}.{st['column']} は全て空です。"
                                         "この列は集計に使えません。"))
                elif nul_pct >= 30:
                    issues.append(("中", f"{alias}.{tname}.{st['column']} は "
                                         f"{nul_pct}% が空です。平均を取ると母数がずれます。"))
                elif emp_pct >= 10:
                    issues.append(("低", f"{alias}.{tname}.{st['column']} は "
                                         f"{emp_pct}% が空文字です。NULLと混在しています。"))
                if st["unique"] == 1 and n > 1:
                    issues.append(("低", f"{alias}.{tname}.{st['column']} は1種類の値しかありません。"))

            # データがいつまで入っているか（古いまま気づかないのを防ぐ）
            for c in cols:
                if not _looks_like_date(c):
                    continue
                rng = _date_range(scope, alias, tname, c["name"])
                if rng and rng[1]:
                    tbl_rows[-1].append(f"{c['name']}: {rng[0]} 〜 {rng[1]}")
                    break

        # 参照整合性。カタログの結合定義とDBのFK宣言の両方を見る。
        # 子と親は保存順ではなく主キーの位置から決める（手書きのYAMLが
        # 逆向きでも、「親に居ない子」を正しい向きで数えるため）
        for edge in edges:
            (ca, ct, cc), (pa, pt, pc) = catalog.child_parent(entries, edge)
            if ca != alias or ct not in allowed:
                continue
            miss = _orphans(scope, (ca, ct, cc), (pa, pt, pc))
            if miss is None:
                continue
            kind = "FK宣言" if edge.get("kind") == "fk" else "カタログの結合定義"
            ref_rows.append([f"{ca}.{ct}.{cc}", f"{pa}.{pt}.{pc}", miss, kind])
            if miss:
                issues.append(("高", f"{ca}.{ct}.{cc} の {miss} 件が "
                                     f"{pa}.{pt} に存在しません。"
                                     "内部結合すると、この件数ぶん落ちます。"))

    if not tbl_rows:
        return _err("調べられるテーブルがありませんでした。"
                    "tables に指定した名前が合っているか確認してください"
                    "（例: 'stocks' または 'demo_inventory.stocks'）。")

    head = ["テーブル", "行数", "列数", "主キー", "主キー重複"]
    if any(len(r) > 5 for r in tbl_rows):
        head.append("日付の範囲")
    tbl_rows = [r + [""] * (len(head) - len(r)) for r in tbl_rows]

    rank = {"高": 0, "中": 1, "低": 2}
    issues.sort(key=lambda x: rank.get(x[0], 3))
    tables = [_table_of("テーブル", head, tbl_rows),
              _table_of("見つかった問題", ["深刻度", "内容"],
                        [[lv, msg] for lv, msg in issues[:80]] or [["—", "問題は見つかりませんでした。"]]),
              _table_of("列ごとの状態", ["テーブル", "列", "型", "空の割合(%)",
                                        "空文字の割合(%)", "値の種類数"], col_rows)]
    if ref_rows:
        tables.append(_table_of("参照整合性", ["子", "親", "親に無い件数", "定義元"], ref_rows))

    high = [m for lv, m in issues if lv == "高"]
    notes = [f"{checked} テーブルを調べました。"
             + (f"深刻な問題が {len(high)} 件あります。" if high
                else "分析を止めるような問題は見つかりませんでした。")]
    notes += high[:5]
    if checked >= _MAX_TABLES:
        notes.append(f"テーブルが多いため {_MAX_TABLES} 件までにしています。"
                     "続きは tables で対象を指定してください。")
    notes.append("行数が0・主キーの重複・親に無い外部キーは、集計結果を直接ゆがめます。"
                 "先にここを直してから数字を読んでください。")

    return _report_result({"title": "データ品質チェック", "tables": tables, "notes": notes,
                           "meta": {"tables_checked": checked,
                                    "issues": len(issues), "critical": len(high)}},
                          scope=scope)


def _table_of(name: str, columns: list, rows: list) -> dict:
    return {"name": name, "columns": columns, "rows": [tuple(r) for r in rows]}


HANDLERS = {
    "compare_periods": _compare_periods,
    "funnel_analysis": _funnel_analysis,
    "cohort_analysis": _cohort_analysis,
    "market_basket": _market_basket,
    "detect_anomalies": _detect_anomalies,
    "survival_analysis": _survival_analysis,
    "data_quality": _data_quality,
}

# data_quality は自分でDBを見に行くので、SQLプレビューの対象外
SQL_TOOLS = {"compare_periods", "funnel_analysis", "cohort_analysis",
             "market_basket", "detect_anomalies", "survival_analysis"}
