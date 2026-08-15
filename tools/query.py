"""調べる・集計する・描く・出す。SQLの結果をそのまま扱うツール。"""
from __future__ import annotations


import advanced
import analysis
import catalog
import charts
import config
import excel
import exports
from . import results
from .common import _err, _json, _report_result, fetch, source_note
from .schemas import _CHART_TOOLS


def _run_sql_query(args: dict, scope: list[dict]) -> dict:
    try:
        columns, rows, truncated, rid, total = fetch(args, scope,
                                                     label=args.get("purpose"))
    except advanced.AnalysisError as e:
        return _err(str(e))
    except Exception as e:
        return _err(f"SQL実行エラー: {e}")

    sample = rows[: config.SAMPLE_ROWS_FOR_LLM]
    llm_content = _json({
        "columns": columns,
        "row_count": len(rows),
        "rows": [list(r) for r in sample],
        "result_id": rid,
        "note": (f"全{len(rows)}行中 先頭{len(sample)}行を表示。"
                 f"この結果は result_id '{rid}' で他のツールから使い回せます"
                 "（同じSQLを書き直さなくてよい）。"
                 if len(rows) > len(sample) else
                 f"この結果は result_id '{rid}' で他のツールから使い回せます。"),
        **source_note(len(rows), truncated, total),
    })
    return {
        "ok": True,
        "llm_content": llm_content,
        "render": {
            "role": "assistant", "kind": "table",
            "columns": columns, "rows": rows, "truncated": truncated,
        },
    }


_CHART_FIELDS = ("chart_type", "x", "y", "color", "size", "text", "path",
                 "nbins", "orientation", "barmode", "title",
                 # 種別ごとに使う指定
                 "y2", "z", "lower", "upper", "facet", "dimensions",
                 "source", "target", "start", "end",
                 "open", "high", "low", "close",
                 "value", "agg", "max", "suffix", "valueformat",
                 "colorscale", "marginal", "trendline")


def _plot_chart(args: dict, scope: list[dict]) -> dict:
    try:
        columns, rows, truncated, rid, total = fetch(args, scope,
                                                     label=args.get("title"))
    except advanced.AnalysisError as e:
        return _err(str(e))
    except Exception as e:
        return _err(f"グラフ用SQLの実行エラー: {e}")

    item = {k: args.get(k) for k in _CHART_FIELDS}
    item["chart_type"] = item.get("chart_type") or "bar"
    errs = charts.validate(item, columns)
    if errs:
        return _err(" / ".join(errs))

    return {
        "ok": True,
        "llm_content": _json({
            "status": "chart_rendered",
            "chart_type": item["chart_type"],
            "columns": columns,
            "row_count": len(rows),
            "result_id": rid,
            **source_note(len(rows), truncated, total),
        }),
        "render": {
            "role": "assistant", "kind": "chart",
            "columns": columns, "rows": rows, **item,
            "title": args.get("title", ""),
        },
    }


def _plot_dual_axis(args: dict, scope: list[dict]) -> dict:
    try:
        columns, rows, truncated, rid, total = fetch(args, scope,
                                                     label=args.get("title"))
    except advanced.AnalysisError as e:
        return _err(str(e))
    except Exception as e:
        return _err(f"2軸グラフ用SQLの実行エラー: {e}")

    x = args.get("x")
    bar_y = args.get("bar_y") or []
    line_y = args.get("line_y") or []
    needed = [x] + list(bar_y) + list(line_y)
    missing = [c for c in needed if c and c not in columns]
    if missing or not bar_y or not line_y:
        msg = (f"指定列が結果に存在しません: {missing} / 利用可能な列: {columns}"
               if missing else "bar_y と line_y にはそれぞれ1つ以上の数値列を指定してください。")
        return _err(msg)

    return {
        "ok": True,
        "llm_content": _json({
            "status": "dual_axis_chart_rendered",
            "columns": columns, "row_count": len(rows),
            "bar_y": bar_y, "line_y": line_y, "result_id": rid,
            **source_note(len(rows), truncated, total),
        }),
        "render": {
            "role": "assistant", "kind": "chart_dual",
            "columns": columns, "rows": rows,
            "x": x, "bar_y": bar_y, "line_y": line_y,
            "left_title": args.get("left_title"), "right_title": args.get("right_title"),
            "title": args.get("title", ""),
        },
    }


def _describe_table(args: dict, scope: list[dict]) -> dict:
    text = catalog.describe_table_text(scope, args.get("db", ""), args.get("table", ""))
    return {"ok": not text.startswith("エラー"), "llm_content": text, "render": None}


def _pivot_table(args: dict, scope: list[dict]) -> dict:
    try:
        columns, rows, truncated, rid, total = fetch(args, scope,
                                                     label=args.get("title"))
    except advanced.AnalysisError as e:
        return _err(str(e))
    except Exception as e:
        return _err(f"クロス集計用SQLの実行エラー: {e}")
    if not rows:
        return _err("データが0行でした。抽出条件を見直してください。")

    try:
        cols, prows = analysis.pivot(
            columns, rows,
            index=args.get("index") or [], cols=args.get("columns") or None,
            values=args.get("values"), aggfunc=args.get("aggfunc") or "sum",
            margins=bool(args.get("margins")),
            percent=args.get("percent"), rank_by=args.get("rank"),
        )
    except Exception as e:
        return _err(f"クロス集計に失敗しました: {e}")

    title = args.get("title") or "クロス集計"
    # 集計後の表もグラフやレポートの材料になるので、指せるようにして返す
    out_rid = results.put(scope, cols, prows, label=f"{title}（クロス集計の結果）")
    llm_content = _json({
        "status": "pivot_ready", "columns": cols, "row_count": len(prows),
        "rows": [list(r) for r in prows[: config.SAMPLE_ROWS_FOR_LLM]],
        "result_id": out_rid, "source_result_id": rid,
        "note": f"集計後の表は result_id '{out_rid}' でグラフやレポートに渡せます。",
        **source_note(len(rows), truncated, total),
    })
    if (args.get("render") or "table") == "heatmap":
        render = {"role": "assistant", "kind": "chart", "columns": cols, "rows": prows,
                  "chart_type": "matrix", "x": cols[0], "title": title}
    else:
        render = {"role": "assistant", "kind": "table", "columns": cols, "rows": prows,
                  "truncated": False}
    return {"ok": True, "llm_content": llm_content, "render": render}


def _analyze_stats(args: dict, scope: list[dict]) -> dict:
    method = args.get("method") or "describe"
    try:
        columns, rows, truncated, rid, total = fetch(args, scope,
                                                     label=args.get("title"))
    except advanced.AnalysisError as e:
        return _err(str(e))
    except Exception as e:
        return _err(f"分析用SQLの実行エラー: {e}")
    if not rows:
        return _err("データが0行でした。抽出条件を見直してください。")

    title = args.get("title") or {"describe": "基本統計量", "correlation": "相関",
                                  "outliers": "外れ値"}.get(method, "分析")
    note = f"（元データ {len(rows):,} 行"
    note += "／上限で切り詰め済み）" if truncated else "）"

    try:
        if method == "describe":
            cols, srows = analysis.describe(columns, rows, args.get("columns"),
                                            args.get("group_by"))
            extra = {}
            render = {"role": "assistant", "kind": "table", "columns": cols, "rows": srows}
        elif method == "correlation":
            cm = args.get("corr_method") or "pearson"
            lag = int(args.get("lag") or 0)
            if args.get("partial"):
                # 交絡を取り除いた相関。「効いて見えるのは第3の変数のせい」を切り分ける
                res = advanced.partial_correlation(
                    columns, rows, args.get("columns"), args.get("control") or [],
                    method=cm)
                return _report_result(res, source_rows=len(rows), truncated=truncated,
                                      total=total, result_id=rid, scope=scope,
                                      extra={"method": "partial_correlation"})
            if lag:
                # 時差相関。「広告費は翌月の売上に効く」を見るための道具
                res = advanced.lag_correlation(
                    columns, rows, args.get("target"), args.get("columns"),
                    max_lag=lag, method=cm)
                return _report_result(res, source_rows=len(rows), truncated=truncated,
                                      total=total, result_id=rid, scope=scope,
                                      extra={"method": "lag_correlation"})
            cols, srows = analysis.correlation(columns, rows, args.get("columns"), cm)
            extra = {"method": cm, "strong_pairs": analysis.correlation_pairs(columns, rows, cm)[:8],
                     "caution": "相関は因果ではありません。効いている理由を確かめるには "
                                "partial=true で交絡を除くか、regression を使ってください。"}
            render = {"role": "assistant", "kind": "chart", "columns": cols, "rows": srows,
                      "chart_type": "matrix", "x": cols[0], "title": f"{title}{note}",
                      "colorscale": "RdBu"}
        else:
            target = args.get("target")
            if not target:
                return _err("outliers には target（外れ値を調べる数値列）が必要です。")
            om = args.get("outlier_method") or "iqr"
            # mahalanobis は複数列をまとめて見る。"売上, 客数" のような指定も許す。
            cols_in = [t.strip() for t in str(target).split(",")] if isinstance(target, str) \
                else list(target)
            res = advanced.outliers_ext(columns, rows,
                                        cols_in if len(cols_in) > 1 else cols_in[0],
                                        method=om, threshold=args.get("threshold"))
            return _report_result(res, source_rows=len(rows), truncated=truncated,
                                  total=total, result_id=rid, scope=scope,
                                  extra={"method": "outliers", "outlier_method": om})
    except Exception as e:
        return _err(f"{title}の計算に失敗しました: {e}")

    out_rid = results.put(scope, cols, srows, label=title)
    return {
        "ok": True,
        "llm_content": _json({
            "status": "stats_ready", "method": method, "columns": cols,
            "row_count": len(srows),
            "rows": [list(r) for r in srows[: config.SAMPLE_ROWS_FOR_LLM]],
            "result_id": out_rid, "source_result_id": rid,
            **source_note(len(rows), truncated, total), **extra,
        }),
        "render": render,
    }


def _export_excel(args: dict, scope: list[dict]) -> dict:
    sheets_in = args.get("sheets") or []
    if not sheets_in:
        return _err("sheets が空です。少なくとも1つ SELECT を指定してください。")

    built, summary = [], []
    for i, sh in enumerate(sheets_in, start=1):
        name = (sh or {}).get("name") or f"Sheet{i}"
        # ファイルには全行入れる（画面向けの2,000行とは別枠）。
        # Excelのシートは仕様上 1,048,576 行までなので、見出しぶんを引いて丸める。
        cap = min(config.EXPORT_MAX_ROWS, 1_048_575)
        try:
            columns, rows, truncated, _, _ = fetch(sh or {}, scope, label=name,
                                                   max_rows=cap)
        except advanced.AnalysisError as e:
            return _err(f"シート '{name}': {e}")
        except Exception as e:
            return _err(f"シート '{name}' のSQL実行エラー: {e}")
        note = (sh or {}).get("note") or ""
        if truncated:
            note = (note + f"（{cap:,}行で切り詰め）").strip()
        charts = (sh or {}).get("charts") or (sh or {}).get("chart")
        if isinstance(charts, dict):
            charts = [charts]
        built.append({"name": name, "columns": columns, "rows": rows, "note": note,
                      "charts": charts or []})
        summary.append({"sheet": name, "columns": columns, "row_count": len(rows),
                        "truncated": truncated,
                        "charts": [c.get("type") for c in (charts or [])]})

    try:
        data = excel.build(built, title=args.get("purpose") or args.get("filename"))
    except ValueError as e:
        return _err(f"Excelのグラフを作れませんでした: {e}")
    except Exception as e:
        return _err(f"Excelの作成に失敗しました: {e}")

    filename = exports.safe_filename(args.get("filename"), "xlsx")
    return {
        "ok": True,
        "llm_content": _json({
            "status": "file_ready",
            "filename": filename,
            "sheets": summary,
            "note": "ユーザーの画面に保存済み。ファイルの中身を再度説明する必要はない。",
        }),
        "render": {
            "role": "assistant", "kind": "file", "filename": filename,
            "mime": exports.XLSX_MIME, "data": data, "sheets": built,
            "note": f"{len(built)}シート",
        },
    }


def _export_csv(args: dict, scope: list[dict]) -> dict:
    files_in = args.get("files") or []
    if not files_in:
        return _err("files が空です。少なくとも1つ SELECT を指定してください。")
    enc = args.get("encoding") or exports.DEFAULT_ENCODING
    delim = args.get("delimiter") or "comma"

    made, summary, preview = [], [], []
    for i, f in enumerate(files_in, start=1):
        name = (f or {}).get("name") or f"data{i}"
        try:
            columns, rows, truncated, _, _ = fetch(f or {}, scope, label=name,
                                                   max_rows=config.EXPORT_MAX_ROWS)
        except advanced.AnalysisError as e:
            return _err(f"'{name}': {e}")
        except Exception as e:
            return _err(f"'{name}' のSQL実行エラー: {e}")
        try:
            data = exports.build_csv(columns, rows, enc, delim)
        except Exception as e:
            return _err(f"'{name}' のCSV作成に失敗しました（文字コード {enc}）: {e}")
        made.append({"filename": exports.safe_filename(name, "csv"), "data": data})
        summary.append({"file": name, "columns": columns, "row_count": len(rows),
                        "truncated": truncated})
        preview.append({"name": name, "columns": columns, "rows": rows})

    if len(made) == 1:
        filename, data, mime = made[0]["filename"], made[0]["data"], exports.CSV_MIME
    else:
        filename = exports.safe_filename(args.get("purpose") or "csv_files", "zip")
        data, mime = exports.build_zip(made), exports.ZIP_MIME

    return {
        "ok": True,
        "llm_content": _json({
            "status": "file_ready", "filename": filename,
            "encoding": enc, "delimiter": delim, "files": summary,
            "note": "ユーザーの画面に保存済み。中身を再度全部説明する必要はない。",
        }),
        "render": {
            "role": "assistant", "kind": "file", "filename": filename,
            "mime": mime, "data": data, "sheets": preview,
            "note": f"文字コード {enc} / 区切り {delim}",
        },
    }


def _export_text(args: dict, scope: list[dict]) -> dict:
    body = str(args.get("body") or "")
    fmt = args.get("format") or "md"
    enc = args.get("encoding") or exports.DEFAULT_ENCODING
    style = "markdown" if fmt == "md" else "plain"

    summary, preview = [], []
    for sec in (args.get("sections") or []):
        heading = str((sec or {}).get("heading") or "")
        try:
            columns, rows, truncated, _, _ = fetch(sec or {}, scope, label=heading,
                                                   max_rows=config.EXPORT_MAX_ROWS)
        except advanced.AnalysisError as e:
            return _err(f"セクション '{heading}': {e}")
        except Exception as e:
            return _err(f"セクション '{heading}' のSQL実行エラー: {e}")
        table = exports.table_to_text(columns, rows, style)
        block = (f"## {heading}\n\n{table}\n" if fmt == "md"
                 else f"■ {heading}\n\n{table}\n")
        placeholder = "{{" + heading + "}}"
        if placeholder in body:
            body = body.replace(placeholder, block)
        else:
            body = body.rstrip() + "\n\n" + block
        summary.append({"heading": heading, "columns": columns, "row_count": len(rows),
                        "truncated": truncated})
        preview.append({"name": heading, "columns": columns, "rows": rows})

    try:
        data = exports.build_text(body, enc)
    except Exception as e:
        return _err(f"テキストの書き出しに失敗しました（文字コード {enc}）: {e}")

    filename = exports.safe_filename(args.get("filename"), fmt)
    return {
        "ok": True,
        "llm_content": _json({
            "status": "file_ready", "filename": filename,
            "format": fmt, "encoding": enc, "sections": summary,
            "chars": len(body),
            "note": "ユーザーの画面に保存済み。本文を再度全部繰り返す必要はない。",
        }),
        "render": {
            "role": "assistant", "kind": "file", "filename": filename,
            "mime": exports.MD_MIME if fmt == "md" else exports.TEXT_MIME,
            "data": data, "text": body, "sheets": preview,
            "note": f"{fmt} / 文字コード {enc} / {len(body):,} 文字",
        },
    }

# このモジュールが受け持つツール
HANDLERS = {
    "run_sql_query": _run_sql_query,
    "describe_table": _describe_table,
    "pivot_table": _pivot_table,
    "analyze_stats": _analyze_stats,
    "plot_chart": _plot_chart,
    "plot_dual_axis": _plot_dual_axis,
    # 用途別のグラフツールは、中身はどれも同じ組み立てを通る
    **{name: _plot_chart for name in _CHART_TOOLS},
    "export_excel": _export_excel,
    "export_csv": _export_csv,
    "export_text": _export_text,
}

# SQLを受け取るツール（実行前プレビュー表示の対象）
SQL_TOOLS = {"run_sql_query", "plot_chart", "plot_dual_axis", "pivot_table",
             "analyze_stats", *_CHART_TOOLS}
