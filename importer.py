"""Excel / CSV を SQLite に取り込む層。

このアプリで **唯一 DB に書き込む場所**。分析側（db.py）は読み取り専用のまま保つ。
書き込みをここに閉じ込めることで、「チャットからDBが書き換わることはない」という
保証を壊さずに、DB・テーブルの新規作成を提供する。

安全のための制約:
  - 読むファイルは config.IMPORT_DIRS の中にあるものだけ（画面からパスは打たせない）
  - シンボリックリンクや .. で許可フォルダの外に出ようとしたら拒否
  - 拡張子・ファイルサイズ・行数の上限あり
  - 作る .db は config.DATA_DIR の直下のみ。名前も英数字系に正規化する
"""
from __future__ import annotations

import re
import sqlite3
import unicodedata
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml

import config

# SQLiteの予約語のうち、テーブル名・列名に使われがちなもの
_RESERVED = {
    "abort", "action", "add", "all", "alter", "and", "as", "asc", "between", "by", "case",
    "check", "column", "commit", "create", "cross", "default", "delete", "desc", "distinct",
    "drop", "else", "end", "escape", "except", "exists", "for", "from", "full", "group",
    "having", "in", "index", "inner", "insert", "into", "is", "join", "key", "left", "like",
    "limit", "not", "null", "offset", "on", "or", "order", "outer", "primary", "references",
    "right", "select", "set", "table", "then", "to", "transaction", "union", "unique",
    "update", "using", "values", "view", "when", "where", "with",
}


class ImportError_(Exception):
    """取り込みに失敗したときに投げる（画面にそのまま出せる日本語メッセージ）。"""


# =============================================================================
# 取り込み元ファイルの列挙（許可フォルダの中だけ）
# =============================================================================

def _read_extra() -> list[str]:
    p = config.IMPORT_DIRS_FILE
    if not p.exists():
        return []
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as e:
        print(f"[importer] 追加フォルダの設定を読めませんでした: {p} ({e})")
        return []
    items = data.get("dirs") if isinstance(data, dict) else data
    return [str(x) for x in (items or []) if str(x).strip()]


def extra_dirs() -> list[Path]:
    """画面から追加されたフォルダ。"""
    return [Path(s).expanduser() for s in _read_extra()]


def configured_dirs() -> list[dict]:
    """許可フォルダの一覧（どこで設定されたかつき）。"""
    out = [{"path": d, "source": "env", "removable": False} for d in config.IMPORT_DIRS]
    for d in extra_dirs():
        out.append({"path": d, "source": "ui", "removable": True})
    return out


def add_dir(raw: str) -> Path:
    """画面からフォルダを追加する。存在と読み取り可否をその場で確かめる。"""
    if not config.IMPORT_DIRS_EDITABLE:
        raise ImportError_("画面からのフォルダ追加は無効化されています（IMPORT_DIRS_EDITABLE）。")
    text = (raw or "").strip().strip('"')
    if not text:
        raise ImportError_("フォルダのパスを入力してください。")
    p = Path(text).expanduser()
    try:
        real = p.resolve(strict=True)
    except OSError:
        raise ImportError_(f"見つかりません: {text}（マウントされているか確認してください）") from None
    if not real.is_dir():
        raise ImportError_(f"フォルダではありません: {real}")
    # ルート直下を丸ごと許可すると、走査が終わらないうえ事故のもとになる
    if real.parent == real:
        raise ImportError_("ドライブ/ファイルシステムのルートは指定できません。"
                           "取り込み用のフォルダを切って指定してください。")
    try:
        next(real.iterdir(), None)
    except PermissionError:
        raise ImportError_(f"読み取り権限がありません: {real}") from None
    except OSError as e:
        raise ImportError_(f"アクセスできません: {real}（{e.strerror or e}）") from None

    current = _read_extra()
    if any(Path(s).expanduser().resolve() == real for s in current
           if Path(s).expanduser().exists()):
        raise ImportError_("そのフォルダは既に登録されています。")
    if any(d.resolve() == real for d in config.IMPORT_DIRS if d.exists()):
        raise ImportError_("env の IMPORT_DIRS に既に入っています。")

    current.append(str(real))
    _write_extra(current)
    return real


def remove_dir(raw: str) -> bool:
    if not config.IMPORT_DIRS_EDITABLE:
        raise ImportError_("画面からのフォルダ変更は無効化されています。")
    current = _read_extra()
    left = [s for s in current if s != raw]
    if len(left) == len(current):
        return False
    _write_extra(left)
    return True


def _write_extra(items: list[str]) -> None:
    p = config.IMPORT_DIRS_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump({"dirs": items}, allow_unicode=True, sort_keys=False),
                 encoding="utf-8")


def allowed_dirs() -> list[Path]:
    """実際に読み込みを許可するフォルダ（env + 画面から追加した分）。"""
    out, seen = [], set()
    for d in list(config.IMPORT_DIRS) + extra_dirs():
        try:
            real = d.resolve()
        except OSError:
            continue
        if real not in seen:
            seen.add(real)
            out.append(real)
    return out


def dir_status() -> list[dict]:
    """許可フォルダごとの状態。

    本番ではネットワークマウント（/mnt/... など）を指すため、
    「ファイルが無い」のか「マウントされていない・権限がない」のかを
    画面で切り分けられるようにする。
    """
    rows = []
    for entry in configured_dirs():
        d = entry["path"]
        info = {"設定値": str(d), "実際のパス": "", "状態": "", "ok": False,
                "source": entry["source"], "removable": entry["removable"]}
        try:
            real = d.resolve()
            info["実際のパス"] = str(real)
            if not real.exists():
                info["状態"] = "見つかりません（マウントされていない可能性があります）"
            elif not real.is_dir():
                info["状態"] = "フォルダではありません"
            else:
                next(real.iterdir(), None)      # 読めるかどうかを実際に試す
                info["状態"] = "利用できます"
                info["ok"] = True
        except PermissionError:
            info["状態"] = "読み取り権限がありません"
        except OSError as e:
            info["状態"] = f"アクセスできません（{e.strerror or e}）"
        rows.append(info)
    return rows


def _walk(root: Path, depth: int = 0, only_supported: bool = True):
    """root 以下を走査する。depth=0 なら階層の制限なし。

    権限の無いフォルダは黙って飛ばす（共有フォルダには必ずあるため、
    そこで止まると他のファイルまで見えなくなる）。

    only_supported=False にすると拡張子で絞らない。「何が置いてあるか」を
    調べる用で、読み込みの可否は別に判断する。
    """
    stack = [(root, 0)]
    seen_dirs: set = set()
    while stack:
        cur, level = stack.pop()
        try:
            real = cur.resolve()
            if real in seen_dirs:        # リンクの輪でぐるぐる回らないように
                continue
            seen_dirs.add(real)
            entries = list(cur.iterdir())
        except OSError:
            continue
        for p in entries:
            try:
                if p.is_dir():
                    if not depth or level < depth:
                        stack.append((p, level + 1))
                elif is_noise(p.name):
                    continue
                elif not only_supported or p.suffix.lower() in config.IMPORT_EXTENSIONS:
                    yield p
            except OSError:
                continue


def is_noise(name: str) -> bool:
    """Windows共有フォルダに必ず混ざる、開いても意味の無いファイル。

    ~$売上.xlsx … Excelで開いている間だけできるロックファイル（開くと壊れて見える）
    .DS_Store / desktop.ini / Thumbs.db … OSが勝手に作るもの
    """
    low = name.lower()
    return (name.startswith("~$") or name.startswith(".")
            or low in ("desktop.ini", "thumbs.db"))


def is_allowed(path: Path) -> bool:
    """許可フォルダの中にある実ファイルかどうか（.. やリンク経由の脱出を防ぐ）。"""
    try:
        real = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    if not real.is_file() or is_noise(real.name):
        return False
    # 拡張子は大小を無視する（Windows側では .CSV と .csv が混在しうる）
    if real.suffix.lower() not in config.IMPORT_EXTENSIONS:
        return False
    return any(real == d or d in real.parents for d in allowed_dirs())


def list_source_files() -> list[Path]:
    """取り込める候補ファイル（許可フォルダ配下を IMPORT_SCAN_DEPTH 階層まで探す）。

    件数は IMPORT_MAX_FILES で打ち切る（そこに達したかは呼び出し側で
    len() を見て判断する）。
    """
    seen: set = set()
    found: list[Path] = []
    for d in allowed_dirs():
        try:
            if not d.is_dir():
                continue
        except OSError:
            continue
        for p in _walk(d, config.IMPORT_SCAN_DEPTH):
            try:
                real = p.resolve()
            except OSError:
                continue
            # 同じファイルが複数の許可フォルダから見えることがあるので重複を除く
            if real in seen or not is_allowed(real):
                continue
            seen.add(real)
            found.append(real)
            if len(found) >= config.IMPORT_MAX_FILES:
                return sorted(found)
    return sorted(found)


def is_within_allowed(path: Path) -> bool:
    """許可フォルダの中にある実ファイルか（拡張子は問わない）。

    is_allowed() は「取り込めるファイルか」まで見るので拡張子で弾く。
    こちらは置き場所だけを見る。何が置いてあるかを一覧するためのもので、
    「読んでよいか」は呼び出し側が別に判断すること。
    """
    try:
        real = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    if not real.is_file() or is_noise(real.name):
        return False
    return any(real == d or d in real.parents for d in allowed_dirs())


def list_all_files(depth: int = 0, limit: int | None = None) -> list[Path]:
    """許可フォルダ配下のファイル（拡張子を問わない）。調査用。"""
    cap = limit or config.IMPORT_MAX_FILES
    seen: set = set()
    found: list[Path] = []
    for d in allowed_dirs():
        try:
            if not d.is_dir():
                continue
        except OSError:
            continue
        for p in _walk(d, depth, only_supported=False):
            try:
                real = p.resolve()
            except OSError:
                continue
            if real in seen:
                continue
            seen.add(real)
            found.append(real)
            if len(found) >= cap:
                return sorted(found)
    return sorted(found)


def display_name(path: Path) -> str:
    """画面に出す相対パス（許可フォルダからの位置）。"""
    for d in allowed_dirs():
        try:
            return str(path.relative_to(d))
        except ValueError:
            continue
    return path.name


def is_allowed_dir(path: Path) -> bool:
    """許可フォルダ自身か、その配下のフォルダか。"""
    try:
        real = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    if not real.is_dir():
        return False
    return any(real == d or d in real.parents for d in allowed_dirs())


def browse(path: str | None = None) -> dict:
    """フォルダの中身を1階層ぶん返す（エクスプローラ風の画面用）。

    path を省略すると許可フォルダの一覧を返す。
    許可フォルダの外は、パスを直接渡されても開かない。
    """
    roots = allowed_dirs()
    if not path:
        return {
            "path": "", "label": "取り込み元フォルダ", "parent": None,
            "dirs": [{"path": str(d), "name": str(d)} for d in roots if d.is_dir()],
            "files": [], "crumbs": [],
        }

    here = Path(path)
    if not is_allowed_dir(here):
        raise ImportError_("そのフォルダは開けません（許可されたフォルダの外です）。")
    here = here.resolve()

    dirs, files = [], []
    try:
        for p in sorted(here.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            try:
                if p.is_dir():
                    if not is_noise(p.name):
                        dirs.append({"path": str(p), "name": p.name})
                elif p.suffix.lower() in config.IMPORT_EXTENSIONS and not is_noise(p.name):
                    files.append({"path": str(p), "name": p.name,
                                  "size": p.stat().st_size,
                                  "mtime": datetime.fromtimestamp(
                                      p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")})
            except OSError:
                continue
    except PermissionError:
        raise ImportError_(f"読み取り権限がありません: {here}") from None
    except OSError as e:
        raise ImportError_(f"開けませんでした: {here}（{e.strerror or e}）") from None

    # パンくず。許可フォルダより上には遡らせない
    root = next((d for d in roots if here == d or d in here.parents), None)
    crumbs, cur = [], here
    while root is not None and cur != root:
        crumbs.append({"path": str(cur), "name": cur.name})
        cur = cur.parent
    crumbs.append({"path": str(root), "name": str(root)})
    crumbs.reverse()
    parent = str(here.parent) if (root is not None and here != root) else ""
    return {"path": str(here), "label": here.name or str(here), "parent": parent,
            "dirs": dirs, "files": files, "crumbs": crumbs}


def check_readable(path: Path) -> None:
    if not is_allowed(path):
        raise ImportError_("許可されたフォルダの中のファイルではありません。")
    mb = path.stat().st_size / (1024 * 1024)
    if mb > config.IMPORT_MAX_FILE_MB:
        raise ImportError_(
            f"ファイルが大きすぎます（{mb:.1f}MB / 上限 {config.IMPORT_MAX_FILE_MB}MB）。")


# =============================================================================
# 読み込み
# =============================================================================

def sheet_names(path: Path) -> list[str]:
    """Excelのシート名。CSVなら空リスト。"""
    if path.suffix.lower() not in (".xlsx", ".xlsm"):
        return []
    check_readable(path)
    return _sheet_names_of(path)


def _sheet_names_of(src) -> list[str]:
    try:
        import openpyxl
        wb = openpyxl.load_workbook(src, read_only=True, data_only=True)
        try:
            return list(wb.sheetnames)
        finally:
            wb.close()
    except Exception as e:
        raise ImportError_(f"Excelを開けませんでした: {e}") from e


# CSVの文字コード。上から順に試す。
CSV_ENCODINGS = ["utf-8-sig", "cp932", "utf-8", "shift_jis", "euc_jp"]


# --- アップロードされたファイル（サーバのフォルダには置かない） --------------------

def check_upload(data: bytes, filename: str) -> str:
    """アップロードの受け入れ判定。戻り値は正規化した拡張子。"""
    if not config.IMPORT_ALLOW_UPLOAD:
        raise ImportError_("アップロードからの取り込みは無効化されています（IMPORT_ALLOW_UPLOAD）。")
    ext = Path(filename or "").suffix.lower()
    if ext not in config.IMPORT_EXTENSIONS:
        raise ImportError_(
            f"扱えない形式です（{ext or '拡張子なし'}）。{'、'.join(config.IMPORT_EXTENSIONS)} のみ対応です。")
    mb = len(data) / (1024 * 1024)
    if mb > config.IMPORT_MAX_FILE_MB:
        raise ImportError_(
            f"ファイルが大きすぎます（{mb:.1f}MB / 上限 {config.IMPORT_MAX_FILE_MB}MB）。")
    return ext


def upload_sheet_names(data: bytes, filename: str) -> list[str]:
    if check_upload(data, filename) not in (".xlsx", ".xlsm"):
        return []
    import io
    return _sheet_names_of(io.BytesIO(data))


def read_upload(data: bytes, filename: str, sheet: str | None = None, header_row: int = 0,
                delimiter: str | None = None, nrows: int | None = None) -> pd.DataFrame:
    """アップロードされたバイト列を DataFrame として読む。ディスクには書かない。"""
    import io
    ext = check_upload(data, filename)
    try:
        if ext in (".xlsx", ".xlsm"):
            return pd.read_excel(io.BytesIO(data), sheet_name=sheet or 0,
                                 header=header_row, nrows=nrows, dtype=object)
        sep = delimiter if delimiter else ("\t" if ext == ".tsv" else None)
        last = None
        for enc in CSV_ENCODINGS:
            try:
                return pd.read_csv(io.BytesIO(data), header=header_row, nrows=nrows,
                                   dtype=object, sep=sep, engine="python", encoding=enc)
            except UnicodeDecodeError as e:
                last = e
        raise ImportError_(
            "文字コードを判定できませんでした。UTF-8 か Shift_JIS で保存し直してください。"
            f"（{last}）")
    except ImportError_:
        raise
    except Exception as e:
        raise ImportError_(f"ファイルを読めませんでした: {e}") from e


# 画面に出す区切り文字の選択肢（.txt は区切りがまちまちなので選べるようにする）
DELIMITERS = {
    "自動判定": None,
    "カンマ ( , )": ",",
    "タブ": "\t",
    "パイプ ( | )": "|",
    "セミコロン ( ; )": ";",
    "空白（連続もまとめる）": r"\s+",
}


def _explain_read_error(e: Exception, path: Path, sheet: str | None) -> str:
    """pandas / OS の例外を、管理者がそのまま対処できる日本語にする。

    定期取り込みの失敗はメール・⚠マーク・AIの注記にこの文がそのまま載るので、
    'No columns to parse from file' のような英語のままでは何をすればよいか分からない。
    """
    name = path.name
    if isinstance(e, PermissionError):
        return (f"{name} を開けません。他のプログラム（Excel など）で開かれているか、"
                "読み取り権限がありません。閉じてから、次回の実行を待つか「今すぐ更新」してください。")
    if isinstance(e, FileNotFoundError):
        return f"{name} が見つかりません（移動・削除された可能性）。"
    msg = str(e)
    if "No columns to parse" in msg or isinstance(e, pd.errors.EmptyDataError):
        return f"{name} の中身が空です（0バイト、または見出し行がありません）。"
    if "Worksheet named" in msg or "Worksheet index" in msg:
        try:
            names = _sheet_names_of(path)
            have = "、".join(names) if names else "（なし）"
        except Exception:
            have = "（不明）"
        return (f"シート「{sheet}」が {name} にありません（シート名が変わった可能性）。"
                f"いまあるシート: {have}。設定のシートを直してください。")
    if "BadZipFile" in type(e).__name__ or "not a zip file" in msg.lower() or "File is not a zip file" in msg:
        return (f"{name} を Excel ファイルとして開けません（壊れているか、拡張子だけ .xlsx の別形式）。"
                "Excel で開いて保存し直してください。")
    if isinstance(e, pd.errors.ParserError):
        return f"{name} を表として読めませんでした（行ごとの列数が揃っていない等）: {msg}"
    return f"{name} を読めませんでした: {msg}"


def read_table(path: Path, sheet: str | None = None, header_row: int = 0,
               encoding: str | None = None, nrows: int | None = None,
               delimiter: str | None = None) -> pd.DataFrame:
    """ファイルを DataFrame として読む。

    header_row は0始まり。見出しが2行目にあるなら 1 を渡す。
    delimiter は CSV/TSV/TXT 用。None なら .tsv はタブ、それ以外は自動判定。
    """
    check_readable(path)
    ext = path.suffix.lower()
    try:
        if ext in (".xlsx", ".xlsm"):
            df = pd.read_excel(path, sheet_name=sheet or 0, header=header_row,
                               nrows=nrows, dtype=object)
        else:
            if path.stat().st_size == 0:
                raise ImportError_(f"{path.name} の中身が空です（0バイト）。")
            sep = delimiter if delimiter else ("\t" if ext == ".tsv" else None)
            last = None
            for enc in ([encoding] if encoding else CSV_ENCODINGS):
                try:
                    df = pd.read_csv(path, header=header_row, nrows=nrows, dtype=object,
                                     sep=sep, engine="python", encoding=enc)
                    break
                except UnicodeDecodeError as e:
                    last = e
            else:
                raise ImportError_(
                    f"{path.name} の文字コードを判定できませんでした。テキスト（CSV）ではないか、"
                    "壊れている可能性があります。UTF-8 か Shift_JIS で保存し直してください。"
                    f"（{last}）")
    except ImportError_:
        raise
    except Exception as e:
        raise ImportError_(_explain_read_error(e, path, sheet)) from e

    if df.empty and not len(df.columns):
        raise ImportError_(f"{path.name} の中身が空のようです（見出し行の指定を確認してください）。")
    return df


# =============================================================================
# 名前と型の正規化
# =============================================================================

def safe_name(name: str, fallback: str = "col") -> str:
    """SQLiteで扱いやすい識別子にする（日本語はそのまま残す）。

    記号と空白を _ にし、数字始まり・予約語・空文字を避ける。
    """
    s = unicodedata.normalize("NFKC", str(name)).strip()
    s = re.sub(r"[^\w]", "_", s, flags=re.UNICODE)   # \w は日本語も含む
    s = re.sub(r"_+", "_", s).strip("_")
    if not s:
        return fallback
    if s[0].isdigit():
        s = "_" + s
    if s.lower() in _RESERVED:
        s = s + "_"
    return s[:64]


def unique_names(names: list[str]) -> list[str]:
    """列名の重複を _2, _3 … で解消する。"""
    out, used = [], {}
    for i, n in enumerate(names):
        base = safe_name(n, fallback=f"col{i + 1}")
        if base in used:
            used[base] += 1
            base = f"{base}_{used[base]}"
        else:
            used[base] = 1
        out.append(base)
    return out


def infer_type(series: pd.Series) -> str:
    """列の中身から SQLite の型を決める（判断できなければ TEXT）。"""
    s = series.dropna()
    s = s[s.astype(str).str.strip() != ""]
    if s.empty:
        return "TEXT"
    num = pd.to_numeric(s, errors="coerce")
    if num.notna().all():
        # 小数点を含まず整数で表せるなら INTEGER
        if (num == num.round()).all() and num.abs().max() < 2 ** 63:
            return "INTEGER"
        return "REAL"
    return "TEXT"


def plan_columns(df: pd.DataFrame) -> list[dict]:
    """列ごとの「元の名前 / 使う名前 / 型」の一覧を作る。"""
    names = unique_names([str(c) for c in df.columns])
    return [{"元の列名": str(orig), "列名": name, "型": infer_type(df[orig])}
            for orig, name in zip(df.columns, names)]


def _cast(series: pd.Series, sqlite_type: str):
    """SQLiteに渡せる素のPython値（int / float / str / None）に揃える。

    numpy の int64 などをそのまま渡すと、sqlite3 がバッファとみなして
    BLOB で保存してしまう（数値として比較も集計もできなくなる）。
    """
    if sqlite_type == "INTEGER":
        num = pd.to_numeric(series, errors="coerce")
        return num.map(lambda v: None if pd.isna(v) else int(v))
    if sqlite_type == "REAL":
        num = pd.to_numeric(series, errors="coerce")
        return num.map(lambda v: None if pd.isna(v) else float(v))
    return series.map(lambda v: None if v is None or pd.isna(v) else str(v))


def prepare_frame(df: pd.DataFrame, columns: list[dict]):
    """型を当てはめた DataFrame と、TEXTに落とした列名の一覧を返す。

    型は先頭数千行から推定するので、後ろの行に数値でない値が混ざることがある。
    そのまま数値に変換すると黙ってNULLになって値が消えるため、
    1件でも変換できない値があればその列は TEXT に落とす。
    """
    out, degraded = {}, []
    for c in columns:
        src = df[c["元の列名"]]
        if c["型"] in ("INTEGER", "REAL"):
            num = pd.to_numeric(src, errors="coerce")
            filled = src.notna() & (src.astype(str).str.strip() != "")
            if bool((filled & num.isna()).any()):
                c["型"] = "TEXT"
                degraded.append(c["列名"])
        out[c["列名"]] = _cast(src, c["型"])
    return pd.DataFrame(out), degraded


# =============================================================================
# 書き込み（このアプリで唯一DBに書く場所）
# =============================================================================

def db_path_for(name: str) -> Path:
    """新しいDBファイルのパス。data/ の直下に限定する。"""
    stem = safe_name(name, fallback="")
    if not stem:
        raise ImportError_("DB名を入力してください（英数字・かな・漢字が使えます）。")
    return (config.DATA_DIR / f"{stem}.db")


def _qi(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def existing_tables(db_path: Path) -> list[str]:
    if not db_path.exists():
        return []
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def table_columns(db_path: Path, table: str) -> list[str]:
    if not db_path.exists():
        return []
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({_qi(table)})")]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def import_dataframe(db_path: Path, table: str, df: pd.DataFrame, columns: list[dict],
                     mode: str = "create", timestamp_col: str | None = None,
                     timestamp_value: str | None = None):
    """DataFrame を1テーブルとして書き込む。

    mode: create=新規作成 / replace=作り直す / append=既存に追記
    timestamp_col: 指定すると、その名前の列に取り込み日時を入れて一緒に書く。
                   追記を重ねたとき「いつ取り込んだ分か」を後から絞れるようにするため。
    戻り値: (書き込んだ行数, 型をTEXTに落とした列名の一覧)
    """
    if db_path.parent.resolve() != config.DATA_DIR.resolve():
        raise ImportError_("DBファイルは data/ の直下にしか作れません。")
    table = safe_name(table, fallback="")
    if not table:
        raise ImportError_("テーブル名を入力してください。")
    if len(df) > config.IMPORT_MAX_ROWS:
        raise ImportError_(
            f"行数が多すぎます（{len(df):,}行 / 上限 {config.IMPORT_MAX_ROWS:,}行）。")
    if not columns:
        raise ImportError_("取り込む列がありません。")

    data, degraded = prepare_frame(df, columns)

    write_cols = [{"列名": c["列名"], "型": c["型"]} for c in columns]
    if timestamp_col:
        ts_name = safe_name(timestamp_col, fallback="取得日時")
        if ts_name in {c["列名"] for c in write_cols}:
            raise ImportError_(
                f"取得日時の列名 '{ts_name}' が元データの列名とぶつかっています。別の名前にしてください。")
        stamp = timestamp_value or datetime.now().isoformat(timespec="seconds")
        data[ts_name] = stamp
        write_cols.append({"列名": ts_name, "型": "TEXT"})

    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    # timeout: 裏のスケジューラと画面からの手動更新が重なっても、即エラーにせず順番待ちする
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        have = table in existing_tables(db_path) if db_path.exists() else False
        if mode == "create" and have:
            raise ImportError_(f"テーブル '{table}' は既にあります。"
                               "「作り直す」か「追記する」を選ぶか、別の名前にしてください。")
        if mode == "replace":
            conn.execute(f"DROP TABLE IF EXISTS {_qi(table)}")
            have = False
        if not have:
            cols_sql = ", ".join(f"{_qi(c['列名'])} {c['型']}" for c in write_cols)
            conn.execute(f"CREATE TABLE {_qi(table)} ({cols_sql})")
        else:
            # 追記先に無い列があると INSERT が落ちるので、先に照合して分かる形で止める。
            # 取得日時だけは後から足せるので ALTER で追加する。
            have_cols = set(table_columns(db_path, table))
            missing = [c["列名"] for c in write_cols if c["列名"] not in have_cols]
            if timestamp_col and safe_name(timestamp_col, "取得日時") in missing:
                ts_name = safe_name(timestamp_col, "取得日時")
                conn.execute(f"ALTER TABLE {_qi(table)} ADD COLUMN {_qi(ts_name)} TEXT")
                missing.remove(ts_name)
            if missing:
                raise ImportError_(
                    f"追記先の '{table}' に無い列があります: {', '.join(missing)}。"
                    "列名を合わせるか、「作り直す」を選んでください。")

        placeholders = ", ".join("?" for _ in write_cols)
        cols_list = ", ".join(_qi(c["列名"]) for c in write_cols)
        rows = list(data[[c["列名"] for c in write_cols]]
                    .itertuples(index=False, name=None))   # _cast で素の値に揃え済み
        conn.executemany(
            f"INSERT INTO {_qi(table)} ({cols_list}) VALUES ({placeholders})", rows)
        conn.commit()
        return len(rows), degraded
    except sqlite3.Error as e:
        conn.rollback()
        raise ImportError_(f"書き込みに失敗しました: {e}") from e
    finally:
        conn.close()


def prune_runs(db_path: Path, table: str, timestamp_col: str, keep: int) -> int:
    """取得日時の新しい keep 回分だけ残し、それより古い回を削除する。

    「回」は取得日時の値の種類で数える（1回の取り込みで入った行は同じ値を持つ）。
    取得日時が NULL の行 ―― この仕組みを入れる前から入っていた行 ―― は消さない。
    戻り値は削除した行数。
    """
    keep = int(keep)
    if keep < 1 or not timestamp_col:
        return 0
    table, ts = safe_name(table), safe_name(timestamp_col, "取得日時")
    if ts not in table_columns(db_path, table):
        return 0
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        cur = conn.execute(
            f"DELETE FROM {_qi(table)} "
            f"WHERE {_qi(ts)} IS NOT NULL AND {_qi(ts)} NOT IN "
            f"(SELECT {_qi(ts)} FROM {_qi(table)} WHERE {_qi(ts)} IS NOT NULL "
            f" GROUP BY {_qi(ts)} ORDER BY {_qi(ts)} DESC LIMIT ?)", (keep,))
        removed = cur.rowcount or 0
        conn.commit()
        return removed
    except sqlite3.Error as e:
        conn.rollback()
        raise ImportError_(f"古い取り込み分の削除に失敗しました: {e}") from e
    finally:
        conn.close()


def table_info(db_path: Path, table: str, timestamp_col: str | None = None) -> dict:
    """1テーブルの中身の要約。「DBの管理」画面で状態を確かめるために使う。

    取得日時の列は、ジョブに設定があればそれを、無ければ既定の列名を探す
    （画面から手で取り込んだテーブルにも付いているため）。
    """
    cols = table_columns(db_path, table)
    ts = None
    for cand in (timestamp_col, config.IMPORT_TIMESTAMP_COLUMN):
        if cand and safe_name(cand, "") in cols:
            ts = safe_name(cand, "")
            break

    info = {"name": table, "columns": cols, "column_count": len(cols),
            "rows": 0, "timestamp_column": ts, "runs": None,
            "latest": None, "oldest": None}
    if not db_path.exists():
        return info
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        info["rows"] = conn.execute(f"SELECT COUNT(*) FROM {_qi(table)}").fetchone()[0]
        if ts:
            row = conn.execute(
                f"SELECT COUNT(DISTINCT {_qi(ts)}), MIN({_qi(ts)}), MAX({_qi(ts)}) "
                f"FROM {_qi(table)} WHERE {_qi(ts)} IS NOT NULL").fetchone()
            info["runs"], info["oldest"], info["latest"] = row[0], row[1], row[2]
    except sqlite3.Error as e:
        info["error"] = str(e)
    finally:
        conn.close()
    return info


def sample_rows(db_path: Path, table: str, limit: int | None = None,
                timestamp_col: str | None = None) -> dict:
    """テーブルの中身を数行だけ覗く（読み取り専用）。

    取得日時の列があれば新しい順に取る。「さっきの取り込みがちゃんと入ったか」を
    確かめるのが主な用途なので、先頭から取ると古い行しか見えず役に立たない。
    """
    table = safe_name(table)
    limit = int(limit or config.IMPORT_SAMPLE_ROWS)
    cols = table_columns(db_path, table)
    out = {"table": table, "columns": cols, "rows": [], "order_by": None, "limit": limit}
    if not db_path.exists() or not cols:
        out["error"] = "テーブルが見つかりません。"
        return out

    ts = None
    for cand in (timestamp_col, config.IMPORT_TIMESTAMP_COLUMN):
        if cand and safe_name(cand, "") in cols:
            ts = safe_name(cand, "")
            break
    order = f" ORDER BY {_qi(ts)} DESC" if ts else ""
    out["order_by"] = ts

    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        cur = conn.execute(f"SELECT * FROM {_qi(table)}{order} LIMIT ?", (limit,))
        out["columns"] = [d[0] for d in cur.description]
        # BLOB はそのままだとJSONに載らないので、見える形に潰しておく
        out["rows"] = [[v.hex()[:32] if isinstance(v, (bytes, bytearray)) else v
                        for v in row] for row in cur.fetchall()]
    except sqlite3.Error as e:
        out["error"] = str(e)
    finally:
        conn.close()
    return out


def run_count(db_path: Path, table: str, timestamp_col: str) -> int:
    """いま何回分の取り込みが入っているか。"""
    ts = safe_name(timestamp_col or "", "")
    if not ts or ts not in table_columns(db_path, safe_name(table)):
        return 0
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        row = conn.execute(
            f"SELECT COUNT(DISTINCT {_qi(ts)}) FROM {_qi(safe_name(table))} "
            f"WHERE {_qi(ts)} IS NOT NULL").fetchone()
        return int(row[0]) if row else 0
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def drop_table(db_path: Path, table: str) -> None:
    if db_path.parent.resolve() != config.DATA_DIR.resolve():
        raise ImportError_("data/ の外は操作できません。")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(f"DROP TABLE IF EXISTS {_qi(table)}")
        conn.commit()
    finally:
        conn.close()
