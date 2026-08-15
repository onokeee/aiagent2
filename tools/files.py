"""取り込み元フォルダを調べるツール（読むだけ）。

「取り込み元に何が来ているか」「このCSVはどんな列か」「まだ取り込んでいない
ファイルはあるか」に答えるためのもの。DBに入る前のファイルの話なので、
SQLでは答えられない。

安全のうえで大事なところは、すべて importer.py の既存の仕組みに任せる。
  allowed_dirs()   … 読んでよいフォルダ（env の IMPORT_DIRS ＋画面で追加した分）
  is_allowed()     … .. やリンクで許可フォルダの外へ出ようとしても弾く
  check_readable() … 読む直前にもう一度確かめる
ここで新しくパスの判定を書かない（守りの仕組みを二重に持つと必ずズレる）。

できるのは一覧と下見だけ。取り込み・作成・変更・削除は一切しない。
取り込みの実行は今までどおり「データ取り込み」画面の操作に限る。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import config
import filecheck
import history
import importer
from .common import _err, _report_result

#: 一覧で返す最大件数。多すぎるとLLMに渡すだけで無駄になる。
_MAX_ROWS = 300
#: 下見で見せる行数の上限。
_MAX_PREVIEW_ROWS = 20
#: 一覧でまとめて形を判定するときの上限。1件ずつ開くので数を抑える。
_MAX_CHECK = 20


def _table(name: str, columns: list, rows: list) -> dict:
    return {"name": name, "columns": columns, "rows": [tuple(r) for r in rows]}


def _size(n: int | None) -> str:
    if n is None:
        return ""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


def _mtime(p: Path) -> str:
    try:
        return datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    except OSError:
        return ""


def _imported_index() -> dict:
    """取り込み済みファイルの索引。パスの表記ゆれに備えて小文字で引く。"""
    out = {}
    for src, rec in history.latest_by_source().items():
        out[src.strip().lower()] = rec
    return out


def _import_state(p: Path, index: dict) -> str:
    """そのファイルが取り込み済みかどうかの一言。"""
    rec = index.get(str(p).lower())
    if rec is None:
        # パスが変わっていても、ファイル名が一致すれば手がかりにはなる
        rec = next((r for key, r in index.items()
                    if Path(key).name == p.name.lower()), None)
        if rec is None:
            return "未取り込み"
        return (f"同名を取り込み済み（{rec.get('db_file')} / {rec.get('table')}"
                f"・{rec.get('at', '')[:16]}）")
    mark = "" if rec.get("ok") else "／前回は失敗"
    return f"{rec.get('db_file')} / {rec.get('table')}（{rec.get('at', '')[:16]}{mark}）"


def _roots_table() -> dict:
    """許可フォルダの状態。マウント切れや権限なしをここで切り分ける。"""
    rows = [[r["設定値"], r["状態"], r["source"]] for r in importer.dir_status()]
    return _table("取り込み元フォルダ", ["フォルダ", "状態", "設定元"], rows)


def _supported(p: Path) -> bool:
    return p.suffix.lower() in config.IMPORT_EXTENSIONS


def _listing(args: dict) -> dict:
    path = str(args.get("path") or "").strip()
    recursive = bool(args.get("recursive"))
    pattern = str(args.get("pattern") or "").strip().lower()
    only_new = bool(args.get("only_not_imported"))
    check = bool(args.get("check"))
    index = _imported_index()

    roots = importer.allowed_dirs()
    if not roots:
        return _err("取り込み元フォルダが設定されていません。"
                    "「データ取り込み」画面で追加するか、env の IMPORT_DIRS を設定してください。")

    if path and not importer.is_allowed_dir(Path(path)):
        return _err(f"そのフォルダは見られません（許可フォルダの外です）: {path}。"
                    f"見られるのは {'、'.join(str(d) for d in roots)} の中だけです。")

    here = Path(path).resolve() if path else None
    dirs: list = []
    files: list[Path] = []

    # 一覧は拡張子で絞らない。「何が置いてあるか」を知るのが目的なので、
    # 取り込めない形式（PDFなど）も見せて、可否は列で示す。
    if recursive:
        for p in importer.list_all_files():
            if here is None or here == p.parent or here in p.parents:
                files.append(p)
    else:
        for d in ([here] if here is not None else roots):
            try:
                entries = sorted(d.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
            except OSError:
                continue
            for p in entries:
                try:
                    if p.is_dir():
                        if not importer.is_noise(p.name):
                            dirs.append(str(p.relative_to(d)) if here is not None
                                        else f"{d.name}/{p.name}")
                    elif importer.is_within_allowed(p):
                        files.append(p.resolve())
                except OSError:
                    continue

    if pattern:
        files = [f for f in files if pattern in f.name.lower()]
        dirs = [d for d in dirs if pattern in str(d).lower()]

    rows, checked = [], 0
    for p in files:
        state = _import_state(p, index)
        if only_new and state != "未取り込み":
            continue
        if not _supported(p):
            shape = "取り込み対象外の形式"
        elif check and checked < _MAX_CHECK:
            checked += 1
            try:
                shape = filecheck.summary_line(filecheck.inspect(p))
            except Exception as e:
                shape = f"判定できず（{type(e).__name__}）"
        else:
            shape = "未判定"
        rows.append([importer.display_name(p), p.suffix.lower().lstrip(".") or "（なし）",
                     _size(p.stat().st_size if p.exists() else None), _mtime(p),
                     state, shape])
    rows.sort(key=lambda r: r[3], reverse=True)      # 新しいものから
    shown = rows[:_MAX_ROWS]

    tables = [_roots_table()]
    if dirs:
        tables.append(_table("フォルダ", ["名前"], [[d] for d in sorted(set(dirs))]))
    tables.append(_table("ファイル",
                         ["場所", "種類", "サイズ", "更新日時", "取り込み状況", "表として使えるか"],
                         shown))

    supported = [r for r in rows if r[5] != "取り込み対象外の形式"]
    notes = [f"{len(rows)} 件のファイルが見つかりました"
             + (f"（多いので新しい順に {len(shown)} 件だけ載せています）"
                if len(rows) > len(shown) else "")
             + f"。うち取り込みに対応した形式は {len(supported)} 件です"
             + f"（対応: {'、'.join(config.IMPORT_EXTENSIONS)}）。"]
    if not recursive and not path:
        notes.append("いまは許可フォルダの直下だけを見ています。"
                     "下の階層も見るなら recursive=true、"
                     "特定のフォルダを見るなら path を指定してください。")
    if not check:
        notes.append("「表として使えるか」は check=true を付けると調べます"
                     "（1件ずつ開くので、多いときは絞ってから）。")
    elif checked >= _MAX_CHECK:
        notes.append(f"判定は {_MAX_CHECK} 件までにしています。"
                     "pattern や path で絞ると続きを見られます。")
    bad = [r for r in rows if r[5].startswith(("取り込みに向かない", "手直しが要る"))]
    if bad:
        notes.append("そのままでは取り込めないものがあります: "
                     + "、".join(f"{r[0]}（{r[5]}）" for r in bad[:3]))
    fresh = [r for r in rows if r[4] == "未取り込み"]
    if fresh:
        notes.append(f"まだ取り込んでいないファイルが {len(fresh)} 件あります: "
                     + "、".join(r[0] for r in fresh[:5])
                     + ("（ほか）" if len(fresh) > 5 else ""))
    if not rows and not dirs:
        notes.append("ファイルはありませんでした。フォルダが空か、権限が無い可能性があります。")
    notes.append("中身と形を詳しく見るには file にパスを指定してください。"
                 "実際に取り込むのは「データ取り込み」画面の操作です。ここでは読むだけです。")

    return _report_result({"title": "取り込み元フォルダの中身",
                           "tables": tables, "notes": notes,
                           "meta": {"files": len(rows), "supported": len(supported),
                                    "not_imported": len(fresh)}})


def _issues_table(res: dict) -> dict:
    return _table("見つかった問題", ["深刻度", "内容", "直し方"],
                  [[i["level"], i["text"], i["fix"]] for i in res["issues"]]
                  or [["—", "気になる点はありませんでした。", ""]])


def _preview(args: dict) -> dict:
    raw = Path(str(args.get("file") or "").strip())
    # 場所の判定と「読める形式か」の判定を分ける。対象外の形式でも
    # 「なぜ取り込めないか」は答えられるようにする。
    if not importer.is_within_allowed(raw):
        return _err(f"そのファイルは見られません: {raw}。"
                    "許可フォルダの中のファイルだけを指定してください。"
                    "パスは一覧（file を指定しない呼び方）で得たものを使ってください。")
    target = raw.resolve()

    if not _supported(target):
        res = filecheck.inspect(target)
        return _report_result({
            "title": f"{target.name} は取り込みに対応していない形式です",
            "tables": [_issues_table(res)],
            "notes": [f"{importer.display_name(target)}"
                      f"（{_size(target.stat().st_size)}・更新 {_mtime(target)}）",
                      f"扱えるのは {'、'.join(config.IMPORT_EXTENSIONS)} です。"
                      "中身は読んでいません。"],
            "meta": {"verdict": res["verdict"]}})

    rows_want = max(1, min(int(args.get("rows") or 5), _MAX_PREVIEW_ROWS))

    try:
        sheets = importer.sheet_names(target)
    except importer.ImportError_ as e:
        return _err(str(e))
    sheet = args.get("sheet") or (sheets[0] if sheets else None)

    # まず形を見る。見出しが1行目に無ければ、その行で読み直す
    try:
        res = filecheck.inspect(target, sheet=sheet)
    except Exception as e:
        res = {"verdict": "判定できず", "issues": [
            {"level": "低", "text": f"形を調べられませんでした: {e}", "fix": ""}],
            "header_row": 0, "shape": {}}
    header_row = (int(args["header_row"]) if args.get("header_row") is not None
                  else int(res.get("header_row") or 0))

    try:
        df = importer.read_table(target, sheet=sheet, header_row=header_row,
                                 nrows=rows_want)
    except importer.ImportError_ as e:
        return _err(f"{target.name}: {e}")
    except Exception as e:
        return _err(f"{target.name} を読めませんでした: {e}")

    plan = importer.plan_columns(df)
    tables = [
        _table("判定", ["項目", "内容"],
               [["そのまま取り込めるか", res["verdict"]],
                ["見出しの行", f"{header_row + 1} 行目"],
                *[[k, v] for k, v in (res.get("shape") or {}).items() if k != "見出し行"]]),
        _issues_table(res),
        _table("列", ["元の列名", "取り込み後の列名", "推定される型"],
               [[c["元の列名"], c["列名"], c["型"]] for c in plan]),
        _table(f"先頭 {len(df)} 行", [str(c) for c in df.columns],
               [[("" if v is None else v) for v in r] for r in df.values.tolist()]),
    ]
    if sheets:
        tables.insert(0, _table("シート", ["シート名"], [[s] for s in sheets]))

    index = _imported_index()
    notes = [f"{importer.display_name(target)}（{_size(target.stat().st_size)}"
             f"・更新 {_mtime(target)}）",
             f"判定: {res['verdict']}",
             f"取り込み状況: {_import_state(target, index)}"]
    for i in res.get("issues", []):
        if i["level"] == "高":
            notes.append(f"{i['text']} → {i['fix']}")
    if sheets:
        notes.append(f"シートは {len(sheets)} 枚あります（いま見ているのは「{sheet}」）。"
                     "シートごとに形が違うので、使いたいシートを sheet で指定して確かめてください。")
    if res.get("encoding"):
        notes.append(f"文字コード: {res['encoding']} / 区切り: {res.get('delimiter')}")
    if header_row:
        notes.append(f"見出しが1行目ではないので、{header_row + 1} 行目を見出しとして読みました。"
                     f"取り込み画面でも「見出しの行」に {header_row + 1} を指定してください。")
    if df.empty:
        notes.append("中身の行が読めませんでした。header_row を変えて試してください。")
    notes.append("型は中身からの推定で、取り込み画面で直せます。"
                 "ここでは読むだけで、取り込みはしていません。")

    return _report_result({"title": f"{target.name} の下見（{res['verdict']}）",
                           "tables": tables, "notes": notes,
                           "meta": {"verdict": res["verdict"], "columns": len(plan),
                                    "header_row": header_row, "sheets": sheets}})


def _explore_import_files(args: dict, scope: list[dict]) -> dict:
    try:
        return _preview(args) if str(args.get("file") or "").strip() else _listing(args)
    except importer.ImportError_ as e:
        return _err(str(e))
    except PermissionError:
        return _err("読み取り権限がありません。共有フォルダの権限を確認してください。")
    except OSError as e:
        return _err(f"フォルダにアクセスできませんでした: {e.strerror or e}")


HANDLERS = {"explore_import_files": _explore_import_files}

# SQLは受け取らない
SQL_TOOLS: set = set()

# 管理者だけに渡すツール。「データ取り込み」画面が管理者専用なので、
# AI経由なら誰でも中身が見られる、という抜け道を作らない。
ADMIN_TOOLS = {"explore_import_files"}
