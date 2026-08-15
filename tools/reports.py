"""レポート出力（PowerPoint / Word / 画面用のレポート）。"""
from __future__ import annotations

import re
from datetime import datetime

import advanced
import charts
import docx_report
import excel
import exports
import figures
import pptx_report
from .common import _err, _json, fetch


def _slide_from_sql(spec: dict, scope: list[dict], index: int) -> dict:
    """slides の1枚ぶん。sql か result_id があればここで中身に変える。"""
    out = dict(spec)
    kind = str(spec.get("kind") or "message").lower()
    if not (spec.get("sql") or spec.get("result_id")) or kind not in ("table", "chart"):
        return out

    columns, rows, truncated, _, _ = fetch(spec, scope, label=spec.get("title"))
    if not rows:
        raise pptx_report.ReportError(f"{index}枚目「{spec.get('title', '')}」の"
                                      "SQLが0行でした。抽出条件を見直してください。")
    if kind == "table":
        out["columns"], out["rows"] = columns, [list(r) for r in rows]
        if truncated:
            out["comment"] = (out.get("comment", "") + "　※ 上限で切り詰め済み").strip()
        return out

    cat = spec.get("category_column") or columns[0]
    if cat not in columns:
        raise pptx_report.ReportError(
            f"{index}枚目: 横軸の列 '{cat}' がSQLの結果にありません"
            f"（ある列: {', '.join(columns)}）。")
    vals = spec.get("value_columns") or [c for c in columns if c != cat]
    missing = [v for v in vals if v not in columns]
    if missing:
        raise pptx_report.ReportError(
            f"{index}枚目: 系列の列 {', '.join(missing)} がSQLの結果にありません"
            f"（ある列: {', '.join(columns)}）。")
    ci = columns.index(cat)
    out["categories"] = [r[ci] for r in rows]
    if str(spec.get("chart") or "bar").lower() == "scatter":
        xi = columns.index(vals[0])
        out["series"] = [{"name": vals[1] if len(vals) > 1 else vals[0],
                          "x": [r[xi] for r in rows],
                          "values": [r[columns.index(vals[1] if len(vals) > 1 else vals[0])]
                                     for r in rows]}]
    else:
        out["series"] = [{"name": v, "values": [r[columns.index(v)] for r in rows]}
                         for v in vals]
    return out


def _export_pptx(args: dict, scope: list[dict]) -> dict:
    slides_in = args.get("slides") or []
    if not slides_in:
        return _err("slides が空です。少なくとも1枚は指定してください。")
    built = []
    for i, spec in enumerate(slides_in, start=1):
        try:
            built.append(_slide_from_sql(spec or {}, scope, i))
        except (pptx_report.ReportError, advanced.AnalysisError) as e:
            return _err(f"{i}枚目: {e}" if isinstance(e, advanced.AnalysisError) else str(e))
        except Exception as e:
            return _err(f"{i}枚目のSQL実行エラー: {e}")
    try:
        data = pptx_report.build(built, title=args.get("title"),
                                 subtitle=args.get("subtitle"),
                                 footer=args.get("footer"))
    except pptx_report.ReportError as e:
        return _err(str(e))
    except Exception as e:
        return _err(f"PowerPointの作成に失敗しました: {e}")

    filename = pptx_report.safe_filename(args.get("filename") or args.get("title"))
    outline = pptx_report.outline(built)
    return {
        "ok": True,
        "llm_content": _json({
            "status": "file_ready", "filename": filename,
            "slides": outline,
            "note": "ユーザーの画面に保存済み。中身を再度説明する必要はない。",
        }),
        "render": {"role": "assistant", "kind": "file", "filename": filename,
                   "mime": PPTX_MIME, "data": data,
                   "note": f"{len(built)}スライド", "outline": outline},
    }


PPTX_MIME = ("application/vnd.openxmlformats-officedocument."
             "presentationml.presentation")


DOCX_MIME = ("application/vnd.openxmlformats-officedocument."
             "wordprocessingml.document")


def _export_docx(args: dict, scope: list[dict]) -> dict:
    """Word文書。図表つきで、そのまま配布できる体裁にする。"""
    secs_in = args.get("sections") or []
    if not secs_in:
        return _err("sections が空です。少なくとも1つの見出しを入れてください。")

    sections, figs, tbls = [], 0, 0
    for i, s in enumerate(secs_in, start=1):
        s = s or {}
        if not s.get("heading"):
            return _err(f"{i}番目のセクションに heading がありません。")
        sec = {k: s.get(k) for k in ("heading", "body", "bullets", "note",
                                     "callout", "level", "page_break")}
        # 図表のキャプションは、見出し頭の「1. 」を落として重複を避ける
        label = re.sub(r"^\s*\d+[.．)、]\s*", "", s["heading"])
        if s.get("sql") or s.get("result_id"):
            try:
                columns, rows, truncated, _, _ = fetch(s, scope, label=s["heading"])
            except advanced.AnalysisError as e:
                return _err(f"「{s['heading']}」: {e}")
            except Exception as e:
                return _err(f"「{s['heading']}」のSQL実行エラー: {e}")
            if not rows:
                return _err(f"「{s['heading']}」のデータが0行でした。")
            limit = int(s.get("max_rows") or 40)
            if s.get("chart"):
                chart = {**(s["chart"] or {}), "columns": columns,
                         "rows": [list(r) for r in rows],
                         "title": (s["chart"] or {}).get("title") or s["heading"]}
                chart.setdefault("chart_type", "bar")
                errs = charts.validate(chart, columns)
                if errs:
                    return _err(f"「{s['heading']}」のグラフ指定: {' / '.join(errs)}")
                img = _chart_image(chart)
                if img:
                    sec["image"] = img
                    sec["caption"] = s.get("caption") or label
                    figs += 1
            if s.get("table", True):        # 既定で表も載せる（根拠として残す）
                sec["table"] = {"columns": columns,
                                "rows": [list(r) for r in rows[:limit]]}
                if len(rows) > limit or truncated:
                    sec["table"]["note"] = f"全 {len(rows):,} 行から抜粋"
                sec["table_caption"] = s.get("table_caption") or label
                tbls += 1
        sections.append(sec)

    try:
        data = docx_report.build(
            sections, title=args.get("title", "レポート"),
            subtitle=args.get("subtitle", ""),
            summary=args.get("summary") or [],
            conclusion=args.get("conclusion", ""),
            recommendations=args.get("recommendations") or [],
            caveats=args.get("caveats") or [],
            footer=args.get("footer", ""), org=args.get("org", ""),
            author=args.get("author", ""), toc=bool(args.get("toc", True)))
    except docx_report.ReportError as e:
        return _err(str(e))
    except Exception as e:
        return _err(f"Word文書の作成に失敗しました: {e}")

    filename = docx_report.safe_filename(args.get("filename") or args.get("title"))
    note = ""
    if figs == 0 and any(s.get("chart") for s in secs_in):
        note = figures.why_unavailable()
    return {
        "ok": True,
        "llm_content": _json({
            "status": "file_ready", "filename": filename,
            "sections": docx_report.outline(sections),
            "figures": figs, "tables": tbls,
            "warning": note or None,
            "note": "ユーザーの画面に保存済み。中身を再度説明する必要はない。",
        }),
        "render": {"role": "assistant", "kind": "file", "filename": filename,
                   "mime": DOCX_MIME, "data": data,
                   "note": f"{len(sections)}セクション / 図{figs} 表{tbls}"
                           + (f" ／ {note}" if note else ""),
                   "outline": docx_report.outline(sections)},
    }


def _report_markdown(args: dict, sections: list[dict]) -> str:
    """レポートを1本の Markdown にする（ダウンロード用と、表示の下敷き）。"""
    out = [f"# {args.get('title', 'レポート')}"]
    if args.get("subtitle"):
        out.append(f"*{args['subtitle']}*")
    out.append(f"*作成: {datetime.now():%Y-%m-%d %H:%M}*")
    if args.get("summary"):
        out += ["", "## 要点"] + [f"- {s}" for s in args["summary"]]
    for i, s in enumerate(sections, 1):
        out += ["", f"## {i}. {s['heading']}"]
        if s.get("body"):
            out += ["", s["body"]]
        if s.get("table"):
            t = s["table"]
            out += ["", exports.table_to_text(t["columns"], t["rows"], "markdown")]
            if t.get("truncated"):
                out.append(f"（全 {t['total']:,} 行のうち上位 {len(t['rows'])} 行）")
        if s.get("chart"):
            out.append(f"（グラフ: {s['chart'].get('chart_type')} — 画面で確認できます）")
        if s.get("note"):
            out += ["", f"> {s['note']}"]
    if args.get("conclusion"):
        out += ["", "## 結論", "", args["conclusion"]]
    if args.get("recommendations"):
        out += ["", "## 推奨する打ち手"] + [f"{i}. {r}" for i, r
                                            in enumerate(args["recommendations"], 1)]
    if args.get("caveats"):
        out += ["", "## 前提・注意"] + [f"- {c}" for c in args["caveats"]]
    return "\n".join(out) + "\n"


# PowerPointのネイティブグラフは種類が限られる。近いもので描けるならそれを使い、
# 描けない種類（サンキー・箱ひげ等）は画像として貼る。
_PPTX_CHART_MAP = {
    "bar": "bar", "hbar": "hbar", "stacked_bar": "bar_stacked",
    "percent_bar": "bar_percent", "lollipop": "bar", "pareto": "bar",
    "line": "line", "step": "line", "bump": "line", "slope": "line",
    "area": "area", "area_percent": "area_stacked",
    "pie": "pie", "donut": "doughnut", "funnel": "hbar", "radar": "radar",
    "scatter": "scatter", "bubble": "scatter", "polar_bar": "radar",
}


# Excelのグラフも種類が限られる。近いものに寄せ、無理なものは表だけにする。
_XLSX_CHART_MAP = {
    "bar": "bar", "hbar": "hbar", "stacked_bar": "bar_stacked",
    "percent_bar": "bar_percent", "lollipop": "bar", "pareto": "bar",
    "line": "line", "step": "line", "bump": "line", "slope": "line",
    "area": "area", "area_percent": "area_stacked",
    "pie": "pie", "donut": "pie", "funnel": "hbar",
    "scatter": "scatter", "bubble": "scatter",
}


def _chart_image(chart: dict):
    """グラフを印刷向けの画像にする。できなければ None。"""
    try:
        return figures.for_print(charts.build_figure(chart))
    except Exception as e:
        print(f"[report] 画像化に失敗: {e}")
        return None


def _series_from(chart: dict, table: dict):
    """表からPowerPointのネイティブグラフ用の系列を組み立てる。"""
    cols = table["columns"]
    cat = chart.get("x") if chart.get("x") in cols else cols[0]
    ci = cols.index(cat)
    if chart.get("chart_type") in ("scatter", "bubble"):
        xs = [r[cols.index(chart["x"])] for r in table["rows"]]
        ys = [r[cols.index(chart["y"])] for r in table["rows"]]
        return [], [{"name": chart.get("y", "値"), "x": xs, "values": ys}]
    wanted = [chart["y"]] if chart.get("y") in cols else \
        [c for c in cols if c != cat][:3]
    return ([r[ci] for r in table["rows"]],
            [{"name": v, "values": [r[cols.index(v)] for r in table["rows"]]}
             for v in wanted])


def _split_message(s: dict) -> tuple[str, str]:
    """1ページの「言いたいこと1行」と、横に添える残りの文章に分ける。

    所見(note)があればそれを1行目にする。無ければ本文の最初の一文を使い、
    その場合は本文の残りだけを横に置く（同じ文を2回出さない）。
    """
    body = (s.get("body") or "").strip()
    if s.get("note"):
        return s["note"], body
    if not body:
        return "", ""
    head, sep, rest = body.partition("。")
    if not sep:
        return body[:90], ""
    return head + "。", rest.strip()


def _report_slides(args: dict, sections: list[dict]) -> list[dict]:
    """レポートの内容を、会議で映せるスライドの並びに翻訳する。"""
    summary = args.get("summary") or []
    slides = [{"kind": "title", "title": args.get("title", "レポート"),
               "subtitle": args.get("subtitle", ""),
               "lines": summary[:4], "org": args.get("org", "")}]
    if len(sections) >= 2:
        slides.append({"kind": "agenda", "title": "本日の内容",
                       "items": [s["heading"] for s in sections]})
    if summary:
        # 帯に出した1点目は繰り返さない（同じ文が2回出ると雑に見える）
        slides.append({"kind": "message", "title": "要約",
                       "message": summary[0],
                       "bullets": summary[1:] or summary,
                       "callout": args.get("conclusion", "")})

    for s in sections:
        message, comment = _split_message(s)
        base = {"title": s["heading"],
                # 見出しの下に置く1行。結論を先に言う。
                "message": message, "comment": comment,
                "notes": s.get("body") or "",
                "source": args.get("source", "")}
        chart, table = s.get("chart"), s.get("table")
        if chart and table:
            kind = chart.get("chart_type")
            native = _PPTX_CHART_MAP.get(kind)
            if native:
                cats, series = _series_from(chart, table)
                slides.append({**base, "kind": "chart", "chart": native,
                               "categories": cats, "series": series})
            else:
                img = _chart_image(chart)
                if img:
                    slides.append({**base, "kind": "chart", "image": img})
                else:
                    slides.append({**base, "kind": "table", **table})
        elif table:
            slides.append({**base, "kind": "table", **table})
        else:
            slides.append({**base, "kind": "message", "comment": None,
                           "lead": base.pop("comment", "") or "",
                           "bullets": s.get("bullets") or []})

    if args.get("conclusion") or args.get("recommendations"):
        slides.append({"kind": "closing", "title": "まとめと次のアクション",
                       "message": args.get("conclusion", ""),
                       "summary": (args.get("summary") or [])[:3],
                       "actions": args.get("recommendations") or []})
    return slides


def _report_docx_sections(args: dict, sections: list[dict]) -> list[dict]:
    """レポートの内容を、Wordのセクションに翻訳する（図は画像で貼る）。"""
    out = []
    for s in sections:
        label = re.sub(r"^\s*\d+[.．)、]\s*", "", s["heading"])
        sec = {"heading": s["heading"], "body": s.get("body", ""),
               "note": s.get("note", ""), "caption": label}
        if s.get("chart"):
            img = _chart_image(s["chart"])
            if img:
                sec["image"] = img
        if s.get("table"):
            sec["table"] = {"columns": s["table"]["columns"],
                            "rows": s["table"]["rows"]}
            sec["table_caption"] = label
            if s["table"].get("truncated"):
                sec["table"]["note"] = f"全 {s['table']['total']:,} 行から抜粋"
        out.append(sec)
    return out


def _build_report(args: dict, scope: list[dict]) -> dict:
    sections_in = args.get("sections") or []
    if not sections_in:
        return _err("sections が空です。少なくとも1つの論点を入れてください。")

    sections, dropped = [], []
    for i, s in enumerate(sections_in, 1):
        s = s or {}
        if not s.get("heading"):
            return _err(f"{i}番目のセクションに heading がありません。")
        out = {"heading": s["heading"], "body": s.get("body", ""),
               "note": s.get("note", "")}
        if s.get("sql") or s.get("result_id"):
            try:
                columns, rows, truncated, _, _ = fetch(s, scope, label=s["heading"])
            except advanced.AnalysisError as e:
                return _err(f"「{s['heading']}」: {e}")
            except Exception as e:
                return _err(f"「{s['heading']}」のSQL実行エラー: {e}")
            limit = int(s.get("max_rows") or 20)
            out["table"] = {"columns": columns, "rows": [list(r) for r in rows[:limit]],
                            "total": len(rows), "truncated": len(rows) > limit or truncated}
            out["sql"] = s.get("sql")
            if s.get("chart"):
                chart = {k: v for k, v in (s["chart"] or {}).items()}
                chart.setdefault("chart_type", "bar")
                errs = charts.validate(chart, columns)
                if errs:
                    # グラフ1つのためにレポート全体を捨てない。
                    # 表は根拠として残るので、図を落として作り切る方が役に立つ。
                    dropped.append(f"「{s['heading']}」のグラフ: {' / '.join(errs)}")
                else:
                    # グラフは全行を使う（表は読みやすさのために切っている）
                    out["chart"] = {**chart, "columns": columns,
                                    "rows": [list(r) for r in rows],
                                    "title": chart.get("title") or s["heading"]}
        elif s.get("chart"):
            dropped.append(f"「{s['heading']}」のグラフ: 元になる sql / result_id がありません。")
        sections.append(out)

    md = _report_markdown(args, sections)
    fmt = (args.get("format") or "md").lower()
    name = args.get("filename") or args.get("title") or "report"
    data = filename = mime = None
    try:
        if fmt == "md":
            data = exports.build_text(md)
            filename = exports.safe_filename(name, "md")
            mime = exports.TEXT_MIME
        elif fmt == "xlsx":
            sheets = []
            for s in sections:
                if not s.get("table"):
                    continue
                sheet = {"name": s["heading"], "columns": s["table"]["columns"],
                         "rows": s["table"]["rows"], "note": s.get("note", "")}
                # 画面のグラフ指定を、そのままExcelのグラフに読み替える
                if s.get("chart"):
                    ch = s["chart"]
                    kind = _XLSX_CHART_MAP.get(ch.get("chart_type"))
                    if kind:
                        cat = ch.get("x") if ch.get("x") in sheet["columns"] \
                            else sheet["columns"][0]
                        vals = ([ch["y"]] if ch.get("y") in sheet["columns"]
                                else [c for c in sheet["columns"] if c != cat][:3])
                        sheet["charts"] = [{"type": kind, "category_column": cat,
                                            "value_columns": vals,
                                            "title": s["heading"]}]
                sheets.append(sheet)
            if not sheets:
                return _err("xlsx にするには、表（sql）のあるセクションが1つ以上必要です。")
            data = excel.build(sheets, title=args.get("title"))
            filename = exports.safe_filename(name, "xlsx")
            mime = exports.XLSX_MIME
        elif fmt == "pptx":
            data = pptx_report.build(_report_slides(args, sections),
                                     title=args.get("title"),
                                     subtitle=args.get("subtitle"),
                                     footer=args.get("footer", ""))
            filename = pptx_report.safe_filename(name)
            mime = PPTX_MIME
        elif fmt == "docx":
            data = docx_report.build(
                _report_docx_sections(args, sections),
                title=args.get("title", "レポート"),
                subtitle=args.get("subtitle", ""),
                summary=args.get("summary") or [],
                conclusion=args.get("conclusion", ""),
                recommendations=args.get("recommendations") or [],
                caveats=args.get("caveats") or [],
                footer=args.get("footer", ""), org=args.get("org", ""))
            filename = docx_report.safe_filename(name)
            mime = DOCX_MIME
    except Exception as e:
        return _err(f"ファイルの作成に失敗しました: {e}")

    render = {"role": "assistant", "kind": "report_doc",
              "title": args.get("title", "レポート"),
              "subtitle": args.get("subtitle", ""),
              "summary": args.get("summary") or [],
              "sections": sections,
              "conclusion": args.get("conclusion", ""),
              "recommendations": args.get("recommendations") or [],
              "caveats": args.get("caveats") or [],
              "markdown": md}
    if data:
        render.update(data=data, filename=filename, mime=mime)

    return {
        "ok": True,
        "llm_content": _json({
            "status": "report_ready", "title": args.get("title"),
            "sections": [{"heading": s["heading"],
                          "rows": (s.get("table") or {}).get("total"),
                          "chart": (s.get("chart") or {}).get("chart_type")}
                         for s in sections],
            "filename": filename,
            "dropped_charts": dropped or None,
            "note": "レポートは画面に表示済み。内容をもう一度書き出す必要はない。"
                    "次に何をするか（送付・追加分析など）だけ短く伝えること。"
                    + ("　※ 一部のグラフは指定が合わず省いた。作り直すなら、"
                       "dropped_charts の指摘どおりに列名を直して呼ぶこと"
                       "（同じ引数で呼び直さない）。" if dropped else ""),
        }),
        "render": render,
    }

HANDLERS = {
    "export_pptx": _export_pptx,
    "export_docx": _export_docx,
    "build_report": _build_report,
}

SQL_TOOLS: set[str] = set()
