"""ツールの実処理が共通で使う小道具。

LLMへ返すJSONの組み立てと、データを用意して advanced.py に渡す定型。
データは sql から取ることも、前のツールが返した result_id を指すこともできる
（results.py 参照）。
"""
from __future__ import annotations

import json

import advanced
import config
import db
from . import results


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _err(message: str) -> dict:
    return {
        "ok": False,
        "llm_content": _json({"error": message}),
        "render": {"role": "assistant", "kind": "error", "message": message},
    }


def _total_rows(sql: str, scope: list[dict]) -> int | None:
    """上限で切り詰められたとき、本当は何行あるのかを数える。

    「全部見た」と誤解したまま結論を書かせないための添え物。
    重いSQLだと数え直しも失敗し得るので、そのときは黙って諦める。
    """
    try:
        _, rows, _ = db.run_select(f"SELECT COUNT(*) FROM ({sql.strip().rstrip(';')})",
                                   scope, max_rows=1)
        return int(rows[0][0])
    except Exception:
        return None


def fetch(spec: dict, scope: list[dict], *, label: str | None = None,
          max_rows: int | None = None):
    """ツールが使うデータを用意する。

    spec に result_id があれば前の結果を使い、無ければ sql を実行する。
    どちらの道でも result_id を返すので、呼び出し側はそれをLLMに伝えて
    次のツールで使い回せるようにする。

    max_rows は取得の上限。省略時は画面向けの MAX_RESULT_ROWS(2,000)。
    ファイル出力は EXPORT_MAX_ROWS を渡してくる。「表とグラフは2,000行で足りるが、
    CSVは全行欲しい」ので、道具ごとに上限が違う。

    戻り値: (columns, rows, truncated, result_id, total_rows)
      total_rows は切り詰めが起きたときだけ入る（本当の総件数）。
    """
    rid = str((spec or {}).get("result_id") or "").strip()
    if rid:
        entry = results.get(scope, rid)
        if entry is None:
            raise advanced.AnalysisError(
                f"result_id '{rid}' のデータが見つかりません。"
                "古くなって捨てられたか、別の会話の結果です。"
                "sql を指定して取り直してください。")
        # 預かっているのは2,000行に切り詰めた結果のことがある。ファイル出力のように
        # もっと大きな上限で全行欲しい呼び出しなら、預けたときのSQLで取り直す。
        # （「集計→CSVに」の流れで、CSVだけ2,000行で欠ける事故を防ぐ）
        if (max_rows and entry["truncated"] and entry.get("sql")
                and max_rows > len(entry["rows"])):
            sql = entry["sql"]
            wide = db.widen_scope(sql, scope)
            columns, rows, truncated = db.run_select(sql, wide, max_rows=max_rows)
            return columns, rows, truncated, rid, (
                _total_rows(sql, wide) if truncated else None)
        return entry["columns"], entry["rows"], entry["truncated"], rid, None

    sql = str((spec or {}).get("sql") or "").strip()
    if not sql:
        raise advanced.AnalysisError(
            "sql と result_id のどちらも指定されていません。"
            "新しくデータを取るなら sql を、前のツールの結果を使うなら result_id を指定してください。")
    # スコープは質問ごとの自動判定なので、例文由来のSQLなどが範囲外のDBを
    # 名指しすることがある。必要なぶんは繋いで実行する（読み取り専用のまま）。
    scope = db.widen_scope(sql, scope)
    columns, rows, truncated = db.run_select(sql, scope, max_rows=max_rows)
    # 使い回し用の預かりは、後続の表・グラフに足りる行数まで。
    # 100万行をそのまま預けると、これ1つで置き場(MAX_CELLS)を食い潰すため。
    keep = rows[: config.MAX_RESULT_ROWS]
    rid = results.put(scope, columns, keep, truncated or len(rows) > len(keep),
                      sql=sql, label=label)
    return columns, rows, truncated, rid, (_total_rows(sql, scope) if truncated else None)


def source_note(row_count: int, truncated: bool, total: int | None,
                cap: int | None = None) -> dict:
    """LLMに渡す「元データの規模」。切り詰めのときは実際の件数も添える。"""
    out = {"source_row_count": row_count, "source_truncated": bool(truncated)}
    if truncated:
        out["source_total_rows"] = total
        out["warning"] = (
            f"上限 {cap or config.MAX_RESULT_ROWS:,} 行で切り詰めました。"
            + (f"実際は {total:,} 行あります。" if total else "")
            + "この結果は全体の一部です。全体を語るなら、SQL側で"
              "GROUP BY で集計するか、条件を絞って取り直してください。")
    return out


def _select_for(args: dict, scope: list[dict]):
    """分析ツール共通の入口。データを用意して (columns, rows) を返す。"""
    columns, rows, truncated, rid, total = fetch(args, scope)
    if not rows:
        raise advanced.AnalysisError("データが0行でした。抽出条件を見直してください。")
    return columns, rows, truncated, rid, total


def _report_result(res: dict, *, source_rows: int | None = None,
                   truncated: bool = False, total: int | None = None,
                   result_id: str | None = None,
                   scope: list[dict] | None = None,
                   extra: dict | None = None) -> dict:
    """advanced.py の戻り値を、画面用アイテムとLLM用の要約に変換する。

    LLMには表を丸ごと渡さない。所見(notes)と各表の先頭数行があれば
    十分に説明でき、トークンも節約できる。
    表そのものを次のツールへ渡せるよう、1つ目の表は result_id を付けて預ける。
    """
    tables = res.get("tables") or []
    llm_tables = []
    for i, t in enumerate(tables):
        rows = t.get("rows") or []
        item = {
            "name": t.get("name"), "columns": t.get("columns"),
            "row_count": len(rows),
            "rows": [list(r) for r in rows[: config.SAMPLE_ROWS_FOR_LLM]],
        }
        # 分析結果の表もグラフやレポートの材料になる。指せるようにしておく。
        if scope is not None and rows:
            item["result_id"] = results.put(scope, t.get("columns") or [], rows,
                                            label=f"{res.get('title')} / {t.get('name')}")
        llm_tables.append(item)
    payload = {"status": "analysis_ready", "title": res.get("title"),
               "notes": res.get("notes") or [], "tables": llm_tables,
               "meta": res.get("meta") or {}}
    if source_rows is not None:
        payload.update(source_note(source_rows, truncated, total))
    if result_id:
        payload["source_result_id"] = result_id
    payload.update(extra or {})
    return {
        "ok": True,
        "llm_content": _json(payload),
        "render": {"role": "assistant", "kind": "report", "title": res.get("title"),
                   "tables": tables, "notes": res.get("notes") or []},
    }


def _analysis_tool(fn):
    """データを用意して advanced.py の関数に渡す、共通のかたち。"""
    def run(args: dict, scope: list[dict]) -> dict:
        try:
            columns, rows, truncated, rid, total = _select_for(args, scope)
            res = fn(args, columns, rows)
        except advanced.AnalysisError as e:
            return _err(str(e))
        except Exception as e:
            return _err(f"分析に失敗しました: {e}")
        if args.get("title"):
            res["title"] = args["title"]
        return _report_result(res, source_rows=len(rows), truncated=truncated,
                              total=total, result_id=rid, scope=scope)
    return run
