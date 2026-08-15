"""相互検証（検算）。同じ数字を独立した2つの経路で計算して突き合わせる。

text-to-SQL の最大のリスクは「もっともらしいが間違っているSQL」ではなく、
「正しいSQLなのに、業務的には別の数字を指している」ことにある。
実際、demo_sales の「売上」は明細から数えると1.23億、請求から数えると0.85億で、
どちらのSQLも正しい。差の3,866万円は未請求の受注339件だった。
どのSQLを書くかで答えが1.5倍変わるのに、聞いた人にはそれが見えない。

そこで、カタログに「一致するはずの2つの式」を検算ルールとして登録しておき、
AIがそのテーブルに触れるSQLを実行するたびに突き合わせる。

  data/<DB>.db.meta.yaml:
    checks:
      - name: 入金と請求（入金済）の一致
        left:  {label: 入金の合計,          sql: SELECT SUM(amount) FROM demo_sales.payments}
        right: {label: 請求のうち入金済み,   sql: SELECT SUM(amount) FROM demo_sales.invoices WHERE paid_flag = 1}
        tolerance_pct: 0.1        # 許容差（%）。これ以内なら一致とみなす
        drilldown: SELECT ...     # 不一致のとき、差の実体を見せるSQL（任意）
        enabled: true

設計上の約束:
  * 左右のSQLは「1行1列のスカラ」を返すこと（SUM や COUNT）。
  * 検算は質問のたびに走るが、結果はデータの版（DBファイルのmtime）で
    キャッシュするので、実際に実行されるのはデータが変わった後の最初の1回だけ。
  * 壊れた検算ルール（SQLエラー）は黙って飛ばす。質問への回答を止めないため。
    ルール自体の点検は、カタログ画面の「検算」から人が行う。
"""
from __future__ import annotations

import re
from pathlib import Path

import db

#: 許容差の既定（%）。丸め誤差を拾わない程度に小さく。
DEFAULT_TOLERANCE_PCT = 0.5
#: 不一致時に内訳SQLで見せる行数。
DRILL_ROWS = 8
#: 検算結果のキャッシュ。キーは（ルールの中身, データの版）。
_cache: dict = {}
_CACHE_MAX = 300


# =============================================================================
# ルールの読み出し
# =============================================================================

def normalize(raw) -> list[dict]:
    """meta の checks をあるべき形に揃える。壊れた項目は落とす。"""
    out = []
    for c in (raw or []):
        if not isinstance(c, dict):
            continue
        left, right = c.get("left") or {}, c.get("right") or {}
        lsql = str(left.get("sql") or "").strip()
        rsql = str(right.get("sql") or "").strip()
        if not lsql or not rsql:
            continue
        try:
            tol = float(c.get("tolerance_pct", DEFAULT_TOLERANCE_PCT))
        except (TypeError, ValueError):
            tol = DEFAULT_TOLERANCE_PCT
        out.append({
            "name": str(c.get("name") or "検算").strip(),
            "left": {"label": str(left.get("label") or "左"), "sql": lsql},
            "right": {"label": str(right.get("label") or "右"), "sql": rsql},
            "tolerance_pct": max(0.0, tol),
            "drilldown": str(c.get("drilldown") or "").strip(),
            "enabled": c.get("enabled", True) is not False,
        })
    return out


def checks_for(scope: list[dict]) -> list[dict]:
    """選択中のDB群に登録されている検算ルール（有効なものだけ）。"""
    import catalog

    out = []
    for s in scope or []:
        meta = s.get("meta") or catalog.load_meta(s["path"])
        for c in normalize(meta.get("checks")):
            if c["enabled"]:
                out.append({**c, "owner": s.get("alias") or ""})
    return out


# =============================================================================
# 「このSQLはどのテーブルに触れているか」
# =============================================================================

def tables_in(sql: str, scope: list[dict]) -> set:
    """SQLが触れている (alias, table) の集合。名前の照合だけで判定する。"""
    found = set()
    for s in scope or []:
        alias = str(s.get("alias") or "")
        for t in (s.get("tables") or []):
            name = str(t)
            qualified = alias and re.search(
                r'(?<![\w."])' + re.escape(alias) + r'\s*\.\s*"?' + re.escape(name) + r'"?(?![\w])',
                sql, re.IGNORECASE)
            bare = re.search(r'(?<![\w."])"?' + re.escape(name) + r'"?(?![\w])',
                             sql, re.IGNORECASE)
            if qualified or bare:
                found.add((alias, name))
    return found


# =============================================================================
# 実行
# =============================================================================

def _scalar(sql: str, scope: list[dict]):
    """1行1列のSELECTを実行して数値を返す。数値でなければ ValueError。"""
    columns, rows, _ = db.run_select(sql, scope, max_rows=1)
    v = rows[0][0] if rows else None
    if v is None:
        return 0.0                      # SUMが空のときのNULLは0として扱う
    return float(v)


def _fingerprint(check: dict) -> tuple:
    return (check["name"], check["left"]["sql"], check["right"]["sql"],
            check["tolerance_pct"], check["drilldown"])


def _data_version(check: dict, scope: list[dict]) -> tuple:
    """検算が読むDBファイルの版。これが変わったら計算し直す。"""
    text = " ".join([check["left"]["sql"], check["right"]["sql"], check["drilldown"]])
    stamps = []
    for s in db.narrow_scope(text, scope):
        try:
            stamps.append((str(s["path"]), Path(s["path"]).stat().st_mtime_ns))
        except OSError:
            stamps.append((str(s["path"]), 0))
    return tuple(sorted(stamps))


def run_check(check: dict, scope: list[dict], use_cache: bool = True) -> dict:
    """検算を1本実行する。

    戻り値:
      {"ok_run": bool, "match": bool, "left": float, "right": float,
       "diff": float, "pct": float|None, "version": str,
       "drill": {"columns", "rows", "truncated"} | None, "error": str|None}
    """
    version = _data_version(check, scope)
    key = (_fingerprint(check), version)
    if use_cache and key in _cache:
        return _cache[key]

    res: dict = {"ok_run": False, "match": True, "left": None, "right": None,
                 "diff": None, "pct": None, "drill": None, "error": None,
                 "version": str(hash(version))}
    try:
        lv = _scalar(check["left"]["sql"], scope)
        rv = _scalar(check["right"]["sql"], scope)
    except Exception as e:
        res["error"] = str(e).splitlines()[0][:200]
        _remember(key, res)
        return res

    diff = lv - rv
    base = max(abs(lv), abs(rv))
    pct = (abs(diff) / base * 100) if base else 0.0
    match = pct <= check["tolerance_pct"]
    res.update({"ok_run": True, "match": match, "left": lv, "right": rv,
                "diff": diff, "pct": round(pct, 2)})

    if not match and check["drilldown"]:
        try:
            columns, rows, truncated = db.run_select(
                check["drilldown"], scope, max_rows=DRILL_ROWS)
            res["drill"] = {"columns": columns,
                            "rows": [list(r) for r in rows],
                            "truncated": truncated}
        except Exception as e:
            res["drill"] = {"error": str(e).splitlines()[0][:160]}

    _remember(key, res)
    return res


def _remember(key, res) -> None:
    if len(_cache) > _CACHE_MAX:
        _cache.clear()
    _cache[key] = res


def clear_cache() -> None:
    """テスト用。"""
    _cache.clear()


# =============================================================================
# 質問への割り込み（ツール実行後に呼ばれる）
# =============================================================================

def alerts_for(sql_texts: list[str], scope: list[dict]) -> list[dict]:
    """実行されたSQL群に関係する検算を走らせ、不一致だけを返す。

    一致した検算・実行できなかった検算は何も言わない
    （毎回「問題ありません」と言われても読まれなくなるだけ）。
    """
    texts = [t for t in (sql_texts or []) if t and t.strip()]
    if not texts or not scope:
        return []
    try:
        checks = checks_for(scope)
    except Exception:
        return []
    if not checks:
        return []

    touched = set()
    for t in texts:
        touched |= tables_in(t, scope)
    if not touched:
        return []

    alerts = []
    for check in checks:
        involved = tables_in(check["left"]["sql"] + " " + check["right"]["sql"], scope)
        if not (involved & touched):
            continue
        res = run_check(check, scope)
        if not res["ok_run"] or res["match"]:
            continue
        alerts.append({
            "key": f"verify||{check['owner']}||{check['name']}||{res['version']}",
            "name": check["name"],
            "left_label": check["left"]["label"], "left": res["left"],
            "right_label": check["right"]["label"], "right": res["right"],
            "diff": res["diff"], "pct": res["pct"],
            "tolerance_pct": check["tolerance_pct"],
            "drill": res["drill"],
        })
    return alerts


def _fmt(v) -> str:
    if v is None:
        return "—"
    return f"{v:,.4f}".rstrip("0").rstrip(".") if v % 1 else f"{int(v):,}"


def llm_note(alert: dict) -> dict:
    """LLMのツール結果に混ぜる、検算の注意書き。"""
    note = {
        "check": alert["name"],
        alert["left_label"]: alert["left"],
        alert["right_label"]: alert["right"],
        "difference": alert["diff"],
        "difference_pct": alert["pct"],
        "instruction": ("この2つの数字は一致するはずですが食い違っています。"
                        "回答では、どちらの数字を使ったのかと、この差異があることを"
                        "必ず注記してください。差異の理由を推測で断定しないこと。"),
    }
    drill = alert.get("drill") or {}
    if drill.get("rows"):
        note["difference_detail_sample"] = {
            "columns": drill["columns"], "rows": drill["rows"][:3]}
    return note


def render_item(alert: dict) -> dict:
    """画面に出す検算カード。分析結果と同じ report の形で描ける。"""
    tables = [{
        "name": "2つの経路の比較",
        "columns": ["経路", "値"],
        "rows": [(alert["left_label"], alert["left"]),
                 (alert["right_label"], alert["right"]),
                 ("差", alert["diff"]),
                 ("差の割合", f"{alert['pct']}%（許容 {alert['tolerance_pct']}%）")],
    }]
    notes = [f"「{alert['left_label']}」と「{alert['right_label']}」は一致するはず"
             f"ですが、{_fmt(abs(alert['diff']))}（{alert['pct']}%）食い違っています。"]
    drill = alert.get("drill") or {}
    if drill.get("rows"):
        tables.append({"name": "差の内訳（先頭のみ）",
                       "columns": drill["columns"], "rows": drill["rows"]})
        notes.append("内訳の表は差の実体の一部です。全体はデータカタログの"
                     "「検算」で確認できます。")
    elif drill.get("error"):
        notes.append(f"内訳SQLは実行できませんでした: {drill['error']}")
    notes.append("この検算ルールはデータカタログの「用語集・例文 → 検算」で"
                 "管理されています。差が正しい業務状態なら、許容差を広げるか"
                 "ルールを無効にしてください。")
    return {"role": "assistant", "kind": "report",
            "title": f"⚠ 検算: {alert['name']}",
            "tables": tables, "notes": notes,
            "verify_key": alert["key"]}
