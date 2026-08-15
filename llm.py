"""OpenAI / OpenAI互換API クライアント / system prompt 生成 / AI下書き。"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from openai import OpenAI

import catalog
import config
import custom_tools
import tools

_client: OpenAI | None = None


def is_configured() -> bool:
    return bool(config.OPENAI_BASE_URL and config.OPENAI_API_KEY)


def client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            base_url=config.OPENAI_BASE_URL or None,
            api_key=config.OPENAI_API_KEY or "not-set",
        )
    return _client


# --- モデルごとの作法の違いを吸収する ---------------------------------------------
#
# 同じ OpenAI互換API でも、モデルによって受け付ける引数が違う。実測した例:
#   gpt-5.6-sol : ツールを使うなら reasoning_effort='none' が必須。temperature も既定値のみ
#   gpt-4o 系   : reasoning_effort を送ると「Unrecognized request argument」で拒否
# モデル名の一覧を持って場合分けすると、ゲートウェイや新モデルのたびに保守が要る。
# そこで「1回投げて、断られた理由を読んで直して、覚える」方式にする。
# 400 は推論前に弾かれるので、やり直しても費用はかからない。
#
# 覚えた内容はプロセスの寿命だけ持つ。再起動後の最初の1回だけ余計に往復する。

#: モデル名 -> 学習した調整 {"set": {引数: 値}, "drop": {引数, ...}}
_QUIRKS: dict[str, dict] = {}
#: 1回の呼び出しで引数を直しにいく上限。無限に投げ続けないための歯止め。
_MAX_FIX = 4


def _apply_quirks(kwargs: dict) -> dict:
    q = _QUIRKS.get(str(kwargs.get("model") or ""))
    if not q:
        return kwargs
    out = {k: v for k, v in kwargs.items() if k not in q.get("drop", set())}
    out.update(q.get("set", {}))
    return out


def _learn(model: str, *, set_: dict | None = None, drop: str | None = None) -> None:
    q = _QUIRKS.setdefault(model, {"set": {}, "drop": set()})
    if set_:
        q["set"].update(set_)
    if drop:
        q["drop"].add(drop)
        q["set"].pop(drop, None)
    print(f"[llm] {model} の呼び出し方を調整しました: "
          f"set={q['set']} drop={sorted(q['drop'])}")


def _fix_for(message: str, kwargs: dict) -> tuple | None:
    """エラー文から、次に試す直し方を決める。戻り値: (set_, drop) か None。"""
    low = message.lower()

    # ツールと推論モードを同時に使えない → 推論を切れば chat/completions で通る
    if "reasoning_effort" in low and "function tools" in low:
        return ({"reasoning_effort": "none"}, None)
    # 値が受け付けられない（'minimal' など）→ 'none' に寄せる
    if "reasoning_effort" in low and "does not support" in low:
        return ({"reasoning_effort": "none"}, None)
    # そもそもこの引数を知らないモデル → 落とす
    if "reasoning_effort" in low and "unrecognized" in low:
        return (None, "reasoning_effort")
    # temperature / top_p が既定値しか許されないモデル → 落として既定に任せる
    for name in ("temperature", "top_p"):
        if f"'{name}'" in low and ("does not support" in low
                                   or "only the default" in low):
            return (None, name)
    # 新しいモデルは max_tokens ではなく max_completion_tokens を使う
    if "max_tokens" in low and "max_completion_tokens" in low:
        v = kwargs.get("max_tokens")
        if v is not None:
            return ({"max_completion_tokens": v}, "max_tokens")
    return None


def _create(**kwargs):
    """chat.completions.create の呼び出し口。

    モデルが受け付けない引数を、エラーの内容を見て直しながら投げ直す。
    直し方が分からないエラーはそのまま投げる（画面にそのまま出す）。
    """
    model = str(kwargs.get("model") or "")
    attempt = _apply_quirks(kwargs)
    for _ in range(_MAX_FIX):
        try:
            return client().chat.completions.create(**attempt)
        except Exception as e:
            fix = _fix_for(str(e), attempt)
            if fix is None:
                raise
            set_, drop = fix
            if set_ and all(attempt.get(k) == v for k, v in set_.items()):
                raise                      # 同じ直しを繰り返している
            if drop and drop not in attempt:
                raise
            _learn(model, set_=set_, drop=drop)
            attempt = _apply_quirks(kwargs)
    return client().chat.completions.create(**attempt)


# --- system prompt -----------------------------------------------------------

def build_system_prompt(scope: list[dict], admin: bool = False) -> str:
    """選択スコープのデータカタログを埋め込んだ system prompt を組み立てる。

    admin は「管理者だけに渡すツール」を一覧に載せるかどうか。
    渡していないツールを説明に書くと、AIが呼ぼうとして失敗するだけになる。
    """
    aliases = [s["alias"] for s in scope]
    if len(aliases) > 1:
        naming = (f"複数のDBが選択されています（{', '.join(aliases)}）。"
                  "テーブル名は必ず『エイリアス.テーブル名』で修飾すること"
                  f"（例: {aliases[0]}.xxx）。DBをまたぐ JOIN も可能。")
    elif aliases:
        naming = (f"選択中のDBは {aliases[0]} の1つ。テーブル名はそのまま書いてよい"
                  f"（{aliases[0]}.テーブル名 と修飾しても可）。")
    else:
        naming = "現在DBが選択されていません。サイドバーでDBとテーブルを選ぶようユーザーに案内すること。"

    # 実際に渡すツール一覧（無効化・説明の上書き・ユーザー定義ツールを反映）
    lines = []
    for t in tools.build_tools(scope, admin=admin):
        fn = t["function"]
        args = ", ".join((fn.get("parameters") or {}).get("properties", {}).keys())
        lines.append(f"- {fn['name']}({args}) : {fn['description']}")
    custom = custom_tools.collect(scope)
    if custom:
        lines.append("※ 上記のうち次はこのDB専用に用意された専用ツールです。"
                     "目的が合致するときは自分でSQLを書かずにこちらを優先して使ってください: "
                     + ", ".join(t["name"] for t in custom))
    tool_list = "\n".join(lines)

    return f"""あなたはSQLiteデータベースの分析アシスタントです。
読み取り専用(SELECTのみ)でデータベースにアクセスできます。

# 振る舞い
- ユーザーの質問に答えるため、必要に応じてツールを呼び出し、必ず実データに基づいて回答する。
- 推測で数値を答えてはいけない。データが必要なら run_sql_query を使う。
- 列の意味や値の実体が不確かなら、SQLを書く前に describe_table で確認する。
- ツールを使うか・どのSQLを書くかはあなたが判断する(挨拶や一般的な雑談ならツール不要)。
- 回答は日本語。まず結論、次に根拠(表やグラフの要点)を簡潔に述べる。
- 後述の「業務用語」に載っている言葉が質問に出たら、必ずその定義に従う。自分の常識で解釈し直さない。
  - 「SQL式:」が書かれている用語は、その式をそのまま WHERE や SELECT に埋め込む。
  - SQL式が無く説明文だけの用語は、その説明と列情報から自分でSQLを組み立てる。
    どの列をどう使ったかを回答の中で一言添える（人が誤りに気づけるようにするため）。
- ツールの結果に verification_warnings（検算の不一致）が入っていることがある。
  これは「同じ数字を別の経路で数えたら食い違った」という自動検算の結果で、
  あなたのSQLの誤りとは限らない。入っていたら、回答の末尾で
  「どの数字を使ったか」と「別の経路では値が異なること」を必ず1〜2文で注記する。
  差異の原因は、検算結果に示された内訳の範囲でだけ述べ、推測で断定しない。

# 可視化の方針（チャットにグラフを描く）
- ユーザーが「グラフ」「可視化」「チャート」「推移」「トレンド」「割合」「内訳」「分布」等を求めたら、必ず plot_* のツールを呼んでグラフを描く。
- 明示が無くても、結果が次に該当するなら積極的にグラフにする。目的でツールを選ぶ：
  - 項目どうしの比較・順位 → plot_comparison（bar / hbar / stacked_bar / pareto / radar など）
  - 時系列・推移 → plot_trend（line / step / area / calendar など）
  - 構成比・内訳・増減の要因 → plot_composition（pie / donut / treemap / funnel / waterfall / sankey）
  - ばらつき・分布 → plot_distribution（histogram / box / violin など）
  - 2つ以上の項目の関係 → plot_relationship（scatter / bubble / heatmap など）
  - 1つの数字を大きく・目標との対比 → plot_kpi（indicator / gauge / bullet）
- sql は集計済み(GROUP BY)にし、x/y にする列を AS で明示する。色分けは color に列名を渡す。
- 棒グラフの積み方は種別で指定する：「積み上げ」なら chart_type="stacked_bar"、「横並び」「比較」なら "bar"。
- 「2軸」「二軸」「棒と折れ線」「件数と比率を一緒に」など、単位の異なる2指標を重ねたい時は plot_dual_axis を使う。
  bar_y(左軸=棒, 件数など) と line_y(右軸=折れ線, 比率など) に列名を渡す。
- 必要なら run_sql_query で数値を確認しつつ、可視化は plot_* で別途描く（両方呼んでよい）。

# SQLで書けないこと（必ず専用ツールを使う）
このSQLiteには STDDEV / VARIANCE / MEDIAN / CORR / PERCENTILE / SQRT / POWER が無く、
PIVOT構文も無い。次はSQLで計算しようとせず、必ずツールを使うこと。
- 「クロス集計」「行に○○・列に△△」「マトリクスで」 → pivot_table
  （sql では集計せず、必要な列を返すだけにする。集計はツールが行う）
- 「相関」「中央値」「ばらつき」「標準偏差」「四分位」「分布の要約」 → analyze_stats
- 「外れ値」「異常値」「突出しているもの」 → analyze_stats の method="outliers"
  （sql は集計せず明細を返す。1行1件の状態にしてから渡すこと）

# ファイル出力
- 「エクセル」「Excel」「xlsx」→ export_excel。観点が複数なら sheets に複数の SELECT を渡し、
  1ブックに複数シートでまとめる。
- 「CSV」「csvにして」「取り込み用」→ export_csv。複数指定するとZIPにまとめて渡される。
  文字コードは既定の utf-8-sig でよい（Excelで文字化けしない）。Shift_JIS を求められたときだけ cp932。
- 「テキストで」「レポートにして」「議事録」「まとめを文書で」→ export_text。
  body に自分で文章を書き、集計表を入れたい箇所に {{見出し}} と書いて sections に SELECT を指定する。
- ファイル出力ツールを呼んだ後は、画面に保存済み。中身の全件を文章で繰り返さず、
  何を入れたかだけ簡潔に伝える。

# 利用可能なツール
{tool_list}

# SQLルール
- SQLite方言。SELECT(または WITH ... SELECT)のみ。INSERT/UPDATE/DELETE/DDL/PRAGMA等は禁止(実行されません)。
- {naming}
- 1回の呼び出しで1ステートメント。末尾セミコロン不要。
- 集計は GROUP BY を使い、列に AS で日本語の別名を付けると表示が分かりやすい。
- 日付は文字列で保存されていることが多い。date() / strftime() を活用する（例: strftime('%Y-%m', 列) で月別）。
- 行数が多くなりそうなら LIMIT や集計で絞る。

# 選択中のデータカタログ
{catalog.prompt_for_scope(scope)}

現在時刻: {datetime.now().isoformat(timespec="seconds")}
"""


# --- 文脈の使用量の見積もり ------------------------------------------------------
#
# 「カタログを増やしてよいか」を判断するには、モデルが一度に読める量に対して
# いまどれだけ使っているかが要る。正確なトークン数はAPIに投げないと分からないが、
# それでは画面を開くたびに課金が発生する。そこで実測から係数を出して概算する。
#
# 実測（gpt-4o-mini・11DB選択）:
#   要約版 system 28,851字 + ツール定義 44,377字 → 入力 29,265 トークン
#   全文版 system 71,879字 + ツール定義 44,377字 → 入力 52,395 トークン
#   差分から、日本語主体の本文は 43,028字 → 23,130トークン = 0.537 トークン/字
# 下の係数で上の2例を計算すると、実測に対して +1.2% / +1.7% に収まる（やや多めに出る）。
# 見積もりは「余裕がある」と言いすぎない方が安全なので、多めに出るぶんには構わない。

#: 日本語主体の文章（カタログ・指示）の 1文字あたりトークン数
TOKENS_PER_CHAR_TEXT = 0.55
#: ツール定義のJSON（英字と記号が多い）の 1文字あたりトークン数
TOKENS_PER_CHAR_JSON = 0.31


def tokens_for(chars: int, kind: str = "text") -> int:
    """文字数からトークン数の概算を出す。"""
    ratio = TOKENS_PER_CHAR_JSON if kind == "json" else TOKENS_PER_CHAR_TEXT
    return int(max(0, chars) * ratio)


def budget(scope: list[dict], model: str | None = None, admin: bool = False) -> dict:
    """このスコープ・このモデルで、文脈をどれだけ使うかの概算。

    「いま」と「上限までカタログが育ったとき」の両方を返す。
    上限を決める画面で、変えた結果どうなるかを見せるため。
    """
    import models as models_mod

    model = model or config.OPENAI_MODEL
    context, known = models_mod.context_window(model)
    limit = models_mod.prompt_inline_limit()

    # 推定せず、実際に組み立てたものを測る（カタログはキャッシュ済みなので速い）
    system = build_system_prompt(scope, admin=admin)
    used_catalog = len(catalog.prompt_for_scope(scope))
    catalog_chars = catalog.inline_length(scope)       # 全文にした場合の長さ
    tool_chars = len(json.dumps(tools.build_tools(scope, admin=admin), ensure_ascii=False))

    # カタログ以外（SQLルール・ツールの使い分け・ツール名の一覧など）
    fixed_chars = max(0, len(system) - used_catalog)

    tool_tokens = tokens_for(tool_chars, "json")
    now = tokens_for(len(system)) + tool_tokens
    at_limit = tokens_for(fixed_chars + limit) + tool_tokens

    def pct(n: int) -> float:
        return round(n / context * 100, 1) if context else 0.0

    return {
        "model": model, "context": context, "context_known": known,
        "limit_chars": limit,
        "catalog_chars": catalog_chars,
        "catalog_inlined": catalog_chars <= limit,
        "tool_tokens": tool_tokens,
        # カタログ以外の固定ぶん。画面で上限を動かしたときに、
        # サーバと同じ式で計算し直せるように渡す。
        "base_tokens": tool_tokens + tokens_for(fixed_chars),
        "tokens_per_char": TOKENS_PER_CHAR_TEXT,
        "now_tokens": now, "now_pct": pct(now), "headroom_pct": round(100 - pct(now), 1),
        "at_limit_tokens": at_limit, "at_limit_pct": pct(at_limit),
        # 上限をここまで上げても文脈の半分に収まる、という目安
        "suggest_max_chars": max(0, int((context * 0.5 - tool_tokens
                                         - tokens_for(fixed_chars))
                                        / TOKENS_PER_CHAR_TEXT)),
    }


# --- 画像つきのメッセージ --------------------------------------------------------

# 受け付ける画像。ここに無い形式は送らない（APIが解釈できないため）
IMAGE_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


def user_message(text: str, images: list[dict] | None = None) -> dict:
    """ユーザー発言を作る。画像があればマルチモーダル形式にする。

    images: [{"mime": "image/png", "b64": "..."}] （b64はデータ本体のみ）
    画像が無いときは、これまで通り content が文字列のメッセージを返す
    （画像非対応のモデルに配列を渡すと弾かれることがあるため）。
    """
    if not images:
        return {"role": "user", "content": text}
    parts: list[dict] = []
    if text:
        parts.append({"type": "text", "text": text})
    for img in images:
        mime = img.get("mime") or "image/png"
        if mime not in IMAGE_MIMES:
            continue
        parts.append({"type": "image_url",
                      "image_url": {"url": f"data:{mime};base64,{img['b64']}",
                                    "detail": img.get("detail") or "auto"}})
    return {"role": "user", "content": parts or text}


# --- チャット補完 --------------------------------------------------------------

def chat(messages: list[dict], tool_defs: list[dict] | None = None,
         model: str | None = None):
    """messages を渡して1回の補完を取得。tools 付き。message オブジェクトを返す。

    tool_defs を省略した場合は組み込みツールのみ。通常はチャット側で
    tools.build_tools(entries) を渡し、ユーザー定義ツールも含める。
    model は画面で選ばれたモデル。省略時は env の既定。
    """
    kwargs = dict(
        model=model or config.OPENAI_MODEL,
        messages=messages,
        tools=tool_defs if tool_defs is not None else tools.BUILTIN_TOOLS,
        tool_choice="auto",
        temperature=config.OPENAI_TEMPERATURE,
    )
    if config.OPENAI_TOP_P is not None:
        kwargs["top_p"] = config.OPENAI_TOP_P
    if config.OPENAI_MAX_TOKENS is not None:
        kwargs["max_tokens"] = config.OPENAI_MAX_TOKENS
    resp = _create(**kwargs)
    return resp.choices[0].message


class StreamedMessage:
    """ストリーミングで組み立てた1回ぶんの応答。

    chat() が返す message オブジェクトと同じ形（content / tool_calls）に
    見えるようにしておく。呼び出し側はどちらでも同じ扱いができる。
    """

    class _Fn:
        def __init__(self, name="", arguments=""):
            self.name, self.arguments = name, arguments

    class _Call:
        def __init__(self, id="", name="", arguments=""):
            self.id, self.type = id, "function"
            self.function = StreamedMessage._Fn(name, arguments)

    def __init__(self):
        self.content = ""
        self.tool_calls = None
        self._parts: dict[int, dict] = {}

    def _finish(self):
        if not self._parts:
            self.tool_calls = None
            return
        self.tool_calls = [
            StreamedMessage._Call(p["id"], p["name"], p["arguments"])
            for _, p in sorted(self._parts.items())
        ]


def chat_stream(messages: list[dict], tool_defs: list[dict] | None = None,
                model: str | None = None):
    """1回の補完をストリーミングで受け取る。

    文字が届くたびに ("text", 差分) を yield し、
    最後に ("done", StreamedMessage) を1回だけ yield する。
    ツール呼び出しは途中経過を出さない（引数のJSONは途中では読めないため）。
    """
    kwargs = dict(
        model=model or config.OPENAI_MODEL,
        messages=messages,
        tools=tool_defs if tool_defs is not None else tools.BUILTIN_TOOLS,
        tool_choice="auto",
        temperature=config.OPENAI_TEMPERATURE,
        stream=True,
    )
    if config.OPENAI_TOP_P is not None:
        kwargs["top_p"] = config.OPENAI_TOP_P
    if config.OPENAI_MAX_TOKENS is not None:
        kwargs["max_tokens"] = config.OPENAI_MAX_TOKENS

    out = StreamedMessage()
    for chunk in _create(**kwargs):
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if getattr(delta, "content", None):
            out.content += delta.content
            yield ("text", delta.content)
        for tc in (getattr(delta, "tool_calls", None) or []):
            # 同じツール呼び出しが複数のチャンクに分かれて届くので、index で束ねる
            part = out._parts.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
            if tc.id:
                part["id"] = tc.id
            fn = getattr(tc, "function", None)
            if fn is not None:
                if getattr(fn, "name", None):
                    part["name"] += fn.name
                if getattr(fn, "arguments", None):
                    part["arguments"] += fn.arguments
    out._finish()
    yield ("done", out)


# --- AI下書き（データカタログ用） ----------------------------------------------

_DRAFT_SYSTEM = """あなたはデータカタログ作成の専門家です。
与えられたテーブルのプロファイル（列名・型・実値の分布・サンプル行）から、
テーブルと各列の業務的な説明文を日本語で推測し、JSONだけを出力してください。

出力形式（JSON以外の文字を含めないこと）:
{
  "description": "テーブルの説明。1行 = 何のレコードかを必ず含める。",
  "columns": {
    "列名": {
      "description": "列の説明",
      "values": {"コード値": "意味"}   // 値がコード(区分値)と思われる列のみ。それ以外は省略
    }
  }
}

注意:
- 確信が持てない場合は「〜と思われる」と書く。
- values は実値一覧にある値だけを対象にする。
- すべての列に説明を付ける。"""


_GLOSSARY_SYSTEM = """あなたはSQLiteに詳しいデータカタログ作成の専門家です。
与えられたテーブル定義（列・型・実値の分布・サンプル行）をもとに、
業務用語の「自然言語の説明」をSQLの式に翻訳してください。

出力形式（JSON以外の文字を含めないこと）:
{"用語": "SQL式", "用語2": "SQL式"}

守ること:
- WHERE にそのまま入る条件式（例: status != '9' AND amount >= 1000000）か、
  SELECT にそのまま入る計算式（例: SUM(amount) * 1.0 / COUNT(*)）だけを書く。
- SELECT や FROM で始まる文全体は書かない。末尾にセミコロンを付けない。
- 列名は与えられたテーブルに実在するものだけを使う。値は実値一覧にあるものを使う。
- SQLiteに無い関数(STDDEV, MEDIAN, PERCENTILE_CONT, SQRT, POWER など)は使わない。
- 説明があいまいで確信が持てない用語は、キーごと省略する（推測で書かない）。"""


def draft_glossary_sql(db_path, table_name: str | None, terms: list[dict]) -> dict:
    """業務用語の説明文からSQL式の下書きを作る。

    terms: [{"term": 用語, "description": 自然言語の説明}, ...]
    戻り値: {用語: SQL式}（翻訳できなかった用語は含まれない）
    """
    if not terms:
        return {}
    profile = catalog.profile_db(Path(db_path))
    meta = catalog.load_meta(Path(db_path))
    if table_name:
        context = catalog.table_text("db", table_name, profile, meta, full=True)
    else:   # テーブルをまたぐ用語。DB全体を見せる
        context = catalog.db_text("db", Path(db_path), None, full=True)
    asked = "\n".join(f"- {t['term']}: {t.get('description') or ''}" for t in terms)

    resp = _create(
        model=config.OPENAI_MODEL,
        messages=[
            {"role": "system", "content": _GLOSSARY_SYSTEM},
            {"role": "user", "content": f"{context}\n\n翻訳したい業務用語:\n{asked}"},
        ],
        temperature=0,
    )
    content = resp.choices[0].message.content or ""
    m = re.search(r"\{.*\}", content, re.DOTALL)
    if not m:
        raise ValueError(f"AIの応答をJSONとして解析できませんでした: {content[:200]}")
    data = json.loads(m.group(0))
    if not isinstance(data, dict):
        raise ValueError("AIの応答が想定した形式ではありません。")
    wanted = {t["term"] for t in terms}
    return {k: str(v).strip() for k, v in data.items() if k in wanted and str(v).strip()}


def draft_table_meta(db_path, table_name: str) -> dict:
    """テーブルのプロファイルからメタ情報の下書きを生成する。"""
    profile = catalog.profile_db(Path(db_path))
    meta = catalog.load_meta(Path(db_path))
    text = catalog.table_text("db", table_name, profile, meta, full=True)
    resp = _create(
        model=config.OPENAI_MODEL,
        messages=[
            {"role": "system", "content": _DRAFT_SYSTEM},
            {"role": "user", "content": f"ファイル名: {Path(db_path).name}\n\n{text}"},
        ],
        temperature=0,
    )
    content = resp.choices[0].message.content or ""
    # ```json ... ``` フェンスを剥がしてから解析
    m = re.search(r"\{.*\}", content, re.DOTALL)
    if not m:
        raise ValueError(f"AI下書きの応答をJSONとして解析できませんでした: {content[:200]}")
    data = json.loads(m.group(0))
    if not isinstance(data, dict):
        raise ValueError("AI下書きの応答が想定した形式ではありません。")
    return data


# --- ユーザー定義ツールの下書き ---------------------------------------------------
#
# SQLを書けない人でもツールを作れるようにするための入口。
# 「何をするツールか」を日本語で書いてもらい、SQLとパラメータはAIに起こさせる。
# 起こしたSQLは呼び出し側で必ず実データに当てて確かめる（推測のまま保存させない）。

_TOOL_SYSTEM = """あなたはSQLiteに詳しいデータ分析アプリの設定担当です。
利用者が日本語で書いた「やりたいこと」を、AIが呼び出せるツールの定義に変換してください。

出力形式（JSON以外の文字を含めないこと）:
{
  "name": "英小文字と_のみの短い名前（例: monthly_sales）",
  "description": "このツールが何を返すかの説明。AIがこれを読んで使うかどうかを決める",
  "sql": "SELECT ...（1文だけ。末尾のセミコロンは不要）",
  "parameters": [
    {"name": "year", "type": "string", "description": "対象年 YYYY",
     "required": true, "example": "2026"}
  ],
  "chart": {"chart_type": "line", "x": "月", "y": "売上", "title": "月別売上"}
}

守ること:
- SQLは SELECT（または WITH ... SELECT）だけ。書き込み・DDLは書かない。
- 列名・テーブル名は、与えられたカタログに実在するものだけを使う。推測で作らない。
- 「毎回変えたい値」は、利用者の日本語から自分で見極めて parameters にする。
  聞かれ方が変わるたびに差し替える値（「指定した年の」「ある部署の」「任意の期間で」など）は
  パラメータにし、SQLでは :名前 の形で参照する。
  一方「部署ごと」「月別」のような集計の切り口は、パラメータではなく GROUP BY で表す。
  迷ったらパラメータにしない。引数が増えるほどAIは呼びにくくなる。
- parameters の type は string / integer / number / boolean のいずれか。
- parameters には description（日本語）と example を必ず書く。
  example は「カタログの実値・期間に実在し、実際に行が返る値」にする。
  この値で試し実行して見せるので、0行になる値を書かないこと。
- 複数のDBにまたがるときは「DB名.テーブル名」で修飾する。
- 列には日本語の別名を AS で付ける（画面にそのまま出るため）。
- SQLiteに無い関数(STDDEV, MEDIAN, PERCENTILE_CONT, SQRT, POWER)やPIVOT構文は使わない。
- 見せ方が「グラフ」のときだけ chart を書く。x と y には SELECT の別名をそのまま使う。
  グラフでないときは chart を省略する。"""


def draft_tool(db_path, purpose: str, params_wanted: list[str] | None = None,
               render: str = "table", previous: dict | None = None,
               error: str | None = None) -> dict:
    """日本語の「やりたいこと」から、ユーザー定義ツールの下書きを起こす。

    db_path        … None なら全DBのカタログを見せる（作る人にDBを選ばせない）。
                     特定のDBに限りたいときだけパスを渡す。
    purpose        … 何をするツールか（日本語）。毎回変えたい値もこの文から読み取らせる
                     ので、呼び出し側が指定を組み立てる必要はない。
    params_wanted  … 毎回変えたい項目を明示したいときだけ渡す（例: ["対象年", "部署"]）。
                     省略すれば purpose の書き方からAIが判断する。
    render         … 結果の見せ方（table / chart / chart_dual / excel / csv / none）
    previous/error … 前回の下書きが実データで失敗したときの、SQLとエラー文。
                     渡すと「どこが間違っていたか」を踏まえて書き直す。
    """
    import db                       # 循環importを避けるため、使うときに読む

    # db_path が None なら全DBを見せる。どのDBに書くかは、やりたいことを読んだAIが
    # 決める（作る人にDBを選ばせない）。量が上限を超えるときは要約に落ちる。
    if db_path is None:
        paths = db.list_db_files()
    else:
        paths = [Path(db_path)]
    context = catalog.prompt_for_scope(
        [{"path": str(p), "alias": db.alias_for(p), "tables": None} for p in paths])
    ask = [f"やりたいこと: {purpose}"]
    if params_wanted:
        ask.append("毎回変えたい項目: " + "、".join(params_wanted))
    ask.append(f"結果の見せ方: {render}")
    if previous and error:
        ask.append("\n前回の下書きは実際のデータで失敗しました。原因を直して書き直してください。")
        ask.append(f"前回のSQL:\n{previous.get('sql', '')}")
        ask.append(f"エラー: {error}")

    resp = _create(
        model=config.OPENAI_MODEL,
        messages=[
            {"role": "system", "content": _TOOL_SYSTEM},
            {"role": "user", "content": f"{context}\n\n{chr(10).join(ask)}"},
        ],
        temperature=0,
    )
    content = resp.choices[0].message.content or ""
    m = re.search(r"\{.*\}", content, re.DOTALL)
    if not m:
        raise ValueError(f"AIの応答をJSONとして解析できませんでした: {content[:200]}")
    data = json.loads(m.group(0))
    if not isinstance(data, dict):
        raise ValueError("AIの応答が想定した形式ではありません。")

    out = {
        "name": str(data.get("name") or "").strip(),
        "description": str(data.get("description") or purpose).strip(),
        "sql": str(data.get("sql") or "").strip().rstrip(";"),
        "parameters": [],
        "render": render,
        "enabled": True,
    }
    for p in (data.get("parameters") or []):
        if not isinstance(p, dict) or not str(p.get("name") or "").strip():
            continue
        t = str(p.get("type") or "string")
        item = {
            "name": str(p["name"]).strip(),
            "type": t if t in custom_tools.PARAM_TYPES else "string",
            "description": str(p.get("description") or "").strip(),
            "required": p.get("required", True) is not False,
        }
        # 試し実行に使う値。空のまま流すと0行になり、動くかどうか確かめられない。
        if p.get("example") not in (None, ""):
            item["example"] = p["example"]
        out["parameters"].append(item)
    if render in ("chart", "chart_dual") and isinstance(data.get("chart"), dict):
        out["chart"] = data["chart"]
    return out
