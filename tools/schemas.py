"""LLMに渡すツールの定義（JSON Schema）。

ここは「何ができるか」の宣言だけを書く場所で、処理は置かない。
実処理は query / stats / reports / mail にある。
"""
from __future__ import annotations


import advanced
import analysis
import charts
import excel
import exports
import pptx_report
import usage

#: 前のツールが返したデータを、SQLを書き直さずに使い回すための指定。
#: 「集計 → グラフ → レポート」で同じSQLが何度も走るのを避ける。
_RESULT_ID = {
    "type": "string",
    "description": "前のツールが返した result_id。これを指定すると sql は不要で、"
                   "同じデータをそのまま使う（同じSQLを書き直さないこと）。",
}


# 指定の名前 -> スキーマ（同じ説明を何度も書かないためのまとめ）
_CHART_ARGS = {
    "x": {"type": "string", "description": "横軸／カテゴリにする列名。"},
    "y": {"type": "string", "description": "値にする列名（数値）。"},
    "y2": {"type": "string", "description": "もう一方の値の列（dumbbell の比較先）。"},
    "z": {"type": "string", "description": "3つ目の数値の列（scatter3d の高さ）。"},
    "color": {"type": "string",
              "description": "色分けに使う列名。積み上げや群分けにも使う。"},
    "size": {"type": "string", "description": "大きさ／幅に使う数値列。"},
    "text": {"type": "string", "description": "点や棒に添えるラベルの列名。"},
    "facet": {"type": "string", "description": "この列の値ごとに小さく分割して並べる。"},
    "path": {"type": "array", "items": {"type": "string"},
             "description": "階層。大きい分類から順に列名を並べる。"},
    "dimensions": {"type": "array", "items": {"type": "string"},
                   "description": "対象にする列名のリスト（3〜6列が読みやすい）。"},
    "lower": {"type": "string", "description": "下限の列（信頼区間や予測の幅）。"},
    "upper": {"type": "string", "description": "上限の列。"},
    "source": {"type": "string", "description": "流れの起点になる列。"},
    "target": {"type": ["string", "number"],
               "description": "流れの終点の列。指標では目標値（数値そのもの、"
                              "または目標が入っている列名）。"},
    "start": {"type": "string", "description": "開始日時の列。"},
    "end": {"type": "string", "description": "終了日時の列。"},
    "open": {"type": "string", "description": "始値の列。"},
    "high": {"type": "string", "description": "高値の列。"},
    "low": {"type": "string", "description": "安値の列。"},
    "close": {"type": "string", "description": "終値の列。"},
    "value": {"type": "string", "description": "指標にする数値の列。"},
    "agg": {"type": "string", "enum": ["sum", "mean", "max", "min", "last"],
            "description": "value をどうまとめるか。既定は sum。"},
    "max": {"type": "number", "description": "ゲージの上限値。省略すると自動。"},
    "suffix": {"type": "string", "description": "数値の後ろに付ける単位（円・%など）。"},
    "nbins": {"type": "integer", "description": "階級の数。既定は自動。"},
    "orientation": {"type": "string", "enum": ["v", "h"],
                    "description": "棒の向き。横棒は h。"},
    "barmode": {"type": "string", "enum": ["group", "stack", "relative"],
                "description": "棒の積み方。既定は group。"},
    "marginal": {"type": "string", "enum": ["box", "violin", "rug"],
                 "description": "ヒストグラムの上に添える分布。任意。"},
    "trendline": {"type": "boolean", "description": "散布図に回帰直線を重ねる。"},
    "colorscale": {"type": "string",
                   "description": "色の濃淡（Blues / Reds / Greens など）。"},
}


# ツール名 -> (分類, 説明, 使う指定, 必須)
_CHART_TOOLS = {
    "plot_comparison": (
        "比較",
        "項目どうしを比べるグラフ。「部署別」「商品別」「順位」「ランキング」"
        "「前年と比べて」「重点管理」を見せたいときに使う。",
        ("x", "y", "y2", "color", "size", "text", "facet", "orientation", "barmode"),
        ("sql", "chart_type", "x", "y", "title")),
    "plot_trend": (
        "推移",
        "時間とともにどう変わったかを見せるグラフ。「推移」「時系列」「予測の幅」"
        "「工程の期間」「日ごとの多寡」「異常な回」を扱うときに使う。",
        ("x", "y", "color", "text", "lower", "upper", "start", "end",
         "open", "high", "low", "close", "facet"),
        ("sql", "chart_type", "title")),
    "plot_composition": (
        "構成",
        "全体が何でできているかを見せるグラフ。「内訳」「構成比」「シェア」"
        "「階層」「増減の要因」「どこからどこへ流れたか」を扱うときに使う。",
        ("x", "y", "color", "path", "source", "target", "text"),
        ("sql", "chart_type", "title")),
    "plot_distribution": (
        "分布",
        "ばらつきの形を見せるグラフ。「分布」「ヒストグラム」「箱ひげ」"
        "「偏り」「正規分布か」「群ごとの散らばり」を扱うときに使う。",
        ("x", "y", "color", "nbins", "facet", "marginal"),
        ("sql", "chart_type", "title")),
    "plot_relationship": (
        "関係",
        "2つ以上の項目の関係を見せるグラフ。「相関」「散布図」「密度」"
        "「多変量」「総当たり」「つながり」を扱うときに使う。",
        ("x", "y", "z", "color", "size", "text", "dimensions", "nbins",
         "source", "target", "colorscale", "trendline", "facet"),
        ("sql", "chart_type", "title")),
    "plot_kpi": (
        "指標",
        "数字を1つ大きく見せるグラフ。「KPI」「達成率」「目標に対して」"
        "「今いくら」を見せたいときに使う。",
        ("value", "target", "agg", "max", "suffix", "colorscale"),
        ("sql", "chart_type", "value", "title")),
}


def _chart_tools() -> list[dict]:
    """用途ごとのグラフツール定義を作る。"""
    out = []
    for name, (cat, desc, fields, required) in _CHART_TOOLS.items():
        props = {
            "sql": {"type": "string",
                    "description": "グラフに使うデータを取る SELECT 文。"
                                   "集計が要るものは GROUP BY 済みにすること。"},
            "chart_type": {"type": "string", "enum": charts.types_in(cat),
                           "description": f"グラフ種別。{charts.type_help(cat)}"},
            "title": {"type": "string", "description": "グラフのタイトル。"},
            "purpose": {"type": "string",
                        "description": "このグラフで示したいことの短い説明。"},
        }
        props.update({f: _CHART_ARGS[f] for f in fields})
        out.append({"type": "function", "function": {
            "name": name,
            "description": (f"{desc}使える種別: {charts.type_help(cat)}"),
            "parameters": {"type": "object", "properties": props,
                           "required": list(required)},
        }})
    return out


BUILTIN_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_sql_query",
            "description": (
                "選択中の SQLite DB群 に対して読み取り専用の SELECT 文を実行し、結果テーブルを取得する。"
                "SELECT(または WITH ... SELECT)以外は実行不可。"
                "複数DBが選択されている場合、テーブル名は必ず『エイリアス.テーブル名』で修飾すること。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "実行する SQLite 用 SELECT 文。SELECT または WITH で始めること。",
                    },
                    "purpose": {
                        "type": "string",
                        "description": "このクエリで何を確認したいかの短い説明(日本語)。",
                    },
                },
                "required": ["sql", "purpose"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "plot_chart",
            "description": (
                "SELECT 文の結果をグラフ化する。時系列の推移や分布を可視化したいときに使用。"
                "内部で SELECT を実行し、指定の x / y / color 列でグラフを描画する。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "グラフ用データを取得する SELECT 文(GROUP BYで集計済みにする)。"},
                    "chart_type": {
                        "type": "string",
                        "enum": list(charts.CHART_TYPES),
                        "description": "グラフ種別。" + charts.type_help(),
                    },
                    "x": {"type": "string",
                          "description": "x軸に使う列名。pie/donut はカテゴリ、funnel は段階、"
                                         "radar は指標名、histogram は対象の数値列。"},
                    "y": {"type": "string",
                          "description": "y軸に使う列名。pie/donut/treemap/sunburst/funnel は値、"
                                         "radar は値。histogram では不要。"},
                    "color": {"type": "string",
                              "description": "系列(色分け)に使う列名。任意。heatmap ではマス目の値になる。"},
                    "size": {"type": "string", "description": "bubble の大きさに使う数値列名。"},
                    "text": {"type": "string", "description": "点や棒に表示するラベルの列名。任意。"},
                    "path": {
                        "type": "array", "items": {"type": "string"},
                        "description": "treemap/sunburst の階層。大きい分類から順に列名を並べる。",
                    },
                    "nbins": {"type": "integer", "description": "histogram の階級数。任意。"},
                    "orientation": {
                        "type": "string", "enum": ["v", "h"],
                        "description": "棒の向き。横棒にしたいときは h。bar以外では無視。",
                    },
                    "barmode": {
                        "type": "string",
                        "enum": ["group", "stack", "relative"],
                        "description": "棒グラフの積み方。積み上げ=stack、横並び比較=group(既定)。bar以外では無視。",
                    },
                    "title": {"type": "string", "description": "グラフのタイトル。"},
                    "purpose": {"type": "string", "description": "このグラフで示したいことの短い説明。"},
                },
                "required": ["sql", "chart_type", "title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "plot_dual_axis",
            "description": (
                "棒グラフ(左軸)と折れ線グラフ(右軸)を組み合わせた2軸グラフを描く。"
                "件数(棒)と比率など単位の異なる指標(折れ線)を同時に見せたいときに使用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "x列と数値の指標列を返す SELECT 文(GROUP BYで集計)。"},
                    "x": {"type": "string", "description": "x軸に使う列名。"},
                    "bar_y": {
                        "type": "array", "items": {"type": "string"},
                        "description": "左軸に棒で表示する数値列名のリスト(1つ以上)。例: 件数。",
                    },
                    "line_y": {
                        "type": "array", "items": {"type": "string"},
                        "description": "右軸に折れ線で表示する数値列名のリスト(1つ以上)。例: 比率(%)。",
                    },
                    "left_title": {"type": "string", "description": "左軸のラベル(任意)。"},
                    "right_title": {"type": "string", "description": "右軸のラベル(任意)。"},
                    "title": {"type": "string", "description": "グラフのタイトル。"},
                    "purpose": {"type": "string", "description": "このグラフで示したいことの短い説明。"},
                },
                "required": ["sql", "x", "bar_y", "line_y", "title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pivot_table",
            "description": (
                "クロス集計表（ピボットテーブル）を作る。"
                "「AとBのマトリクスで」「行に○○、列に△△」「クロス集計」「表形式で比較」"
                "などと言われたら使う。"
                "SQLite には PIVOT 構文が無く CASE WHEN を列の数だけ手書きする必要があるため、"
                "列に展開したい集計はSQLで書かずにこのツールを使うこと。"
                "sql では集計せず、明細または index/columns/values の3列を返すだけでよい。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string",
                            "description": "元データを取得する SELECT 文。集計はこのツールが行う。"},
                    "index": {"type": "array", "items": {"type": "string"},
                              "description": "行にする列名（複数可）。"},
                    "columns": {"type": "string",
                                "description": "列に展開する列名。省略すると行ごとの集計表になる。"},
                    "values": {"type": "string", "description": "集計する値の列名。"},
                    "aggfunc": {"type": "string", "enum": list(analysis.AGG_FUNCS),
                                "description": "集計方法。既定は sum。"},
                    "margins": {"type": "boolean", "description": "総計の行と列を付けるか。既定は false。"},
                    "percent": {"type": "string", "enum": list(analysis.PERCENT_MODES),
                                "description": "実数の代わりに構成比(%)で出す。"
                                               + " / ".join(f"{k}={v}" for k, v
                                                            in analysis.PERCENT_MODES.items())},
                    "rank": {"type": "string",
                             "description": "大きい順に並べて順位を付ける。列名を書くとその列で、"
                                            "'total' と書くと行の合計で並べる。"},
                    "render": {"type": "string", "enum": ["table", "heatmap"],
                               "description": "表示方法。heatmap にすると色付きの行列で見せる。既定は table。"},
                    "title": {"type": "string", "description": "見出し。"},
                    "purpose": {"type": "string", "description": "この集計で確認したいことの短い説明。"},
                },
                "required": ["sql", "index", "values"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_stats",
            "description": (
                "統計的な分析を行う。"
                "SQLite には STDDEV / MEDIAN / CORR / PERCENTILE が無いため、"
                "「相関」「中央値」「ばらつき」「四分位」「外れ値」「異常値」を聞かれたら"
                "SQLで計算しようとせず必ずこのツールを使うこと。"
                "sql は集計せずに明細を返す（1行1件）ようにする。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string",
                            "description": "分析対象の明細を取得する SELECT 文。集計はしない。"},
                    "method": {
                        "type": "string", "enum": ["describe", "correlation", "outliers"],
                        "description": "describe=基本統計量(件数/平均/標準偏差/最小/四分位/中央値/最大) / "
                                       "correlation=相関行列 / outliers=外れ値の抽出",
                    },
                    "columns": {"type": "array", "items": {"type": "string"},
                                "description": "対象の数値列。省略すると数値列を自動判定する。"},
                    "group_by": {"type": "string",
                                 "description": "describe のとき、この列ごとに分けて統計を出す。任意。"},
                    "target": {"type": "string",
                               "description": "outliers のとき、外れ値を調べる数値列。必須。"
                                              "mahalanobis のときは列名をカンマ区切りで複数。"},
                    "outlier_method": {"type": "string",
                                       "enum": list(advanced.OUTLIER_METHODS_EXT),
                                       "description": " / ".join(
                                           f"{k}={v}" for k, v
                                           in advanced.OUTLIER_METHODS_EXT.items())},
                    "threshold": {"type": "number",
                                  "description": "外れ値の閾値。iqr は既定1.5、zscore は既定3。"},
                    "corr_method": {"type": "string", "enum": list(analysis.CORR_METHODS),
                                    "description": "pearson=直線的な関係(既定) / spearman=順位の関係"},
                    "lag": {"type": "integer",
                            "description": "correlation のとき、何期先までずらして相関を見るか。"
                                           "「広告費は翌月の売上に効くか」のような遅れて出る効果を"
                                           "調べたいときに指定する。sql は時点の昇順で1行1期にすること。"},
                    "partial": {"type": "boolean",
                                "description": "correlation のとき true にすると偏相関にする。"
                                               "control で指定した列の影響を取り除いてから相関を見るので、"
                                               "「第3の変数のせいで関係して見えるだけ」を切り分けられる。"},
                    "control": {"type": "array", "items": {"type": "string"},
                                "description": "partial=true のとき、影響を取り除きたい列。"},
                    "title": {"type": "string", "description": "見出し。"},
                    "purpose": {"type": "string", "description": "この分析で確認したいことの短い説明。"},
                },
                "required": ["sql", "method"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "export_excel",
            "description": (
                "SELECT の結果を Excel ファイル(.xlsx)にまとめ、ユーザーがダウンロードできる状態にする。"
                "「エクセルで」「xlsxで」「ファイルにして」「ダウンロードしたい」"
                "などと言われたら使う。sheets に複数の SELECT を渡すと複数シートのブックになる。"
                "chart を書くと、そのシートのデータからExcelのグラフを作って貼る"
                "（画像ではないので、受け取った側が範囲や種類を変えられる）。"
                "数字を並べるだけのシートより、グラフを1つ付けた方が伝わる。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sheets": {
                        "type": "array",
                        "description": "ブックに入れるシート。1要素につき1シート。",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "description": "シート名(31文字以内)。"},
                                "sql": {"type": "string", "description": "このシートに書き出す SELECT 文。"},
                                "note": {"type": "string", "description": "シート先頭に入れる補足(任意)。"},
                                "chart": {
                                    "type": "object",
                                    "description": "このシートのデータから作るグラフ（任意）。",
                                    "properties": {
                                        "type": {"type": "string",
                                                 "enum": list(excel.CHART_TYPES),
                                                 "description": "グラフの種類。"},
                                        "category_column": {"type": "string",
                                                            "description": "横軸にする列名。"},
                                        "value_columns": {"type": "array",
                                                          "items": {"type": "string"},
                                                          "description": "系列にする数値列。"},
                                        "title": {"type": "string", "description": "グラフの題名。"},
                                        "y_title": {"type": "string", "description": "縦軸の名前。"},
                                        "x_title": {"type": "string", "description": "横軸の名前。"},
                                        "data_labels": {"type": "boolean",
                                                        "description": "値ラベルを出すか。"},
                                    },
                                    "required": ["type"],
                                },
                                "charts": {
                                    "type": "array",
                                    "items": {"type": "object", "additionalProperties": True},
                                    "description": "グラフを複数貼るときはこちらに並べる。",
                                },
                            },
                            "required": ["name", "sql"],
                        },
                    },
                    "filename": {"type": "string", "description": "ファイル名(拡張子不要)。例: 月別売上"},
                    "purpose": {"type": "string", "description": "何のためのファイルかの短い説明。"},
                },
                "required": ["sheets", "filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "export_csv",
            "description": (
                "SELECT の結果を CSV ファイルとして書き出し、ユーザーがダウンロードできる状態にする。"
                "「CSVで」「csvにして」「取り込み用のファイル」などと言われたら使う。"
                "files に複数指定すると、まとめてZIPで渡す。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "files": {
                        "type": "array",
                        "description": "書き出すファイル。1要素につき1CSV。",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "description": "ファイル名(拡張子不要)。"},
                                "sql": {"type": "string", "description": "書き出す SELECT 文。"},
                            },
                            "required": ["name", "sql"],
                        },
                    },
                    "encoding": {
                        "type": "string", "enum": list(exports.ENCODINGS),
                        "description": "文字コード。既定は utf-8-sig（Excelで開いても文字化けしない）。"
                                       "Shift_JIS が要るときだけ cp932。",
                    },
                    "delimiter": {
                        "type": "string", "enum": list(exports.DELIMITERS),
                        "description": "区切り文字。既定は comma。TSVにしたいときは tab。",
                    },
                    "purpose": {"type": "string", "description": "何のためのファイルかの短い説明。"},
                },
                "required": ["files"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "export_text",
            "description": (
                "文章（レポート・要約・メモ）をテキストファイルとして書き出し、"
                "ユーザーがダウンロードできる状態にする。"
                "「テキストで」「レポートにして」「議事録に」「まとめを文書で」などと言われたら使う。"
                "body に本文を自分で書き、必要なら sections に SELECT を指定して集計表を差し込む。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "ファイル名(拡張子不要)。"},
                    "body": {
                        "type": "string",
                        "description": "本文。あなたが書いた文章をそのまま入れる。"
                                       "sections を使う場合、差し込みたい位置に {{見出し}} と書く。",
                    },
                    "sections": {
                        "type": "array",
                        "description": "本文に差し込む集計表。省略可。",
                        "items": {
                            "type": "object",
                            "properties": {
                                "heading": {"type": "string",
                                            "description": "見出し。本文の {{この文字列}} が表に置き換わる。"
                                                           "本文に無ければ末尾に追記される。"},
                                "sql": {"type": "string", "description": "表にする SELECT 文。"},
                            },
                            "required": ["heading", "sql"],
                        },
                    },
                    "format": {
                        "type": "string", "enum": ["md", "txt"],
                        "description": "md=Markdown（表が罫線付き）/ txt=プレーンテキスト。既定は md。",
                    },
                    "encoding": {
                        "type": "string", "enum": list(exports.ENCODINGS),
                        "description": "文字コード。既定は utf-8-sig。",
                    },
                },
                "required": ["filename", "body"],
            },
        },
    },
    # ---- 統計 ---------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "hypothesis_test",
            "description": (
                "統計的仮説検定。「差があると言えるか」「偶然ではないか」「有意か」"
                "「A/Bどちらが良いか」「効果があったか」を判断したいときに使う。"
                "平均の差・比率の差・分布の偏り・相関の有無を、p値と効果量つきで判定する。"
                "sql は集計せず明細（1行1件）を返すこと。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "検定対象の明細を取る SELECT 文。"},
                    "method": {"type": "string", "enum": list(advanced.TEST_METHODS),
                               "description": " / ".join(f"{k}={v}" for k, v
                                                         in advanced.TEST_METHODS.items())},
                    "value_col": {"type": "string", "description": "測定値の列（数値）。"},
                    "group_col": {"type": "string",
                                  "description": "群を表す列。2群比較・分散分析・カイ二乗で使う。"},
                    "value_col2": {"type": "string",
                                   "description": "対応のある検定や相関で、もう一方の列。"},
                    "popmean": {"type": "number", "description": "1標本t検定で比較する基準値。"},
                    "expected": {"type": "array", "items": {"type": "number"},
                                 "description": "適合度検定で期待する比率や度数。"},
                    "alternative": {"type": "string",
                                    "enum": ["two-sided", "less", "greater"],
                                    "description": "対立仮説。既定は両側(two-sided)。"},
                    "title": {"type": "string", "description": "見出し。"},
                },
                "required": ["sql", "method"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "regression",
            "description": (
                "回帰分析。「何が効いているか」「要因分析」「どの変数が影響するか」"
                "「予測式を作りたい」ときに使う。係数・p値・寄与の大きさ・"
                "あてはまり(R²)・多重共線性まで返す。"
                "目的変数が0/1なら logistic、件数なら poisson を選ぶ。"
                "文字列の説明変数（部署など）は自動でダミー変数にする。"
                "sql は集計せず明細（1行1件）を返すこと。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "分析対象の明細を取る SELECT 文。"},
                    "target": {"type": "string", "description": "目的変数（説明したい列）。"},
                    "features": {"type": "array", "items": {"type": "string"},
                                 "description": "説明変数の列名。"},
                    "method": {"type": "string", "enum": list(advanced.REGRESSION_METHODS),
                               "description": " / ".join(f"{k}={v}" for k, v
                                                         in advanced.REGRESSION_METHODS.items())},
                    "predict": {"type": "array", "items": {"type": "object"},
                                "description": "予測したい入力の一覧。例 [{\"広告費\":100}]",
                                "additionalProperties": True},
                    "title": {"type": "string", "description": "見出し。"},
                },
                "required": ["sql", "target", "features"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "distribution_analysis",
            "description": (
                "分布の形を調べる。「ばらつき」「ヒストグラム」「偏り」「どんな分布か」"
                "「上位下位の広がり」を見たいときに使う。度数分布・要約統計・"
                "正規分布などへの当てはめ判定を返す。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "明細を取る SELECT 文。"},
                    "target": {"type": "string", "description": "調べる数値列。"},
                    "bins": {"type": "integer", "description": "階級の数。既定20。"},
                    "group_col": {"type": "string", "description": "群ごとに比べるときの列。"},
                    "fit": {"type": "array", "items": {"type": "string",
                                                       "enum": list(advanced.DISTRIBUTIONS)},
                            "description": "当てはめを試す分布。既定は norm と lognorm。"},
                    "title": {"type": "string", "description": "見出し。"},
                },
                "required": ["sql", "target"],
            },
        },
    },
    # ---- 時系列 -------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "forecast",
            "description": (
                "将来の値を予測する。「来月はいくら」「このままいくと」「着地見込み」"
                "「予測」を聞かれたら使う。予測値と95%の幅、"
                "過去データで試した誤差率(MAPE)を返す。"
                "sql は時点ごとに1行（例: 月ごとの売上合計）にすること。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string",
                            "description": "時点と値を返す SELECT 文（時点の昇順で1行1期）。"},
                    "time_col": {"type": "string", "description": "時点の列（年月など）。"},
                    "value_col": {"type": "string", "description": "予測する数値の列。"},
                    "periods": {"type": "integer", "description": "何期先まで予測するか。既定6。"},
                    "method": {"type": "string",
                               "enum": ["auto"] + list(advanced.FORECAST_METHODS),
                               "description": "auto=データ量から自動選択。" + " / ".join(
                                   f"{k}={v}" for k, v in advanced.FORECAST_METHODS.items())},
                    "season_length": {"type": "integer",
                                      "description": "季節の周期。月次で1年なら12、曜日なら7。"},
                    "exog": {
                        "type": "object", "additionalProperties": True,
                        "description": "説明変数つきで予測する場合の指定。"
                                       "{\"columns\": [\"広告費\"], \"future\": [[120],[130]]} の形で、"
                                       "future には予測する期数と同じ数だけ将来の値を並べる。"
                                       "「広告費をこう置いたら売上はどうなるか」に答えられる。",
                    },
                    "title": {"type": "string", "description": "見出し。"},
                },
                "required": ["sql", "time_col", "value_col"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "timeseries_analysis",
            "description": (
                "時系列の見方をまとめる。「推移」「トレンド」「季節性」「前年同月比」"
                "「移動平均」「周期」を聞かれたときに使う。"
                "sql は時点ごとに1行にすること。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "時点と値を返す SELECT 文。"},
                    "time_col": {"type": "string", "description": "時点の列。"},
                    "value_col": {"type": "string", "description": "値の列。"},
                    "window": {"type": "integer", "description": "移動平均の期間。既定3。"},
                    "season_length": {"type": "integer",
                                      "description": "季節の周期（月次なら12）。指定すると季節分解する。"},
                    "title": {"type": "string", "description": "見出し。"},
                },
                "required": ["sql", "time_col", "value_col"],
            },
        },
    },
    # ---- 試算・シミュレーション ---------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "monte_carlo_simulation",
            "description": (
                "モンテカルロ・シミュレーション。「もし〜だったら」「リスク」"
                "「確率」「見込みの幅」「何%の確率で」を扱うときに使う。"
                "不確実な入力を分布で与え、式を何万回も試して結果の分布を出す。"
                "実データのばらつきをそのまま使いたい変数は dist=empirical と column を指定し、"
                "その列を返す sql も併せて渡すこと。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "formula": {"type": "string",
                                "description": "変数名を使った計算式。例 (単価 - 原価) * 数量 - 固定費"},
                    "variables": {
                        "type": "object", "additionalProperties": True,
                        "description": "{変数名: {dist, ...}}。dist は normal(mean,std) / "
                                       "uniform(min,max) / triangular(min,mode,max) / "
                                       "lognormal(mean,std) / poisson(lam) / binomial(n,p) / "
                                       "empirical(column) / fixed(value)。",
                    },
                    "trials": {"type": "integer", "description": "試行回数。既定10000。"},
                    "sql": {"type": "string",
                            "description": "empirical を使うときに、その列を含む明細を取る SELECT 文。"},
                    "targets": {"type": "array", "items": {"type": "number"},
                                "description": "「この値を超える確率」を知りたいしきい値。"},
                    "title": {"type": "string", "description": "見出し。"},
                },
                "required": ["formula", "variables"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scenario_analysis",
            "description": (
                "シナリオ比較（楽観・標準・悲観など）。前提を数パターン置いて"
                "結果を並べ、どの変数の影響が大きいかも出す。"
                "確率分布まで置く必要がないときは、こちらの方が説明しやすい。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "formula": {"type": "string", "description": "変数名を使った計算式。"},
                    "scenarios": {"type": "object", "additionalProperties": True,
                                  "description": "{シナリオ名: {変数名: 値}}"},
                    "base": {"type": "object", "additionalProperties": True,
                             "description": "共通の前提値。感度分析の基準にもなる。"},
                    "title": {"type": "string", "description": "見出し。"},
                },
                "required": ["formula", "scenarios"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bootstrap_estimate",
            "description": (
                "ブートストラップ法で平均などの信頼区間を出す。"
                "「この差は誤差の範囲か」「どのくらい確からしいか」を、"
                "分布の形を仮定せずに示せる。件数が少ないときにも使える。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "明細を取る SELECT 文。"},
                    "target": {"type": "string", "description": "対象の数値列。"},
                    "statistic": {"type": "string",
                                  "enum": ["mean", "median", "std", "sum", "p90"],
                                  "description": "推定する統計量。既定 mean。"},
                    "group_col": {"type": "string", "description": "群ごとに出すときの列。"},
                    "trials": {"type": "integer", "description": "再抽出の回数。既定5000。"},
                    "title": {"type": "string", "description": "見出し。"},
                },
                "required": ["sql", "target"],
            },
        },
    },
    # ---- 分ける -------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "clustering",
            "description": (
                "k-meansでグループ分けする。「セグメント」「タイプ分け」「似ている順に分類」"
                "「顧客を分けたい」ときに使う。列ごとの尺度差は標準化して吸収する。"
                "分け方が分からないときは k に \"auto\" を指定すると、"
                "最も素直に分かれる数を自動で選び、各グループの特徴も言葉で返す。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "明細を取る SELECT 文。"},
                    "features": {"type": "array", "items": {"type": "string"},
                                 "description": "分類に使う数値列。"},
                    "k": {"type": ["integer", "string"],
                          "description": "グループ数。既定3。\"auto\" にすると"
                                         "シルエット係数が最も高い数（2〜8）を自動で選ぶ。"},
                    "categorical": {"type": "array", "items": {"type": "string"},
                                    "description": "分類に使いたい区分の列（地域・会員区分など）。"
                                                   "0/1に開いてから一緒に分ける。"},
                    "label_col": {"type": "string", "description": "行の名前になる列（顧客名など）。"},
                    "title": {"type": "string", "description": "見出し。"},
                },
                "required": ["sql", "features"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "abc_analysis",
            "description": (
                "ABC分析（パレート分析）。「売上の8割を占める商品」「重点管理」"
                "「上位集中度」を見るときに使う。累計構成比でA/B/Cに区分する。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "品目と値を返す SELECT 文。"},
                    "label_col": {"type": "string", "description": "品目の列（商品名など）。"},
                    "value_col": {"type": "string", "description": "金額や数量の列。"},
                    "thresholds": {"type": "array", "items": {"type": "number"},
                                   "description": "A/Bの境目。既定 [70, 90]（累計%）。"},
                    "title": {"type": "string", "description": "見出し。"},
                },
                "required": ["sql", "label_col", "value_col"],
            },
        },
    },
    # ---- レポート -----------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "export_pptx",
            "description": (
                "PowerPointのレポートを作る。「パワポで」「スライドにして」「報告資料」"
                "「プレゼン」と言われたら使う。会議でそのまま映せる体裁で出力する。"
                "グラフは編集できるPowerPointのグラフとして入る。"
                "各スライドに sql を書くと、その場でSQLを実行して中身を埋める。"
                "\n"
                "重要: 各スライドに message（そのページで言いたいこと1行）を必ず書く。"
                "見出しの下に帯で表示され、聞き手はここだけ読めば分かる。"
                "「売上推移」ではなく「3月の落ち込みは期ずれで、実勢は右肩上がり」と書く。"
                "\n"
                "構成の目安: title（表紙）→ kpi（数字の要約）→ "
                "section（章の区切り）→ chart / table（根拠）→ compare（案の比較）→ "
                "closing（まとめと次のアクション）。中扉が2つ以上あれば目次は自動で入る。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "ファイル名（拡張子不要）。"},
                    "title": {"type": "string", "description": "レポート全体の題名。"},
                    "subtitle": {"type": "string", "description": "副題（対象期間など）。"},
                    "footer": {"type": "string", "description": "各ページ下部に入れる文字。"},
                    "slides": {
                        "type": "array",
                        "description": "スライドの並び。",
                        "items": {
                            "type": "object",
                            "properties": {
                                "kind": {"type": "string", "enum": list(pptx_report.SLIDE_KINDS),
                                         "description": "title=表紙 / agenda=目次 / "
                                                        "section=中扉 / message=文字だけ / "
                                                        "table=表 / chart=グラフ / "
                                                        "kpi=数字を大きく / compare=2案の比較 / "
                                                        "closing=まとめと次のアクション"},
                                "title": {"type": "string"},
                                "subtitle": {"type": "string"},
                                "message": {"type": "string",
                                            "description": "そのページで言いたいこと1行。"
                                                           "見出しの下に帯で出る。必ず書く。"},
                                "sql": {"type": "string",
                                        "description": "table/chart のとき、中身を取る SELECT 文。"},
                                "chart": {"type": "string", "enum": list(pptx_report.CHART_TYPES),
                                          "description": "グラフの種類。"},
                                "category_column": {"type": "string",
                                                    "description": "chart のとき横軸にする列。"},
                                "value_columns": {"type": "array", "items": {"type": "string"},
                                                  "description": "chart のとき系列にする数値列。"},
                                "bullets": {"type": "array",
                                            "items": {"type": "object",
                                                      "additionalProperties": True},
                                            "description": "箇条書き。文字列でも "
                                                           "{text, level, strong} でもよい。"},
                                "lead": {"type": "string",
                                         "description": "message のとき、箇条書きの前に置く導入文。"},
                                "body": {"type": "string", "description": "本文。"},
                                "items": {"type": "array", "items": {"type": "object",
                                                                     "additionalProperties": True},
                                          "description": "kpi のとき "
                                                         "[{label, value, unit, delta, "
                                                         "delta_unit, delta_label, "
                                                         "higher_is_better, note}]。"},
                                "panes": {"type": "array", "items": {"type": "object",
                                                                     "additionalProperties": True},
                                          "description": "compare のとき、左右2つ "
                                                         "[{title, value, unit, bullets}]。"},
                                "summary": {"type": "array", "items": {"type": "string"},
                                            "description": "closing のときのまとめ。"},
                                "actions": {"type": "array",
                                            "items": {"type": "object",
                                                      "additionalProperties": True},
                                            "description": "closing のとき "
                                                           "[{text, owner, due}]。"},
                                "callout": {"type": "string",
                                            "description": "下部の囲みで強調する一文。1枚に1つまで。"},
                                "comment": {"type": "string",
                                            "description": "図表の右に添える所見。"},
                                "source": {"type": "string",
                                           "description": "出所・集計条件。数字の資料には入れる。"},
                                "notes": {"type": "string", "description": "発表者ノート。"},
                                "data_labels": {"type": "boolean",
                                                "description": "グラフに数値ラベルを出す。"
                                                               "既定は自動判断。"},
                                "highlight_rows": {"type": "array",
                                                   "items": {"type": "integer"},
                                                   "description": "table で強調する行（0始まり）。"},
                                "max_rows": {"type": "integer",
                                             "description": "table で載せる最大行数。既定12。"},
                            },
                            "required": ["kind"],
                        },
                    },
                },
                "required": ["slides"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "build_report",
            "description": (
                "分析の結果を1つのレポートにまとめる。"
                "「レポートにして」「まとめて」「報告書」「分析結果を整理して」"
                "「結論と根拠を示して」と言われたら使う。"
                "画面に読みやすい形で出しつつ、ダウンロードできるファイルも作る。"
                "各セクションに sql を書けば表が入り、chart を書けばグラフも入る。"
                "要点(summary)と結論(conclusion)は必ず自分の言葉で書くこと。"
                "数字を並べるだけでなく『だから何か』を書く。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "レポートの題名。"},
                    "subtitle": {"type": "string", "description": "対象期間や条件。"},
                    "summary": {
                        "type": "array", "items": {"type": "string"},
                        "description": "要点。最初に読む人が3行で分かるように書く。",
                    },
                    "sections": {
                        "type": "array",
                        "description": "本編。1セクション＝1つの論点。",
                        "items": {
                            "type": "object",
                            "properties": {
                                "heading": {"type": "string", "description": "見出し。"},
                                "body": {"type": "string",
                                         "description": "説明の文章。何が言えるかを書く。"},
                                "sql": {"type": "string",
                                        "description": "根拠として載せる表の SELECT 文。任意。"},
                                "chart": {
                                    "type": "object", "additionalProperties": True,
                                    "description": "グラフの指定。chart_type と x / y などを"
                                                   "入れる。sql の結果を使う。任意。",
                                },
                                "note": {"type": "string",
                                         "description": "この節の所見・注意点。任意。"},
                                "max_rows": {"type": "integer",
                                             "description": "表に載せる最大行数。既定20。"},
                            },
                            "required": ["heading"],
                        },
                    },
                    "conclusion": {"type": "string", "description": "結論。"},
                    "recommendations": {
                        "type": "array", "items": {"type": "string"},
                        "description": "推奨する打ち手。実行できる粒度で書く。",
                    },
                    "caveats": {
                        "type": "array", "items": {"type": "string"},
                        "description": "前提・制約・数字の読み方の注意。",
                    },
                    "format": {
                        "type": "string",
                        "enum": ["md", "docx", "pptx", "xlsx", "none"],
                        "description": "ダウンロード用ファイルの形式。"
                                       "md=軽い文書(既定) / docx=Word報告書（図表つき）/ "
                                       "pptx=スライド / xlsx=表ごとにシート / "
                                       "none=画面表示だけ",
                    },
                    "filename": {"type": "string", "description": "ファイル名（拡張子不要）。"},
                    "org": {"type": "string", "description": "表紙に入れる部署名など。"},
                    "footer": {"type": "string",
                               "description": "各ページ下部の文字（「社外秘」など）。"},
                },
                "required": ["title", "sections"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "export_docx",
            "description": (
                "Word文書（.docx）を作る。「Wordで」「docxで」「報告書にして」"
                "「配布資料」「回覧」と言われたら使う。"
                "表紙・目次・図表番号つきのキャプション・ページ番号が入り、"
                "そのまま配布できる体裁になる。"
                "各セクションに sql を書くと表が入り、chart も書くとグラフが図として入る。"
                "本文(body)は必ず自分の言葉で書くこと。表を貼っただけの文書は読まれない。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "文書の題名。"},
                    "subtitle": {"type": "string", "description": "副題（対象期間など）。"},
                    "org": {"type": "string", "description": "表紙に入れる部署名。"},
                    "author": {"type": "string", "description": "作成者名。"},
                    "footer": {"type": "string",
                               "description": "各ページ下部の文字（「社外秘」など）。"},
                    "toc": {"type": "boolean", "description": "目次を入れるか。既定は入れる。"},
                    "summary": {"type": "array", "items": {"type": "string"},
                                "description": "冒頭の要約。3点程度。"},
                    "sections": {
                        "type": "array",
                        "description": "本編。1セクション＝1つの見出し。",
                        "items": {
                            "type": "object",
                            "properties": {
                                "heading": {"type": "string", "description": "見出し。"},
                                "level": {"type": "integer",
                                          "description": "見出しの階層（1が大見出し）。"},
                                "body": {"type": "string", "description": "本文。"},
                                "bullets": {"type": "array", "items": {"type": "string"},
                                            "description": "箇条書き。"},
                                "sql": {"type": "string",
                                        "description": "表・グラフの元になる SELECT 文。"},
                                "chart": {"type": "object", "additionalProperties": True,
                                          "description": "グラフの指定（chart_type と x / y など）。"
                                                         "図として貼られる。"},
                                "table": {"type": "boolean",
                                          "description": "表も載せるか。既定は載せる。"},
                                "caption": {"type": "string", "description": "図のキャプション。"},
                                "table_caption": {"type": "string",
                                                  "description": "表のキャプション。"},
                                "note": {"type": "string", "description": "補足・注記。"},
                                "callout": {"type": "string",
                                            "description": "囲みで強調したい一文。"},
                                "max_rows": {"type": "integer",
                                             "description": "表に載せる最大行数。既定40。"},
                                "page_break": {"type": "boolean",
                                               "description": "このセクションの前で改ページする。"},
                            },
                            "required": ["heading"],
                        },
                    },
                    "conclusion": {"type": "string", "description": "結論。"},
                    "recommendations": {
                        "type": "array",
                        "items": {"type": "object", "additionalProperties": True},
                        "description": "推奨する打ち手。[{text, owner, due}] または文字列の並び。",
                    },
                    "caveats": {"type": "array", "items": {"type": "string"},
                                "description": "前提・注意。"},
                    "filename": {"type": "string", "description": "ファイル名（拡張子不要）。"},
                },
                "required": ["title", "sections"],
            },
        },
    },
    # ---- メール -------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "find_mail_recipients",
            "description": (
                "宛先をDBから探す。名前・部署・アドレスの一部で検索できる。"
                "「〇〇部に送って」「田中さんに送って」と言われたら、"
                "まずこれで実在するアドレスを確認してから compose_email を呼ぶこと。"
                "アドレスを推測で作ってはいけない。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "検索語（氏名・部署名・アドレスの一部）。空なら一覧。"},
                    "table": {"type": "string", "description": "探す表を絞るとき。省略可。"},
                    "limit": {"type": "integer", "description": "最大件数。既定50。"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compose_email",
            "description": (
                "メールの下書きを作る。画面に確認カードが出て、"
                "ユーザーが「送信」を押したときだけ実際に送られる（自動送信はしない）。"
                "宛先は find_mail_recipients で確認した実在のアドレスを使うこと。"
                "直前に作った Excel / CSV / PowerPoint を添付できる。"
                "本文は挨拶・要点・詳細・結びの順で、日本語のビジネスメールとして書く。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {"type": "array", "items": {"type": "string"},
                           "description": "宛先アドレス。"},
                    "to_query": {"type": "string",
                                 "description": "アドレスの代わりに検索語で指定する場合"
                                                "（例: 営業部）。DBから引いて宛先にする。"},
                    "cc": {"type": "array", "items": {"type": "string"}},
                    "bcc": {"type": "array", "items": {"type": "string"}},
                    "subject": {"type": "string", "description": "件名。"},
                    "body": {"type": "string", "description": "本文（プレーンテキスト）。"},
                    "attach_filenames": {
                        "type": "array", "items": {"type": "string"},
                        "description": "この会話で作ったファイル名。省略時は添付なし。"
                                       "'all' を入れると直近に作ったファイルを全部添付する。"},
                    "reply_to": {"type": "string", "description": "返信先アドレス。"},
                },
                "required": ["subject", "body"],
            },
        },
    },
    # ---- 業務でよく聞かれる分析 ---------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "compare_periods",
            "description": (
                "2つの期間を比べ、差がどこから来たのかまで分解する。"
                "「先月と比べて」「前年同月比」「前期からどう変わったか」"
                "「なぜ落ちたのか」を聞かれたら使う。"
                "全体の増減だけでなく、どの区分が押し下げ／押し上げたか（寄与度）を出す。"
                "qty_col を渡すと、金額の変化を「数量が動いたぶん」と「単価が動いたぶん」に分ける。"
                "sql は期間の列・値の列（あれば区分の列）を含む形で、2期間ぶんまとめて取ること。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string",
                            "description": "2期間ぶんのデータを取る SELECT 文。"
                                           "期間の列・値の列・（任意で）区分の列を返す。"},
                    "period_col": {"type": "string", "description": "期間を表す列（'2026-01' など）。"},
                    "value_col": {"type": "string", "description": "比べる数値の列（売上など）。"},
                    "dimension_col": {"type": "string",
                                      "description": "増減の内訳を見る区分の列（部門・商品など）。任意。"},
                    "qty_col": {"type": "string",
                                "description": "数量の列。指定すると数量要因と単価要因に分解する。"},
                    "current": {"type": "string", "description": "当期。省略すると最後の期。"},
                    "previous": {"type": "string", "description": "前期。省略すると最後から2番目の期。"},
                    "title": {"type": "string", "description": "見出し。"},
                },
                "required": ["sql", "period_col", "value_col"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "data_quality",
            "description": (
                "分析の前に、データそのものの異常を洗い出す。"
                "「数字が合わない」「件数がおかしい」「このデータは信用できるか」"
                "と言われたとき、また重要な集計を出す前の確認に使う。"
                "行数・主キーの重複・空の列・親に存在しない外部キー・日付の範囲を調べ、"
                "深刻な順に並べて返す。SQLは要らない（選択中のDBを直接見る）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tables": {"type": "array", "items": {"type": "string"},
                               "description": "調べるテーブル名。省略すると選択中のテーブルを順に見る。"},
                    "title": {"type": "string", "description": "見出し。"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "detect_anomalies",
            "description": (
                "時系列から「いつもと違う時点」と「いつから変わったか」を見つける。"
                "「異常」「急に増えた」「おかしい日」「いつから悪化したか」を聞かれたら使う。"
                "前後の期間と比べるので、右肩上がりのデータでも直近を全部異常とは言わない。"
                "静的な外れ値（analyze_stats の outliers）とは用途が違う。"
                "sql は時点ごとに1行にすること。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "時点と値を返す SELECT 文。"},
                    "time_col": {"type": "string", "description": "時点の列。"},
                    "value_col": {"type": "string", "description": "監視する数値の列。"},
                    "window": {"type": "integer", "description": "比べる前後の期間。既定7。"},
                    "threshold": {"type": "number",
                                  "description": "何倍離れたら異常とするか。既定3。小さくすると多く拾う。"},
                    "season_length": {"type": "integer",
                                      "description": "曜日や月の周期。指定すると季節変動を除いてから判定する。"},
                    "changepoints": {"type": "boolean",
                                     "description": "水準が変わった時点も探すか。既定true。"},
                    "title": {"type": "string", "description": "見出し。"},
                },
                "required": ["sql", "time_col", "value_col"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "funnel_analysis",
            "description": (
                "段階ごとの通過・離脱・滞留を出す。"
                "「見積から受注までの転換率」「どこで落ちているか」「滞留」"
                "「リードタイム」を聞かれたら使う。"
                "sql は 1行=1案件 にして、各段階の日付（または通過フラグ）の列を並べること。"
                "例: 見積日・受注日・請求日・入金日を1行に持つSELECT。"
                "値が入っていればその段階を通過した扱いになる。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string",
                            "description": "1行=1案件で、各段階の日付列を並べた SELECT 文。"},
                    "steps": {"type": "array", "items": {"type": "string"},
                              "description": "段階を表す列名を、順番に並べる。"},
                    "labels": {"type": "array", "items": {"type": "string"},
                               "description": "画面に出す段階の名前。省略すると列名を使う。"},
                    "group_col": {"type": "string",
                                  "description": "区分ごとに通過率を比べるときの列（担当・地域など）。"},
                    "title": {"type": "string", "description": "見出し。"},
                },
                "required": ["sql", "steps"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cohort_analysis",
            "description": (
                "いつ始めた人がどれだけ続いているかを見る。"
                "「継続率」「定着」「リピート」「離脱」「初回からの推移」を聞かれたら使う。"
                "初回の期でグループ分けし、経過期ごとの残存率をマトリクスで返す。"
                "sql は 1行=(対象, 期) の明細にすること（同じ人が複数期に出てよい）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string",
                            "description": "対象と期の明細を返す SELECT 文。"},
                    "id_col": {"type": "string", "description": "対象の列（顧客ID・社員IDなど）。"},
                    "period_col": {"type": "string",
                                   "description": "期の列（'2026-01' など、並べて正しい順になる形）。"},
                    "value_col": {"type": "string",
                                  "description": "金額なども見るときの数値列。任意。"},
                    "max_periods": {"type": "integer", "description": "何期先まで見るか。既定12。"},
                    "title": {"type": "string", "description": "見出し。"},
                },
                "required": ["sql", "id_col", "period_col"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "market_basket",
            "description": (
                "一緒に買われている（使われている）品目の組み合わせを見つける。"
                "「併売」「セット販売」「一緒に買われる」「関連商品」を聞かれたら使う。"
                "支持度・確信度・リフトを返す。SQLでは実質書けない分析。"
                "sql は 1行=(伝票, 品目) の明細にすること。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string",
                            "description": "伝票と品目の明細を返す SELECT 文。"},
                    "transaction_col": {"type": "string",
                                        "description": "伝票を表す列（受注ID・レシートIDなど）。"},
                    "item_col": {"type": "string", "description": "品目の列（商品名など）。"},
                    "min_support": {"type": "number",
                                    "description": "全伝票に占める最低の出現率(%)。既定1.0。"},
                    "top": {"type": "integer", "description": "返す組み合わせの数。既定25。"},
                    "title": {"type": "string", "description": "見出し。"},
                },
                "required": ["sql", "transaction_col", "item_col"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "survival_analysis",
            "description": (
                "「どれだけ持つか」「いつ辞めるか」を扱う。"
                "設備の故障間隔(MTBF)・部品の寿命・社員の在籍期間・顧客の継続期間に使う。"
                "まだ起きていない分（在籍中・稼働中）を捨てずに計算するので、"
                "単純平均のように短く見積もることがない。"
                "Weibull分布の形から、劣化型か初期不良型か偶発型かも判定する。"
                "sql は 1行=1対象 にして、期間の列と（あれば）発生フラグの列を返すこと。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "1行=1対象の明細を返す SELECT 文。"},
                    "duration_col": {"type": "string",
                                     "description": "期間の列（稼働時間・在籍日数など）。"},
                    "event_col": {"type": "string",
                                  "description": "起きたか(1)まだか(0)の列。省略すると全件で起きた扱い。"},
                    "group_col": {"type": "string",
                                  "description": "群ごとに比べるときの列（機種・部署など）。"},
                    "title": {"type": "string", "description": "見出し。"},
                },
                "required": ["sql", "duration_col"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "explore_import_files",
            "description": (
                "「データ取り込み」の取り込み元フォルダにあるファイルを調べる。読むだけで、"
                "取り込み・変更・削除はしない（取り込みは画面の操作でしか行われない）。"
                "「取り込み元に何がある？」「新しく届いたファイルは？」「このExcelは取り込める？」"
                "「まだ取り込んでいないファイルは？」と聞かれたら使う。"
                "まだDBに入っていないファイルの話なので、SQLでは答えられない。"
                "\n"
                "file を指定しなければ一覧（拡張子を問わず、場所・サイズ・更新日時・"
                "取り込み済みか）、指定すればそのファイルの下見になる。"
                "一覧で得たパスをそのまま file に渡すこと（パスを推測して組み立てないこと）。"
                "\n"
                "下見では「そのまま取り込める / 手直しが要る / 取り込みに向かない」を判定し、"
                "理由と直し方を返す。セル結合・多段見出し・月が横に並んだクロス表・"
                "合計行の混入・見出しが1行目にない、といった"
                "「取り込めるが正しく使えない」形を見つけられる。"
                "取り込みを勧める前に、この判定を確認すること。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string",
                             "description": "見たいフォルダ。省略すると許可フォルダの直下を見る。"},
                    "recursive": {"type": "boolean",
                                  "description": "下の階層もまとめて見るか。既定 false。"},
                    "pattern": {"type": "string",
                                "description": "名前に含まれる文字での絞り込み（例: 売上）。"},
                    "only_not_imported": {"type": "boolean",
                                          "description": "まだ取り込んでいないファイルだけに絞る。"},
                    "check": {"type": "boolean",
                              "description": "一覧の各ファイルについて、表として使える形かどうかも"
                                             "判定する。1件ずつ開くので20件までにしている。"},
                    "file": {"type": "string",
                             "description": "中身を下見するファイルのパス。一覧で得たものを使う。"},
                    "sheet": {"type": "string",
                              "description": "Excelのとき、見たいシート名。省略すると先頭のシート。"},
                    "header_row": {"type": "integer",
                                   "description": "見出しの行（0始まり）。2行目が見出しなら1。"},
                    "rows": {"type": "integer",
                             "description": "下見で読む行数。既定5、最大20。"},
                    "title": {"type": "string", "description": "見出し。"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_glossary_term",
            "description": (
                "業務用語をデータカタログの用語集に登録する「登録カード」をチャットに出す。"
                "実際に登録するかはユーザーがカードのボタンで決める（勝手には登録されない）。"
                "\n使いどころ:"
                "\n- ユーザーが言葉の定義を教えてくれたとき"
                "（「有効な受注とはキャンセル以外のこと」など）"
                "\n- あいまいな用語をあなたが解釈し、その解釈をユーザーが認めたとき"
                "\n- 同じ言葉の意味を何度も聞き直していると気づいたとき"
                "\nまず「この定義で用語集に登録しますか？」と一言確認し、"
                "前向きな返事があったらこのツールでカードを出す。"
                "会話のたびに毎回は出さない（うるさくなる）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "term": {"type": "string", "description": "用語（例: 有効な受注）。"},
                    "description": {"type": "string",
                                    "description": "日本語の定義。ユーザーの言い回しを活かす。"},
                    "sql": {"type": "string",
                            "description": "SQLの条件式・計算式（任意）。"
                                           "例: orders.status != '9' AND orders.kbn = '1'。"
                                           "会話で実際に使って正しかった式を入れる。"},
                    "how": {"type": "string",
                            "description": "どのデータをどこから取り、どう絞る/計算するのかを、"
                                           "SQLを知らない人に伝わる日本語で書く。テーブルや列は"
                                           "業務の言葉で呼ぶ。例:「受注データから、キャンセル(9)"
                                           "以外で取引区分が通常(1)の行を数える」。必須。"},
                    "table": {"type": "string",
                              "description": "用語を置くテーブル。その用語が主に関わるテーブル名。"
                                             "複数テーブルにまたがる用語のときは省略し db を指定。"},
                    "db": {"type": "string",
                           "description": "DB全体の用語にするときのDBファイル名（例: demo_sales.db）。"},
                },
                "required": ["term", "description", "how"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_example",
            "description": (
                "いまの質問と実行済みSQLを「例文」としてカタログに登録する登録カードを出す。"
                "例文はAIのお手本になり、似た質問への精度が上がる。"
                "実際に登録するかはユーザーがカードのボタンで決める。"
                "\n使いどころ:"
                "\n- ユーザーが「これを例文にして」「この答えを覚えて」と言ったとき"
                "\n- ユーザーが回答を「合っている」と認め、その質問が今後もよく出そうなとき"
                "\nsql には、この会話で実際に実行して正しかったSQLをそのまま入れる（書き直さない）。"
                "毎回は提案しない。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string",
                                 "description": "ユーザーの質問文（言い回しを変えない）。"},
                    "sql": {"type": "string",
                            "description": "実行して正しかったSELECT文そのまま。"},
                    "summary": {"type": "string",
                                "description": "どのデータをどこから取り、どう集計したかを、"
                                               "SQLを知らない人に伝わる日本語で。"
                                               "例:「受注データと顧客マスタをつなぎ、"
                                               "ランクごとに売上金額を合計した」。"},
                },
                "required": ["question", "sql", "summary"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "show_er_diagram",
            "description": (
                "DBのER図（テーブル同士の関係図）をチャット画面に表示する。"
                "「ER図を見せて」「テーブルの関係を図で」「データ構造を見たい」"
                "と言われたら使う。図は読み取り専用で、利用者が拡大縮小・全画面表示できる。"
                "表示と同時に結合の一覧も返るので、それを踏まえて補足してよい。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "db": {"type": "string",
                           "description": "対象のDBファイル名（例: demo_sales.db）。"
                                          "質問がどのDBの話かはカタログの説明から判断する。"},
                    "purpose": {"type": "string",
                                "description": "何を確かめたくて表示するかの短い説明。"},
                },
                "required": ["db"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_usage",
            "description": (
                "このアプリ自身の使われ方（利用状況）を調べる。分析対象のDBの中身ではなく、"
                "チャット履歴と取り込みの記録が材料なので、SQLでは答えられない。"
                "「このアプリはどれくらい使われている？」「誰が使っている？」"
                "「よく使われる機能は？」「どんな質問が多い？」「どこで失敗している？」"
                "「カタログのどこを直せばいい？」と聞かれたら使う。"
                "\n"
                "method で見る角度を選ぶ。まず summary で全体像を出し、"
                "気になった点を errors や users で掘るとよい。"
                "特に errors は失敗を「カタログを直せば減るもの」と"
                "「モデル・API側の問題」に分けて返すので、改善の打ち手を答えるときに使う。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "method": {
                        "type": "string",
                        "enum": [*usage.METHODS, "imports"],
                        "description": " / ".join(
                            f"{k}={v}" for k, v in usage.METHODS.items())
                        + " / imports=取り込みの実行実績",
                    },
                    "days": {"type": "integer",
                             "description": "直近何日ぶんを見るか。省略すると全期間。"},
                    "user": {"type": "string",
                             "description": "特定の利用者だけに絞る（ユーザー名）。任意。"},
                    "title": {"type": "string", "description": "見出し。"},
                    "purpose": {"type": "string",
                                "description": "この集計で確認したいことの短い説明。"},
                },
                "required": ["method"],
            },
        },
    },
    # ---- グラフ（用途別） ---------------------------------------------------
    *_chart_tools(),
    {
        "type": "function",
        "function": {
            "name": "describe_table",
            "description": (
                "選択中DBのテーブル詳細（列・型・説明・コード値の意味・実値の分布・サンプル行）を取得する。"
                "初めて使うテーブルでSQLを書く前に呼んで、列名や値の実体を確認する。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "db": {"type": "string", "description": "DBのエイリアス名。"},
                    "table": {"type": "string", "description": "テーブル名。"},
                },
                "required": ["db", "table"],
            },
        },
    },
]


def _allow_result_id(node) -> None:
    """sql を受け取る所すべてに result_id を足し、sql を必須から外す。

    レポートの節やExcelのシートのように、入れ子の中にも sql がある。
    1つずつ手で書き足すと必ず抜けるので、木をたどって機械的に付ける。
    """
    if isinstance(node, list):
        for v in node:
            _allow_result_id(v)
        return
    if not isinstance(node, dict):
        return
    props = node.get("properties")
    if isinstance(props, dict) and "sql" in props and "result_id" not in props:
        props["result_id"] = dict(_RESULT_ID)
        req = node.get("required")
        if isinstance(req, list) and "sql" in req:
            node["required"] = [r for r in req if r != "sql"]
    for v in node.values():
        _allow_result_id(v)


_allow_result_id(BUILTIN_TOOLS)

# plot_chart は用途別のグラフツール（plot_comparison など）で完全に置き換えられる。
# 同じことが2通りでできると、AIはどちらを使うか毎回迷い、定義の文字数も倍かかる。
# 実処理は残してあるので、過去の会話やユーザー定義の上書きが壊れることはない。
_RETIRED = {"plot_chart"}
BUILTIN_TOOLS = [t for t in BUILTIN_TOOLS if t["function"]["name"] not in _RETIRED]
