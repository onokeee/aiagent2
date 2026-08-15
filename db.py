"""SQLite アクセス層（複数DB対応）。

data/ フォルダの .db ファイルを列挙し、選択されたDB群を読み取り専用で
ATTACH した1つの接続に対して SELECT を実行する。複数DBを選択した場合は
`エイリアス.テーブル名` でファイルをまたいだ JOIN が可能。

最重要: ユーザー(LLM)が生成したSQLは SELECT のみ 実行を許可する。
多層防御で守る:
  1. 構文チェック   : 単一ステートメント / SELECT・WITH で始まる / 書込キーワード禁止
  2. 読み取り専用接続: mode=ro で ATTACH するのでそもそも書込不可
  3. オーソライザ    : SQLite の authorizer で SELECT/READ 以外を DENY
  4. タイムアウト    : progress handler で暴走クエリを中断
"""
from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

import config

# --- data/ フォルダのDBファイル ----------------------------------------------

def list_db_files() -> list[Path]:
    """data/ 直下の .db ファイル一覧（名前順）。"""
    if not config.DATA_DIR.exists():
        return []
    return sorted(p for p in config.DATA_DIR.glob("*.db") if p.is_file())


def path_for(name) -> Path:
    """画面から渡されたDB名を data/ の実ファイルに解決する。

    名前を data/ に連結するのではなく、列挙済みの一覧から名前が一致するものを
    探す。こうしておくと "../" のような指定が入っても data/ の外には出ない。
    """
    target = Path(str(name or "")).name          # ディレクトリ部分は捨てる
    for p in list_db_files():
        if p.name == target:
            return p
    raise FileNotFoundError(f"DBが見つかりません: {name}")


# 記号と空白だけを潰す。日本語などのマルチバイト文字はそのまま残す
# （SQLiteは非ASCIIの識別子をクオート無しで扱えるので、"売上.db" は 売上.受注 と書ける）。
# ここでASCIIだけに絞ると「店舗マスタ」が "_____" になり、
# 複数の日本語DBを選んだときに区別できなくなる。
_ALIAS_BAD = re.compile(r"[^\w]", re.UNICODE)
_RESERVED_ALIASES = {"main", "temp"}


def alias_for(path: Path) -> str:
    """ファイル名から SQL で使うエイリアス名（英数字と_のみ）を作る。"""
    a = _ALIAS_BAD.sub("_", Path(path).stem)
    if not a or a[0].isdigit():
        a = "db_" + a
    if a.lower() in _RESERVED_ALIASES:
        a += "_db"
    return a


def aliases_for(paths: list[Path]) -> list[str]:
    """複数ファイルに一意なエイリアスを割り当てる（衝突時は連番を付ける）。"""
    result: list[str] = []
    used: set[str] = set()
    for p in paths:
        a = alias_for(p)
        base, n = a, 2
        while a.lower() in used:
            a = f"{base}_{n}"
            n += 1
        used.add(a.lower())
        result.append(a)
    return result


# --- 接続ヘルパ ---------------------------------------------------------------

def _ro_uri(path) -> str:
    return Path(path).resolve().as_uri() + "?mode=ro"


def connect_ro(path) -> sqlite3.Connection:
    """単一DBへの読み取り専用接続（プロファイリング用）。"""
    return sqlite3.connect(_ro_uri(path), uri=True)


# SQLiteが同時にATTACHできる数の上限（既定10）。main を1つ使うので実質これだけ。
MAX_ATTACHED = 10


def connect_scope(paths_aliases: list[tuple]) -> sqlite3.Connection:
    """空の :memory: を main とし、各DBを読み取り専用で ATTACH した接続を作る。

    paths_aliases: [(path, alias), ...]
    """
    if len(paths_aliases) > MAX_ATTACHED:
        raise ValueError(
            f"1つのSQLで扱えるDBは{MAX_ATTACHED}個までです"
            f"（この問い合わせは{len(paths_aliases)}個を必要としています）。SQLiteの制限です。"
            "テーブル名を『DB名.テーブル名』の形で書けば、実際に使うDBだけを繋ぐので"
            "多くの場合はこの制限に当たりません。"
            "どうしても必要なら、サイドバーで対象のDBを絞ってください。")
    conn = sqlite3.connect("file::memory:", uri=True)
    for path, alias in paths_aliases:
        # alias は英数字と_のみに正規化済みなので識別子として安全
        conn.execute(f'ATTACH DATABASE ? AS "{alias}"', (_ro_uri(path),))
    return conn


def dbs_named_in(sql: str) -> list[str]:
    """SQLが「エイリアス.」の形で名指ししているDBファイル名。"""
    out = []
    for p in list_db_files():
        a = alias_for(p)
        if a and re.search(r'(?<![\w."])' + re.escape(a) + r'\s*\.', sql, re.IGNORECASE):
            out.append(p.name)
    return out


def widen_scope(sql: str, scope: list[dict]) -> list[dict]:
    """SQLが必要とするDBを、選ばれていなくても繋ぐ。

    ユーザー定義ツールは作った人がDBを意識せずに書くので、SQLが別DBに入ることがある。
    選択中のDBだけを繋ぐと、正しいツールが "no such table" で落ちる。
    読むだけであり、DBの選択はもともと「見る範囲を絞る」ためのもので
    アクセス制御ではない（README参照）ため、必要なものは繋いでよい。

    ATTACH の上限があるので、そこで打ち止める（超えた分は元のエラーで気づける）。
    """
    out = list(scope or [])
    have = {str(s.get("alias") or "").lower() for s in out}
    for p in list_db_files():
        if len(out) >= MAX_ATTACHED:
            break
        a = alias_for(p)
        if a.lower() in have:
            continue
        if re.search(r'(?<![\w."])' + re.escape(a) + r'\s*\.', sql, re.IGNORECASE):
            out.append({"path": str(p), "alias": a, "name": p.name, "tables": None})
            have.add(a.lower())
    return out


def narrow_scope(sql: str, scope: list[dict]) -> list[dict]:
    """そのSQLに関係するDBだけに絞る。

    選択中のDBを全部つなぐ必要はない。SQLiteは一度に10個までしかATTACHできないので、
    11個以上選んでいると、2つのテーブルを見るだけの問い合わせも実行できなくなっていた。
    「エイリアス.テーブル」で名指しされたDBと、修飾なしのテーブル名が一致するDBだけを残す。

    どちらでも判断できないときは、今までどおり全部を返す（勝手に減らして
    「no such table」にするより、元の分かりやすいエラーの方がよい）。
    """
    if len(scope) <= 1:
        return scope

    picked, seen = [], set()

    def add(s):
        key = str(s.get("path"))
        if key not in seen:
            seen.add(key)
            picked.append(s)

    # 名指しされているDB（複数DBを選んでいるときは必ずこの形で書かせている）
    for s in scope:
        alias = str(s.get("alias") or "")
        if alias and re.search(r'(?<![\w."])' + re.escape(alias) + r'\s*\.',
                               sql, re.IGNORECASE):
            add(s)
    # 修飾なしのテーブル名で参照されているDB。上と混在したSQLでも取りこぼさない
    for s in scope:
        for t in (s.get("tables") or []):
            if re.search(r'(?<![\w."])' + re.escape(str(t)) + r'(?![\w"])',
                         sql, re.IGNORECASE):
                add(s)
                break

    return picked[:MAX_ATTACHED] if picked else scope


# --- SELECT 専用ガード --------------------------------------------------------

# replace はここに入れない。SQLite の replace(X,Y,Z) は文字列関数で、
# 「株式会社」を落とすといった用途でごく普通に使う。書き込みになるのは
# REPLACE INTO の形だけなので、それは下で別に見る。
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|attach|detach|"
    r"reindex|vacuum|pragma|grant|revoke|begin|commit|rollback|savepoint|merge)\b",
    re.IGNORECASE,
)
_REPLACE_INTO = re.compile(r"\breplace\s+into\b", re.IGNORECASE)

#: 文字列リテラルと引用符付き識別子。'' や "" のエスケープも1つの塊として食う。
_QUOTED = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|`(?:[^`]|``)*`|\[[^\]]*\]")


def _strip_sql_comments(sql: str) -> str:
    sql = re.sub(r"--[^\n]*", " ", sql)                     # 行コメント
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)   # ブロックコメント
    return sql


def _blank_quoted(sql: str) -> str:
    """引用符で囲まれた中身を空にした、検査用のコピーを作る。

    キーワードや ';' をそのまま探すと、値の中の文字まで拾ってしまう。
    WHERE status = 'delete' や WHERE note = ';' が「危険なSQL」として
    弾かれていた。実行するのは元のSQLで、これは検査にしか使わない。
    """
    return _QUOTED.sub(lambda m: m.group(0)[0] + m.group(0)[-1], sql)


def validate_select(sql: str) -> str:
    """SELECT文として安全か検証し、整形済みSQLを返す。問題があれば ValueError。"""
    if not sql or not sql.strip():
        raise ValueError("SQLが空です。")
    cleaned = _strip_sql_comments(sql).strip().rstrip(";").strip()
    if not cleaned:
        raise ValueError("実行可能なSQLがありません。")
    probe = _blank_quoted(cleaned)          # 検査は中身を抜いたコピーに対して行う
    if ";" in probe:
        raise ValueError("複数ステートメントは実行できません(SELECT文を1つだけ指定してください)。")
    low = probe.lower()
    if not (low.startswith("select") or low.startswith("with")):
        raise ValueError("SELECT文(または WITH ... SELECT)のみ実行できます。")
    m = _FORBIDDEN.search(probe) or _REPLACE_INTO.search(probe)
    if m:
        raise ValueError(f"書き込み・DDL系のキーワード '{m.group(0)}' は使用できません。読み取り専用です。")
    return cleaned


# SQLite オーソライザで許可するアクション
_ALLOWED_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}
for _name in ("SQLITE_RECURSIVE",):  # 環境によって存在しない場合がある
    if hasattr(sqlite3, _name):
        _ALLOWED_ACTIONS.add(getattr(sqlite3, _name))


def _authorizer(action, arg1, arg2, db_name, trigger):
    if action in _ALLOWED_ACTIONS:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


# SQLiteに無い関数 → 代わりに使うもの。
# エラーメッセージにこれを添えないと、LLMは同じ関数で何度も書き直す。
_MISSING_FUNC_HINTS = {
    "stddev": "analyze_stats(method='describe')", "stdev": "analyze_stats(method='describe')",
    "stddev_samp": "analyze_stats(method='describe')",
    "variance": "analyze_stats(method='describe')", "var_samp": "analyze_stats(method='describe')",
    "median": "analyze_stats(method='describe')",
    "percentile": "analyze_stats(method='describe')",
    "percentile_cont": "analyze_stats(method='describe')",
    "percentile_disc": "analyze_stats(method='describe')",
    "corr": "analyze_stats(method='correlation')",
    "regr_slope": "regression", "stddev_pop": "analyze_stats(method='describe')",
    "sqrt": "analyze_stats か advanced 系のツール",
    "power": "掛け算で書き換える（x*x など）",
    "date_trunc": "strftime('%Y-%m', 列) など strftime を使う",
    "now": "date('now') / datetime('now')",
    "year": "strftime('%Y', 列)", "month": "strftime('%m', 列)",
    "day": "strftime('%d', 列)", "concat": "|| で連結する",
    "ifnull_": "IFNULL は使える", "listagg": "group_concat",
    "string_agg": "group_concat", "top": "LIMIT",
}


def explain_error(e: Exception) -> str:
    """SQLの失敗を、次に何をすればよいかまで書いた文にする。"""
    msg = str(e)
    m = re.search(r"no such function:\s*([A-Za-z_0-9]+)", msg)
    if m:
        fn = m.group(1)
        hint = _MISSING_FUNC_HINTS.get(fn.lower())
        if hint:
            return (f"{msg} … SQLite には {fn}() がありません。"
                    f"SQLで書き直そうとせず、{hint} を使ってください。")
        return (f"{msg} … SQLite には {fn}() がありません。"
                "標準のSQLite関数だけで書き直すか、専用の分析ツールを使ってください。")
    m = re.search(r"no such column:\s*(\S+)", msg)
    if m:
        return (f"{msg} … 列名が違います。describe_table でテーブルの列を確認してから"
                "書き直してください（推測で列名を作らないこと）。")
    if "syntax error" in msg:
        return (f"{msg} … SQLite で解釈できない書き方です。"
                "ウィンドウ関数の一部・WITHIN GROUP・PIVOT などは使えません。"
                "集計や統計は専用ツール（pivot_table / analyze_stats）に任せてください。")
    return msg


def run_select(sql: str, scope: list[dict], max_rows: int | None = None,
               timeout_s: int | None = None, params: dict | None = None):
    """検証済みSELECTを、選択スコープのDB群に対して実行する。

    scope:  [{"path": str, "alias": str, ...}, ...]（tables キーは無視。
            安全性はDB単位のATTACH + SELECT専用ガードで担保する）
    params: バインド変数（:name）に渡す値。値はSQL文字列に埋め込まれず
            プレースホルダ経由で渡るため、SQLインジェクションは起こらない。
    戻り値: (columns, rows, truncated)
    """
    if not scope:
        raise ValueError("対象のDBが選択されていません。サイドバーでDBを選択してください。")
    safe_sql = validate_select(sql)
    max_rows = max_rows or config.MAX_RESULT_ROWS
    timeout_s = timeout_s or config.QUERY_TIMEOUT_SEC

    # 選択中のDBを全部つながない。このSQLが要るものだけを繋ぐ（ATTACHは10個まで）
    use = narrow_scope(safe_sql, scope)
    conn = connect_scope([(s["path"], s["alias"]) for s in use])
    try:
        conn.set_authorizer(_authorizer)
        start = time.time()
        conn.set_progress_handler(lambda: 1 if (time.time() - start) > timeout_s else 0, 10000)
        try:
            cur = conn.execute(safe_sql, params or {})  # 単一ステートメントのみ実行可能
        except sqlite3.Error as e:
            # 「何が悪いか」だけでなく「代わりに何を使うか」まで返す
            raise sqlite3.OperationalError(explain_error(e)) from e
        columns = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(max_rows + 1)
        truncated = len(rows) > max_rows
        rows = [tuple(r) for r in rows[:max_rows]]
        return columns, rows, truncated
    finally:
        conn.close()


if __name__ == "__main__":
    # SELECT専用ガード + ATTACH横断クエリのセルフテスト
    import tempfile, os

    ok_cases = [
        "SELECT 1",
        "select a.x from t a join u b on a.id=b.id",
        "WITH t AS (SELECT 1 AS a) SELECT a FROM t",
        # 文字列リテラルの中のキーワードや記号で弾かないこと
        "SELECT replace(name,'株式会社','') FROM t",
        "SELECT * FROM t WHERE x='delete me'",
        "SELECT * FROM t WHERE note=';'",
        "SELECT * FROM t WHERE s='don''t drop it'",
        'SELECT "delete" FROM t',
    ]
    ng_cases = [
        "DELETE FROM t",
        "DROP TABLE t",
        "UPDATE t SET x=1",
        "SELECT 1; DELETE FROM t",
        "INSERT INTO t VALUES(1)",
        "PRAGMA table_info(t)",
        "ATTACH DATABASE 'x.db' AS z",
        "REPLACE INTO t VALUES(1)",
        "SELECT * FROM t WHERE x='a'; DROP TABLE t",
    ]
    for s in ok_cases:
        validate_select(s)
        print("OK   ", s)
    for s in ng_cases:
        try:
            validate_select(s)
            print("!! ガードすり抜け:", s)
        except ValueError as e:
            print("BLOCK", s, "=>", e)

    # ATTACH 横断クエリ
    d = tempfile.mkdtemp()
    p1, p2 = os.path.join(d, "a.db"), os.path.join(d, "b.db")
    c = sqlite3.connect(p1); c.execute("CREATE TABLE t(id INTEGER, v TEXT)")
    c.execute("INSERT INTO t VALUES(1,'x'),(2,'y')"); c.commit(); c.close()
    c = sqlite3.connect(p2); c.execute("CREATE TABLE u(id INTEGER, w TEXT)")
    c.execute("INSERT INTO u VALUES(1,'A'),(2,'B')"); c.commit(); c.close()
    scope = [{"path": p1, "alias": "a"}, {"path": p2, "alias": "b"}]
    cols, rows, tr = run_select("SELECT t.v, u.w FROM a.t t JOIN b.u u ON t.id=u.id", scope)
    print("CROSS-DB JOIN:", cols, rows)
    # 書込は物理的に拒否されるか
    try:
        run_select("SELECT 1", scope)  # ガード通過の確認
        conn = connect_scope([(p1, "a")])
        conn.execute("INSERT INTO a.t VALUES(9,'z')")
        print("!! 読み取り専用が効いていない")
    except sqlite3.OperationalError as e:
        print("RO-GUARD:", e)
