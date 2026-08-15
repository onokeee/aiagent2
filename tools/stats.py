"""統計と試算。advanced.py の分析関数をツールとして公開する。"""
from __future__ import annotations


import advanced
from .common import _analysis_tool, _err, _report_result, _select_for


_hypothesis_test = _analysis_tool(lambda a, c, r: advanced.hypothesis_test(
    c, r, a.get("method"), value_col=a.get("value_col"), group_col=a.get("group_col"),
    value_col2=a.get("value_col2"), popmean=float(a.get("popmean") or 0),
    expected=a.get("expected"), alternative=a.get("alternative") or "two-sided"))


_regression = _analysis_tool(lambda a, c, r: advanced.regression(
    c, r, a.get("target"), a.get("features") or [], method=a.get("method") or "ols",
    predict=a.get("predict")))


_distribution_analysis = _analysis_tool(lambda a, c, r: advanced.distribution(
    c, r, a.get("target"), bins=int(a.get("bins") or 20), fit=a.get("fit"),
    group_col=a.get("group_col")))


_forecast = _analysis_tool(lambda a, c, r: advanced.forecast(
    c, r, a.get("time_col"), a.get("value_col"), periods=int(a.get("periods") or 6),
    method=a.get("method") or "auto",
    season_length=int(a["season_length"]) if a.get("season_length") else None,
    exog=a.get("exog")))


_timeseries_analysis = _analysis_tool(lambda a, c, r: advanced.timeseries(
    c, r, a.get("time_col"), a.get("value_col"), window=int(a.get("window") or 3),
    season_length=int(a["season_length"]) if a.get("season_length") else None))


_bootstrap_estimate = _analysis_tool(lambda a, c, r: advanced.bootstrap(
    c, r, a.get("target"), statistic=a.get("statistic") or "mean",
    trials=int(a.get("trials") or 5000), group_col=a.get("group_col")))


# k は "auto" も受けるので、ここで数値に変換しない
_clustering = _analysis_tool(lambda a, c, r: advanced.clustering(
    c, r, a.get("features") or [], k=a.get("k") or 3,
    label_col=a.get("label_col"), categorical=a.get("categorical")))


_abc_analysis = _analysis_tool(lambda a, c, r: advanced.abc_analysis(
    c, r, a.get("label_col"), a.get("value_col"), thresholds=a.get("thresholds")))


def _monte_carlo_simulation(args: dict, scope: list[dict]) -> dict:
    columns = rows = None
    if args.get("sql"):
        try:
            columns, rows, _ = _select_for(args, scope)
        except advanced.AnalysisError as e:
            return _err(str(e))
        except Exception as e:
            return _err(f"実データの取得に失敗しました: {e}")
    try:
        res = advanced.monte_carlo(
            args.get("formula", ""), args.get("variables") or {},
            trials=int(args.get("trials") or 10000), columns=columns, rows=rows,
            targets=args.get("targets"))
    except advanced.AnalysisError as e:
        return _err(str(e))
    except Exception as e:
        return _err(f"シミュレーションに失敗しました: {e}")
    if args.get("title"):
        res["title"] = args["title"]
    return _report_result(res)


def _scenario_analysis(args: dict, scope: list[dict]) -> dict:
    try:
        res = advanced.scenario(args.get("formula", ""), args.get("scenarios") or {},
                                base=args.get("base"))
    except advanced.AnalysisError as e:
        return _err(str(e))
    except Exception as e:
        return _err(f"シナリオ分析に失敗しました: {e}")
    if args.get("title"):
        res["title"] = args["title"]
    return _report_result(res)

HANDLERS = {
    "hypothesis_test": _hypothesis_test,
    "regression": _regression,
    "distribution_analysis": _distribution_analysis,
    "forecast": _forecast,
    "timeseries_analysis": _timeseries_analysis,
    "monte_carlo_simulation": _monte_carlo_simulation,
    "scenario_analysis": _scenario_analysis,
    "bootstrap_estimate": _bootstrap_estimate,
    "clustering": _clustering,
    "abc_analysis": _abc_analysis,
}

# scenario_analysis と monte_carlo_simulation は SQL が任意なので含めない
SQL_TOOLS = {"hypothesis_test", "regression", "distribution_analysis", "forecast",
             "timeseries_analysis", "bootstrap_estimate", "clustering", "abc_analysis"}
