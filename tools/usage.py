"""このアプリ自身の使われ方を調べるツール（読むだけ）。

usage.py の集計をLLMに公開する。材料はチャット履歴と取り込みの記録なので、
分析対象のDBを選んでいなくても答えられる（SQLでは答えられない話でもある）。

他人の質問文まで見えるため、管理者にだけ渡す。tools/files.py と同じ扱い。
"""
from __future__ import annotations

import usage
from .common import _err, _report_result


def _analyze_usage(args: dict, scope: list[dict]) -> dict:
    days = args.get("days")
    try:
        res = usage.analyze(str(args.get("method") or "summary"),
                            days=int(days) if days else None,
                            user=(args.get("user") or "").strip() or None)
    except ValueError as e:
        return _err(str(e))
    except Exception as e:
        return _err(f"利用状況の集計に失敗しました: {e}")

    if args.get("title"):
        res["title"] = args["title"]
    # scope を渡して表に result_id を付ける。グラフ化やExcel出力にそのまま繋げられる。
    return _report_result(res, scope=scope)


HANDLERS = {"analyze_usage": _analyze_usage}

# SQLは受け取らない（材料はDBではなく履歴ファイル）
SQL_TOOLS: set = set()

# 他の利用者の質問・失敗まで見えるので管理者だけに渡す。
ADMIN_TOOLS = {"analyze_usage"}
