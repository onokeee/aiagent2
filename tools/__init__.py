"""LLM(OpenAI互換 function calling)に渡すツール定義と実行ロジック。

組み込みツールの系統:
  調べる    run_sql_query / describe_table
  集計する  pivot_table / analyze_stats
  描く      plot_comparison / plot_trend / plot_composition / plot_distribution /
            plot_relationship / plot_kpi / plot_dual_axis
  統計      hypothesis_test / regression / distribution_analysis
  時系列    forecast / timeseries_analysis / detect_anomalies
  試算      monte_carlo_simulation / scenario_analysis / bootstrap_estimate
  分ける    clustering / abc_analysis
  業務分析  compare_periods / funnel_analysis / cohort_analysis / market_basket /
            survival_analysis / data_quality
  ファイル  explore_import_files（取り込み元フォルダを読むだけ。管理者のみ）
  自己分析  analyze_usage（このアプリ自身の使われ方。管理者のみ）
  出す      export_excel / export_csv / export_text / export_pptx / export_docx /
            build_report
  送る      find_mail_recipients / compose_email
            ※ 実際の送信はユーザーが画面のボタンを押したときだけ。
              誤送信は取り消せないので、LLMには下書きまでしかさせない。

データを取るツールは sql の代わりに result_id を受け取れる。前のツールが返した
データをそのまま使えるので、同じSQLを何度も流さずに済む（results.py 参照）。

これに加えて、ユーザーが画面から定義したSQLテンプレート型ツール
（各DBの .meta.yaml の tools:）を実行時に合成する。custom_tools.py 参照。
組み込みツールは .meta.yaml の builtin_tools: で無効化・説明の上書きができる。

dispatch(name, arguments_json, scope, entries, admin) の戻り値:
  {
    "ok": bool,
    "llm_content": str,        # LLMへ返すテキスト(JSON)。トークン節約のため要約。
    "render": dict | None,     # UI描画用アイテム(app側が kind を見て解釈)
  }

scope はチャット側で選択された対象DB群:
  [{"path": str, "alias": str, "tables": [...]}, ...]

ファイルの分かれ方:
  schemas.py  … LLMに見せるツールの宣言（JSON Schema）。処理は書かない
  common.py   … 実処理が共通で使う小道具（データの取り出しもここ）
  results.py  … 取ったデータの置き場（result_id で使い回す）
  query.py    … 調べる・集計する・描く・出す
  stats.py    … 統計と試算
  business.py … 期間比較・ファネル・コホート・併売・異常検知・生存時間・品質
  reports.py  … PowerPoint / Word / 画面用レポート
  mail.py     … 宛先探しと下書き
  files.py    … 取り込み元フォルダの調査（管理者のみ）
  usage.py    … このアプリ自身の利用状況（管理者のみ）
ツールを1つ足すときは、宣言(schemas)と実処理(各モジュール)の2箇所を触る。
実処理を置いたモジュールの HANDLERS に名前を登録すれば dispatch から引ける。
管理者だけに渡したいものは、そのモジュールの ADMIN_TOOLS にも名前を入れる。
"""
from __future__ import annotations

import json

import config
import custom_tools
import db
import excel
import exports
import verify
from . import business, files, mail, query, reports, stats, usage
from . import results as _results
from .common import _err, _json
from .schemas import BUILTIN_TOOLS

_MODULES = (query, stats, reports, mail, business, files, usage)

# ツール名 -> 実処理。各モジュールが自分のぶんを申告する。
_HANDLERS = {name: fn for m in _MODULES for name, fn in m.HANDLERS.items()}

# SQLを受け取る組み込みツール（実行前プレビュー表示の対象）
SQL_TOOLS = {name for m in _MODULES for name in m.SQL_TOOLS}

# 管理者にだけ渡すツール。画面側で管理者専用になっているものは、
# AI経由でも同じ制限にしないと抜け道になる（取り込み元フォルダの中身など）。
ADMIN_TOOLS = {name for m in _MODULES for name in getattr(m, "ADMIN_TOOLS", ())}


def render_sql(tool: dict) -> str:
    """UIのプレビュー用。実行時は :name のままバインドするので置換はしない。"""
    return str(tool.get("sql") or "").strip()


def _run_custom(tool: dict, args: dict, scope: list[dict]) -> dict:
    sql = render_sql(tool)
    try:
        params = custom_tools.coerce_params(tool, args)
    except ValueError as e:
        return _err(str(e))
    # ツールは作るときにDBを意識させないので、SQLが選択外のDBに入ることがある。
    # 必要なぶんは繋いでから実行する（結果を預ける先も同じ範囲にする）。
    scope = db.widen_scope(sql, scope)
    try:
        columns, rows, truncated = db.run_select(sql, scope, params=params)
    except Exception as e:
        return _err(f"ツール '{tool.get('name')}' のSQL実行エラー: {e}")

    kind = tool.get("render") or "table"
    chart = tool.get("chart") or {}
    sample = rows[: config.SAMPLE_ROWS_FOR_LLM]

    # 取った表を預けて result_id を返す。組み込みツールは前からこうしているのに
    # ユーザー定義ツールだけ返しておらず、「このツールの結果をグラフにして」と
    # 言われてもAIには渡す手段が無かった（SQLはAIに見せていないので取り直せない）。
    # これがあれば、表で作ったツールでも後からグラフ・Excel・統計に回せる。
    rid = _results.put(scope, columns, rows, truncated,
                       sql=sql, label=f"{tool.get('name')}（ユーザー定義ツール）")
    llm_content = _json({
        "tool": tool.get("name"),
        "columns": columns,
        "row_count": len(rows),
        "truncated": truncated,
        "rows": [list(r) for r in sample],
        "result_id": rid,
        "note": ((f"全{len(rows)}行中 先頭{len(sample)}行を表示。"
                  if len(rows) > len(sample) else "")
                 + f"この結果は result_id '{rid}' で他のツールに渡せます"
                   "（グラフを描く・集計する・統計をかける・"
                   "Excel/CSV/PowerPoint/Word にする、など）。"),
    })

    if kind == "none":
        return {"ok": True, "llm_content": llm_content, "render": None}
    if kind in ("excel", "csv"):
        sheet = {"name": chart.get("title") or tool.get("name") or "Sheet1",
                 "columns": columns, "rows": rows,
                 "note": f"{config.MAX_RESULT_ROWS}行で切り詰め" if truncated else ""}
        base = chart.get("filename") or tool.get("name")
        try:
            if kind == "excel":
                data = excel.build([sheet], title=tool.get("description"))
                filename, mime = exports.safe_filename(base, "xlsx"), exports.XLSX_MIME
            else:
                enc = chart.get("encoding") or exports.DEFAULT_ENCODING
                data = exports.build_csv(columns, rows, enc)
                filename, mime = exports.safe_filename(base, "csv"), exports.CSV_MIME
        except Exception as e:
            return _err(f"ファイルの作成に失敗しました: {e}")
        return {"ok": True, "llm_content": _json({
            "status": "file_ready", "tool": tool.get("name"),
            "filename": filename, "columns": columns, "row_count": len(rows),
            "note": "ユーザーの画面に保存済み。",
        }), "render": {
            "role": "assistant", "kind": "file", "filename": filename,
            "mime": mime, "data": data, "sheets": [sheet],
        }}
    if kind == "chart":
        missing = [c for c in (chart.get("x"), chart.get("y")) if c and c not in columns]
        if missing:
            return _err(f"グラフ用の列が結果にありません: {missing} / 利用可能: {columns}")
        return {"ok": True, "llm_content": llm_content, "render": {
            "role": "assistant", "kind": "chart", "columns": columns, "rows": rows,
            "chart_type": chart.get("chart_type", "bar"),
            "x": chart.get("x"), "y": chart.get("y"), "color": chart.get("color"),
            "barmode": chart.get("barmode"), "title": chart.get("title", "") or tool.get("name"),
        }}
    if kind == "chart_dual":
        bar_y = chart.get("bar_y") or []
        line_y = chart.get("line_y") or []
        needed = [chart.get("x")] + list(bar_y) + list(line_y)
        missing = [c for c in needed if c and c not in columns]
        if missing:
            return _err(f"2軸グラフ用の列が結果にありません: {missing} / 利用可能: {columns}")
        return {"ok": True, "llm_content": llm_content, "render": {
            "role": "assistant", "kind": "chart_dual", "columns": columns, "rows": rows,
            "x": chart.get("x"), "bar_y": bar_y, "line_y": line_y,
            "left_title": chart.get("left_title"), "right_title": chart.get("right_title"),
            "title": chart.get("title", "") or tool.get("name"),
        }}
    return {"ok": True, "llm_content": llm_content, "render": {
        "role": "assistant", "kind": "table",
        "columns": columns, "rows": rows, "truncated": truncated,
    }}


def build_tools(entries: list[dict], admin: bool = False) -> list[dict]:
    """組み込み（無効化・説明上書きを反映）＋ユーザー定義 のツール定義一覧。

    admin=False のときは管理者専用のツールを渡さない。渡さなければ
    AIはその存在を知らないので、呼ばれること自体が起きない。
    """
    ov = custom_tools.builtin_overrides(entries)
    out = []
    for t in BUILTIN_TOOLS:
        name = t["function"]["name"]
        if name in ADMIN_TOOLS and not admin:
            continue
        o = ov.get(name) or {}
        if o.get("enabled") is False:
            continue
        if o.get("description"):
            t = {**t, "function": {**t["function"], "description": o["description"]}}
        out.append(t)
    for tool in custom_tools.collect_everywhere(entries):
        if not custom_tools.validate(tool, set()):     # 壊れた定義はAIに渡さない
            out.append(custom_tools.to_schema(tool))
    return out


def _required_params() -> dict:
    """スキーマで必須になっている引数。{ツール名: (引数名, ...)}"""
    return {t["function"]["name"]: tuple((t["function"].get("parameters") or {})
                                         .get("required") or ())
            for t in BUILTIN_TOOLS}


_REQUIRED = _required_params()


#: スキーマ上は必須でも、実処理が既定値を持っている引数。
#: ここまで止めると、これまで通っていた呼び出しが弾かれてしまう
#: （表題やファイル名は無ければツール側が付ける）。
_HAS_DEFAULT = {"title", "filename", "chart_type", "purpose"}


def _missing_required(name: str, args: dict) -> list[str]:
    """必須なのに渡ってこなかった引数（既定値を持つものは除く）。

    LLMは required を落とすことがある。そのまま実処理へ渡すと、
    pandas の "'[None] not in index'" のような内部エラーになって返る。
    これでは何を直せばよいか分からず、同じ呼び出しを繰り返して打ち切られる。
    ここで止めて、足りない引数の名前をそのまま返す。
    """
    if not isinstance(args, dict):
        return []
    out = []
    for k in _REQUIRED.get(name) or ():
        if k in _HAS_DEFAULT:
            continue
        v = args.get(k)
        # 0 や False は正しい値なので、空とみなすのは None と空の入れ物だけ
        if v is None or (isinstance(v, (str, list, dict, tuple)) and len(v) == 0):
            out.append(k)
    return out


def _string_list_params() -> dict:
    """スキーマ上「文字列の配列」になっている引数。{ツール名: {引数名, ...}}"""
    out: dict = {}
    for t in BUILTIN_TOOLS:
        fn = t["function"]
        props = (fn.get("parameters") or {}).get("properties", {})
        names = {k for k, v in props.items()
                 if v.get("type") == "array"
                 and (v.get("items") or {}).get("type") == "string"}
        if names:
            out[fn["name"]] = names
    return out


_LIST_PARAMS = _string_list_params()


def _coerce_lists(name: str, args: dict) -> dict:
    """配列で受ける引数に文字列が1つ来たら、要素1つの配列として扱う。

    LLMは列名が1つのとき index="地域" のように素の文字列で渡してくることがある。
    そのまま渡すと文字列が1文字ずつに散り、「'地' という列がありません」という
    人には意味の分からないエラーになる（日本語の列名だと必ずこうなる）。
    ここで直せば、13個ある同じ形の引数すべてに効く。
    """
    wanted = _LIST_PARAMS.get(name)
    if not wanted or not isinstance(args, dict):
        return args
    for k in wanted:
        v = args.get(k)
        if isinstance(v, str):
            args[k] = [v.strip()] if v.strip() else []
    return args


def _gather_sqls(node, scope: list[dict], acc: list) -> None:
    """呼び出しの引数から、実行されるSQLを全部拾う。

    レポートの節・Excelのシートのように入れ子の中にも sql がある。
    result_id で前の結果を使い回している場合は、その元のSQLを引く。
    """
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "sql" and isinstance(v, str) and v.strip():
                acc.append(v)
            elif k == "result_id" and isinstance(v, str) and v.strip():
                entry = _results.get(scope, v)
                if entry and entry.get("sql"):
                    acc.append(entry["sql"])
            else:
                _gather_sqls(v, scope, acc)
    elif isinstance(node, list):
        for v in node:
            _gather_sqls(v, scope, acc)


def _attach_verification(res: dict, sqls: list[str], scope: list[dict]) -> dict:
    """実行後の相互検証。触れたテーブルに関係する検算を突き合わせる。

    不一致があれば res["verify_alerts"] に積む。画面とLLMへの出し方は
    呼び出し側（chat側）が決める（同じ警告を会話の中で繰り返さないため）。
    検証自体の失敗で回答を止めない。
    """
    if not res.get("ok") or not sqls:
        return res
    try:
        alerts = verify.alerts_for(sqls, scope)
    except Exception as e:
        print(f"[verify] 検算でエラー（回答は続行）: {e}")
        return res
    if alerts:
        res["verify_alerts"] = alerts
    return res


def dispatch(name: str, arguments_json: str | None, scope: list[dict],
             entries: list[dict] | None = None, admin: bool = False) -> dict:
    try:
        args = json.loads(arguments_json) if arguments_json else {}
    except json.JSONDecodeError as e:
        return _err(f"ツール引数のJSON解析に失敗しました: {e}")

    # 渡していないツールを名指しで呼ばれても実行しない（守りは2箇所で持つ）
    if name in ADMIN_TOOLS and not admin:
        return _err(f"'{name}' は管理者だけが使えます。")

    args = _coerce_lists(name, args)
    missing = _missing_required(name, args)
    if missing:
        return _err(f"'{name}' の必須の引数が指定されていません: {'、'.join(missing)}。"
                    f"（{name} の必須引数は {'、'.join(_REQUIRED[name])}）"
                    "この引数を入れて呼び直してください。列名が分からないときは、"
                    "先に describe_table か run_sql_query で列を確認すること。")

    sqls: list = []
    _gather_sqls(args, scope, sqls)

    handler = _HANDLERS.get(name)
    if handler:
        try:
            return _attach_verification(handler(args, scope), sqls, scope)
        except Exception as e:  # ツールの例外でアプリを落とさない
            return _err(f"ツール '{name}' の実行でエラー: {e}")

    tool = next((t for t in custom_tools.collect_everywhere(entries or []) if t.get("name") == name), None)
    if tool is None:
        return {"ok": False, "llm_content": _json({"error": f"未知のツール: {name}"}), "render": None}
    try:
        sqls.append(render_sql(tool))
        return _attach_verification(_run_custom(tool, args, scope), sqls, scope)
    except Exception as e:
        return _err(f"ツール '{name}' の実行でエラー: {e}")
