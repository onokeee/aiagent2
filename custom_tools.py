"""ユーザーがUIから定義するツール（SQLテンプレート型）。

ツール1つは「名前 + 説明 + パラメータ定義 + SQLテンプレート + 出力形式」で表す。
Pythonコードは書かせない。SQLは既存の SELECT専用ガード（db.run_select）を通し、
パラメータは SQLite のバインド変数として渡すのでSQLインジェクションは起こらない。

保存先は各DBの .meta.yaml の `tools:`（そのDBを選択したときだけAIに渡る）。

  tools:
    - name: monthly_sales
      description: 指定年の月別売上を返す。「今年の売上推移」などで使う。
      parameters:
        - name: year
          type: string
          description: "対象年 'YYYY'"
          required: true
      sql: |
        SELECT strftime('%Y-%m', o.order_date) AS 月, ... WHERE ... = :year ...
      render: chart          # table | chart | chart_dual | none
      chart: {chart_type: line, x: 月, y: 売上, title: 月別売上}
      enabled: true

組み込みツールの有効/無効と説明文の上書きは .meta.yaml の `builtin_tools:` に持つ。

  builtin_tools:
    plot_dual_axis: {enabled: false}
    run_sql_query: {description: "…独自の言い回しに差し替え…"}
"""
from __future__ import annotations

import re

RENDER_KINDS = ("table", "chart", "chart_dual", "excel", "csv", "none")
PARAM_TYPES = ("string", "integer", "number", "boolean")

# ツール名はOpenAIのfunction名の制約に合わせる（英数字とアンダースコア）
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,47}$")
# SQL中のバインド変数 :name を拾う（:: は型キャストなので除外）
_BIND_RE = re.compile(r"(?<!:):([A-Za-z_][A-Za-z0-9_]*)")

# 組み込みツール名（ユーザー定義ツールと衝突させない）。
# 以前はここに4つだけ列挙していたが、実際の組み込みは38個ある。
# 漏れた名前（forecast など）でツールを作れてしまい、AIに同じ名前の関数が
# 2つ渡って、しかも実行されるのは組み込み側だけ、という不整合が起きていた。
# 一覧は tools.schemas が持っているので、そちらから引く（循環importを避けて遅延）。
def builtin_names() -> set:
    from tools.schemas import BUILTIN_TOOLS
    return {t["function"]["name"] for t in BUILTIN_TOOLS}


def safe_name(candidate: str, taken=()) -> str:
    """どんな文字列からでも、使えるツール名を作る。

    AIが起こした名前や日本語の説明が元でも、function名の制約
    （英字始まり・英数字と_・48文字以内）に収め、既存とも組み込みとも
    衝突しない名前にする。ユーザーに名前で悩ませないための道具。
    """
    ascii_ = re.sub(r"[^a-z0-9_]+", "_", str(candidate or "").lower()).strip("_")
    base = ascii_[:40] if re.match(r"^[a-z]", ascii_) else ""
    if not base:
        base = "tool"
    used = {str(t).lower() for t in taken} | {n.lower() for n in builtin_names()}
    name, n = base, 2
    while name.lower() in used:
        name = f"{base}_{n}"
        n += 1
    return name


def bind_names(sql: str) -> list[str]:
    """SQLテンプレートに現れるバインド変数名（重複なし・出現順）。"""
    seen, out = set(), []
    for m in _BIND_RE.finditer(sql or ""):
        if m.group(1) not in seen:
            seen.add(m.group(1))
            out.append(m.group(1))
    return out


def validate(tool: dict, existing_names: set = frozenset()) -> list[str]:
    """ツール定義を検証し、問題点のリストを返す（空なら妥当）。"""
    errs: list[str] = []
    name = str(tool.get("name") or "").strip()
    if not name:
        errs.append("ツール名は必須です。")
    elif not _NAME_RE.match(name):
        errs.append("ツール名は英字で始まる英数字とアンダースコアのみ（48文字以内）にしてください。")
    elif name in builtin_names():
        errs.append(f"'{name}' は組み込みツールと同じ名前です。別の名前にしてください。")
    elif name in existing_names:
        errs.append(f"'{name}' は既に存在します。")

    if not str(tool.get("description") or "").strip():
        errs.append("説明は必須です（AIがこのツールを使うかどうかの判断材料になります）。")

    sql = str(tool.get("sql") or "").strip()
    if not sql:
        errs.append("SQLは必須です。")

    params = tool.get("parameters") or []
    pnames = []
    for i, p in enumerate(params, start=1):
        pn = str((p or {}).get("name") or "").strip()
        if not pn:
            errs.append(f"パラメータ{i}: 名前が空です。")
            continue
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", pn):
            errs.append(f"パラメータ '{pn}': 英数字とアンダースコアのみ使えます。")
        if pn in pnames:
            errs.append(f"パラメータ '{pn}' が重複しています。")
        pnames.append(pn)
        if (p or {}).get("type") not in PARAM_TYPES:
            errs.append(f"パラメータ '{pn}': 型は {', '.join(PARAM_TYPES)} のいずれかにしてください。")

    # SQL中の :name と パラメータ定義の対応
    if sql:
        binds = set(bind_names(sql))
        for miss in sorted(binds - set(pnames)):
            errs.append(f"SQLに :{miss} がありますが、パラメータが定義されていません。")
        for unused in sorted(set(pnames) - binds):
            errs.append(f"パラメータ '{unused}' がSQL中で使われていません（:{unused} と書きます）。")

    render = tool.get("render") or "table"
    if render not in RENDER_KINDS:
        errs.append(f"出力形式は {', '.join(RENDER_KINDS)} のいずれかにしてください。")
    if render == "chart":
        import charts
        c = tool.get("chart") or {}
        ct = str(c.get("chart_type") or "").strip()
        if not ct:
            errs.append("グラフ出力には chart.chart_type が必要です。")
        elif ct not in charts.CHART_TYPES:
            errs.append(f"未対応のグラフ種別です: {ct}")
        else:
            for k in charts.required_fields(ct):
                v = c.get(k)
                if (not v) if k != "path" else (not list(v or [])):
                    errs.append(f"{ct} には chart.{k} が必要です。")
    if render == "chart_dual":
        c = tool.get("chart") or {}
        if not str(c.get("x") or "").strip():
            errs.append("2軸グラフには chart.x が必要です。")
        if not (c.get("bar_y") or []):
            errs.append("2軸グラフには chart.bar_y（棒にする列）が1つ以上必要です。")
        if not (c.get("line_y") or []):
            errs.append("2軸グラフには chart.line_y（折れ線にする列）が1つ以上必要です。")
    return errs


def to_schema(tool: dict) -> dict:
    """ユーザー定義ツール → OpenAI function calling のJSON Schema。"""
    props, required = {}, []
    for p in (tool.get("parameters") or []):
        pn = str(p.get("name") or "").strip()
        if not pn:
            continue
        props[pn] = {"type": p.get("type") or "string",
                     "description": str(p.get("description") or "")}
        if p.get("required", True):
            required.append(pn)
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": str(tool.get("description") or ""),
            "parameters": {"type": "object", "properties": props, "required": required},
        },
    }


def coerce_params(tool: dict, args: dict) -> dict:
    """LLMが渡してきた引数を、定義された型に寄せてバインド用の辞書にする。"""
    out = {}
    for p in (tool.get("parameters") or []):
        pn = str(p.get("name") or "").strip()
        if not pn:
            continue
        v = args.get(pn)
        if v is None:
            out[pn] = None
            continue
        t = p.get("type") or "string"
        try:
            if t == "integer":
                out[pn] = int(v)
            elif t == "number":
                out[pn] = float(v)
            elif t == "boolean":
                out[pn] = 1 if (v is True or str(v).lower() in ("true", "1", "yes")) else 0
            else:
                out[pn] = str(v)
        except (TypeError, ValueError):
            raise ValueError(f"パラメータ '{pn}' を {t} として解釈できません: {v!r}")
    return out


def collect(entries: list[dict]) -> list[dict]:
    """選択スコープのDB群から、有効なユーザー定義ツールを集める。

    entries: [{"alias", "meta", ...}, ...]
    戻り値の各要素は tool 定義に "owner"（DBエイリアス）を足したもの。
    同名が複数DBにあった場合は最初のものだけを採用する。
    """
    out, seen = [], set()
    for e in entries:
        for t in (e.get("meta", {}).get("tools") or []):
            if not isinstance(t, dict):
                continue
            name = str(t.get("name") or "").strip()
            if not name or name in seen or t.get("enabled") is False:
                continue
            seen.add(name)
            out.append({**t, "owner": e["alias"]})
    return out


def builtin_overrides(entries: list[dict]) -> dict:
    """組み込みツールの有効/無効・説明上書きを合成する。

    無効化はどれか1つのDBで無効なら無効（安全側）。説明は最初に見つかったものを採用。
    """
    merged: dict[str, dict] = {}
    for e in entries:
        for name, ov in (e.get("meta", {}).get("builtin_tools") or {}).items():
            if not isinstance(ov, dict):
                continue
            cur = merged.setdefault(name, {})
            if ov.get("enabled") is False:
                cur["enabled"] = False
            desc = str(ov.get("description") or "").strip()
            if desc and not cur.get("description"):
                cur["description"] = desc
    return merged
