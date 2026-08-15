"""データ取り込み画面。Excel / CSV / TXT から DB・テーブルを作り、定期更新も設定する。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from flask import Blueprint, g, jsonify, render_template, request

import catalog
import cleanup
import config
import db
import history
import importer
import jobs
import scheduler

from . import filestore
from .helpers import admin_required, jsonable

bp = Blueprint("imp", __name__)


@bp.get("/import")
@admin_required
def index():
    return render_template(
        "import.html",
        dirs=importer.dir_status(),
        files=[{"path": str(p), "label": importer.display_name(p)}
               for p in importer.list_source_files()],
        max_files=config.IMPORT_MAX_FILES,
        extensions=", ".join(config.IMPORT_EXTENSIONS),
        delimiters=list(importer.DELIMITERS),
        db_files=[f.name for f in db.list_db_files()],
        existing={f.name: importer.existing_tables(f) for f in db.list_db_files()},
        manage=_manage_view(),
        intervals=list(jobs.INTERVALS),
        modes=jobs.MODES,
        default_ts=config.IMPORT_TIMESTAMP_COLUMN,
        max_keep=jobs.MAX_KEEP_RUNS,
        default_keep=jobs.DEFAULT_KEEP_RUNS,
        dirs_editable=config.IMPORT_DIRS_EDITABLE,
        allow_upload=config.IMPORT_ALLOW_UPLOAD,
    )


def _manage_view() -> dict:
    """「DBの管理」タブが必要とするもの一式。

    定期取り込みは対象テーブルに紐づけて見せるので、DB→テーブル→そのテーブルを
    更新するジョブ、という並びにまとめる。テーブルが消えた・まだ作られていない
    ジョブは宙に浮くので orphans に分けて、画面から必ず触れるようにする。
    """
    by_target: dict[tuple, list[dict]] = {}
    for j in jobs.list_jobs():
        by_target.setdefault((j.get("db_file"), j.get("table")), []).append(j)

    used: set[tuple] = set()
    dbs = []
    for f in db.list_db_files():
        try:
            names = importer.existing_tables(f)
        except Exception as e:
            dbs.append({"name": f.name, "error": str(e), "tables": []})
            continue
        tables = []
        for t in names:
            js = by_target.get((f.name, t), [])
            if js:
                used.add((f.name, t))
            info = importer.table_info(f, t, js[0].get("timestamp_column") if js else None)
            tables.append({**info, "jobs": [_job_row(j) for j in js]})
        st = f.stat()
        dbs.append({"name": f.name, "size": st.st_size,
                    "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                    "tables": tables})

    orphans = [_job_row(j) for key, js in by_target.items() if key not in used for j in js]
    return {"dbs": dbs, "orphans": orphans, "locked": _locked_tables(),
            "sched": scheduler.status()}


def _locked_tables() -> dict:
    """手で更新してはいけないテーブル。{DBファイル: {テーブル: 理由}}

    定期実行＋追記のテーブルは、画面からの1回きりの取り込みでも
    余計な取得日時が1回ぶん増えて更新間隔が崩れるので、そちらも止める。
    """
    out: dict[str, dict] = {}
    for j in jobs.list_jobs():
        why = jobs.manual_run_blocked(j)
        if why:
            out.setdefault(j.get("db_file", ""), {})[j.get("table", "")] = why
    return out


def _job_row(j: dict) -> dict:
    nxt = jobs.next_run_at(j)
    kept = None
    if j.get("timestamp_column"):
        try:
            kept = importer.run_count(config.DATA_DIR / j["db_file"], j["table"],
                                      j["timestamp_column"])
        except Exception:
            kept = None
    # 画面はこれらのキーを必ず読むので、古い定義や手書きのジョブでも欠けないよう埋める
    defaults = {"sheet": None, "delimiter": None, "header_row": 0, "start_at": "",
                "timestamp_column": None, "keep_runs": None, "enabled": True,
                "last_run": "", "last_status": "", "last_message": "", "columns": []}
    return {**defaults, **j,
            "interval_label": jobs.interval_label(j.get("interval_minutes", 0)),
            "mode_label": "追記" if j.get("mode") == "append" else "洗い替え",
            "source_label": importer.display_name(Path(j.get("source", ""))),
            "kept": kept,
            "manual_blocked": jobs.manual_run_blocked(j),
            "next_label": nxt.strftime("%m-%d %H:%M") if nxt else "手動のみ"}


# =============================================================================
# 取り込み元フォルダの管理とファイル選択
# =============================================================================

@bp.get("/api/import/dirs")
@admin_required
def dirs_list():
    return jsonify({"dirs": [{k: (str(v) if k == "path" else v) for k, v in d.items()}
                             for d in importer.dir_status()],
                    "editable": config.IMPORT_DIRS_EDITABLE and bool(g.user.is_admin)})


@bp.post("/api/import/dirs")
@admin_required
def dirs_edit():
    """取り込み元フォルダの追加・削除。読める範囲が広がる操作なので管理者だけ。"""
    if not g.user.is_admin:
        return jsonify({"error": "取り込み元フォルダの変更は管理者のみです。"}), 403
    body = request.json or {}
    try:
        if body.get("action") == "remove":
            importer.remove_dir(body.get("path", ""))
        else:
            importer.add_dir(body.get("path", ""))
    except importer.ImportError_ as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "dirs": importer.dir_status()})


@bp.post("/api/import/browse")
@admin_required
def browse():
    """フォルダを1階層ぶん開く（エクスプローラ風の選択画面用）。"""
    try:
        return jsonify(importer.browse((request.json or {}).get("path") or None))
    except importer.ImportError_ as e:
        return jsonify({"error": str(e)}), 400


# =============================================================================
# プレビューと取り込み
# =============================================================================

def _read_source(body: dict, nrows=None):
    """サーバのフォルダ / アップロード のどちらからでも DataFrame を返す。"""
    delim = importer.DELIMITERS.get(body.get("delimiter") or "自動判定")
    header = int(body.get("header_row") or 0)
    sheet = body.get("sheet") or None
    token = body.get("upload")
    if token:
        item = filestore.get(token, g.user.username)
        if item is None:
            raise importer.ImportError_(
                "アップロードしたファイルが見つかりません。もう一度選び直してください。")
        return importer.read_upload(item["data"], item["filename"], sheet=sheet,
                                    header_row=header, delimiter=delim, nrows=nrows)
    return importer.read_table(Path(body.get("path", "")), sheet=sheet, header_row=header,
                               delimiter=delim, nrows=nrows)


@bp.post("/api/import/upload")
@admin_required
def upload():
    """手元のPCから選んだファイルを受け取る。ディスクには書かず、メモリに預かる。"""
    f = request.files.get("file")
    if f is None:
        return jsonify({"error": "ファイルが選ばれていません。"}), 400
    data = f.read()
    try:
        importer.check_upload(data, f.filename or "")
    except importer.ImportError_ as e:
        return jsonify({"error": str(e)}), 400
    token = filestore.put(data, f.filename or "upload", "application/octet-stream",
                          g.user.username)
    try:
        sheets = importer.upload_sheet_names(data, f.filename or "")
    except importer.ImportError_ as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "upload": token, "name": f.filename,
                    "size": len(data), "sheets": sheets})


@bp.post("/api/import/preview")
@admin_required
def preview():
    body = request.json or {}
    token = body.get("upload")
    try:
        if token:
            item = filestore.get(token, g.user.username)
            if item is None:
                raise importer.ImportError_(
                    "アップロードしたファイルが見つかりません。もう一度選び直してください。")
            stem = Path(item["filename"]).stem
            sheets = importer.upload_sheet_names(item["data"], item["filename"])
        else:
            path = Path(body.get("path", ""))
            stem = path.stem
            sheets = importer.sheet_names(path)
        df = _read_source(body, nrows=2000)
    except importer.ImportError_ as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"読み込みに失敗しました: {e}"}), 400

    plan = importer.plan_columns(df)
    head = df.head(config.IMPORT_PREVIEW_ROWS)
    return jsonify({
        "ok": True, "sheets": sheets,
        "columns": [str(c) for c in df.columns],
        "rows": jsonable(head.values.tolist()),
        "scanned": len(df),
        "plan": [{**p, "include": True} for p in plan],
        "suggest_table": importer.safe_name(stem),
        "suggest_db": stem,
    })


def _log_manual(db_path, body: dict, mode: str, ok: bool, message: str,
                started, **kw) -> None:
    """画面からの1回きりの取り込みを履歴に残す（成功も失敗も）。"""
    upload = body.get("upload")
    source = "（自分のPCからアップロード）" if upload else body.get("path", "")
    history.add(db_path.name if db_path else (body.get("db_file") or ""),
                importer.safe_name(body.get("table", "")), ok, message,
                kind="manual", mode=mode, source=source,
                sheet=body.get("sheet") or None,
                user=getattr(g.user, "username", None), started=started, **kw)


@bp.post("/api/import/run")
@admin_required
def run():
    body = request.json or {}
    cols = [{"元の列名": c["source"], "列名": importer.safe_name(c["name"], c["source"]),
             "型": c["type"]} for c in (body.get("columns") or []) if c.get("include")]
    if not cols:
        return jsonify({"error": "取り込む列が選ばれていません。"}), 400

    mode = body.get("mode") or "replace"
    ts_col = (body.get("timestamp_column") or "").strip() or None
    keep = body.get("keep_runs")
    # 1回きりの取り込みでも、定期取り込みと同じ条件を課す。
    # 後から定期化したときに「取得日時が無い古い行」が残らないようにするため。
    errors = jobs.validate({"db_file": "x", "table": "x", "source": "x", "mode": mode,
                            "timestamp_column": ts_col, "keep_runs": keep})
    if errors:
        return jsonify({"error": " / ".join(errors)}), 400
    if mode == "append":
        keep = int(keep)

    # 定期実行＋追記のテーブルは、手で足すと取得日時が1回ぶん余計に増えて
    # 更新間隔が崩れる。定期取り込みの「▶ 今すぐ更新」と同じ理由で止める。
    target_db = (body.get("db_file") or "") if not body.get("new_db") else ""
    locked = _locked_tables().get(target_db, {}).get(importer.safe_name(body.get("table", "")))
    if locked:
        return jsonify({"error": locked}), 400

    started = datetime.now()
    db_path = None
    try:
        db_path = (importer.db_path_for(body["db_name"]) if body.get("new_db")
                   else config.DATA_DIR / body["db_file"])
        full = _read_source(body)
        n, degraded = importer.import_dataframe(
            db_path, body["table"], full, cols, mode=mode, timestamp_col=ts_col)
        removed = (importer.prune_runs(db_path, body["table"], ts_col, keep)
                   if mode == "append" else 0)
        kept = importer.run_count(db_path, body["table"], ts_col)
    except importer.ImportError_ as e:
        _log_manual(db_path, body, mode, False, str(e), started, keep=keep)
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        _log_manual(db_path, body, mode, False, f"取り込みに失敗しました: {e}",
                    started, keep=keep)
        return jsonify({"error": f"取り込みに失敗しました: {e}"}), 500

    message = f"{n:,}行を{'追記' if mode == 'append' else '洗い替え'}しました。"
    if mode == "append":
        message += f" 保持 {kept}/{keep}回"
        if removed:
            message += f"（古い {removed:,}行を削除）"
    _log_manual(db_path, body, mode, True, message, started,
                rows=n, removed=removed, kept=kept, keep=keep)

    catalog.profile_db(db_path, force=True)
    return jsonify({"ok": True, "rows": n, "degraded": degraded, "removed": removed,
                    "kept": kept, "keep": keep if mode == "append" else None,
                    "timestamp_column": importer.safe_name(ts_col, "取得日時"),
                    "db": db_path.name, "table": importer.safe_name(body["table"])})


@bp.get("/api/import/manage")
@admin_required
def manage_view():
    return jsonify(_manage_view())


@bp.get("/api/import/table")
@admin_required
def table_detail():
    """テーブルを開いたときに読む中身（サンプル行と更新履歴）。

    一覧を出すたびに全テーブルを走査すると重いので、開いたものだけ取りに来る。
    """
    db_file = request.args.get("db", "")
    table = request.args.get("table", "")
    path = config.DATA_DIR / db_file
    if path.parent.resolve() != config.DATA_DIR.resolve() or not path.exists():
        return jsonify({"error": "DBが見つかりません。"}), 404
    ts = next((j.get("timestamp_column") for j in jobs.list_jobs()
               if j.get("db_file") == db_file and j.get("table") == table), None)
    return jsonify({
        "sample": jsonable(importer.sample_rows(path, table, timestamp_col=ts)),
        "history": history.for_table(db_file, table, limit=50),
        "kinds": history.KINDS,
    })


@bp.get("/api/import/impact")
@admin_required
def impact():
    """消す前の下見。何が巻き添えになるかを返す（何も書き換えない）。

    table を付ければテーブル1つ、無ければDB丸ごとの分。
    """
    try:
        path = db.path_for(request.args.get("db") or "")
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    table = request.args.get("table") or ""
    found = (cleanup.table_impact(path, table) if table
             else cleanup.db_impact(path))
    return jsonify({"db": path.name, "table": table,
                    "groups": cleanup.summarize(found)})


@bp.post("/api/import/drop-table")
@admin_required
def drop_table():
    """テーブルを消して、カタログに残る参照も一緒に片づける。

    掃除をしないと、存在しないテーブルの説明がAIに渡り続け、
    例文の検証は no such table で落ちる。
    """
    body = request.json or {}
    try:
        path = db.path_for(body.get("db") or "")
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    table = str(body.get("table") or "").strip()
    if not table:
        return jsonify({"error": "テーブル名がありません。"}), 400
    importer.drop_table(path, table)
    done = cleanup.clean_table(path, table,
                              drop_jobs=body.get("drop_jobs", True) is not False)
    print(f"[import] {path.name} の {table} を削除しました（{g.user.username}）")
    return jsonify({"ok": True, "groups": cleanup.summarize(done)})


@bp.post("/api/import/delete-db")
@admin_required
def delete_db():
    """DBをファイルごと消す。元には戻せないので、
    ファイル名をそのまま入力してもらったときだけ実行する。
    """
    body = request.json or {}
    try:
        path = db.path_for(body.get("db") or "")
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    if str(body.get("confirm") or "").strip() != path.name:
        return jsonify({"error": f"確認のため、DBのファイル名「{path.name}」を"
                                 "そのまま入力してください。"}), 400
    try:
        done = cleanup.delete_db(path, drop_jobs=body.get("drop_jobs", True) is not False)
    except (ValueError, OSError) as e:
        return jsonify({"error": str(e)}), 400
    print(f"[import] {path.name} を削除しました（{g.user.username}）")
    return jsonify({"ok": True, "groups": cleanup.summarize(done)})


# =============================================================================
# 定期取り込み
# =============================================================================

@bp.post("/api/jobs/save")
@admin_required
def job_save():
    body = request.json or {}
    if body.get("upload") or not body.get("path"):
        return jsonify({"error": "アップロードしたファイルは定期取り込みに登録できません"
                                 "（サーバ上に置かれていないため、次回以降読み直せません）。"
                                 "取り込み元フォルダに置いたファイルを選んでください。"}), 400
    cols = [{"元の列名": c["source"], "列名": importer.safe_name(c["name"], c["source"]),
             "型": c["type"]} for c in (body.get("columns") or []) if c.get("include")]
    # 「＋ 新しいDBを作る」のままでも登録できるようにする。
    # ファイルは最初の実行時に作られるので、ここでは名前だけ決めておけばよい。
    db_file = body.get("db_file")
    if not db_file and body.get("db_name"):
        try:
            db_file = importer.db_path_for(body["db_name"]).name
        except importer.ImportError_ as e:
            return jsonify({"error": str(e)}), 400
    draft = {
        "id": body.get("id"),
        "name": (body.get("name") or "").strip() or Path(body.get("path", "")).stem,
        "source": body.get("path"), "sheet": body.get("sheet") or None,
        "header_row": int(body.get("header_row") or 0),
        "delimiter": importer.DELIMITERS.get(body.get("delimiter") or "自動判定"),
        "db_file": db_file, "table": importer.safe_name(body.get("table", "")),
        "mode": body.get("mode") or "replace",
        "timestamp_column": (body.get("timestamp_column") or "").strip() or None,
        # 取得日時は洗い替えでも付ける（いつ時点のデータかを残すため）
        "keep_runs": body.get("keep_runs"),
        "start_at": (body.get("start_at") or "").strip(),
        "columns": cols,
        "interval_minutes": jobs.INTERVALS.get(body.get("interval") or "手動のみ", 0),
        "enabled": True,
    }
    errors = jobs.validate(draft)
    if errors:
        return jsonify({"error": " / ".join(errors)}), 400
    # 同じ取り込み元→同じテーブルは1つだけ。2つあると同時刻に2回追記されて全行が二重になる
    dup = jobs.find_duplicate(draft)
    if dup:
        return jsonify({"error":
                        f"この取り込み元と保存先の定期取り込み「{dup.get('name')}」はすでに登録されています"
                        f"（{jobs.interval_label(dup.get('interval_minutes') or 0)}）。"
                        "頻度や停止はデータカタログの各テーブルの「管理」で変更できます。"}), 400
    if draft["mode"] == "append":
        draft["keep_runs"] = int(draft["keep_runs"])
    return jsonify({"ok": True, "job": _job_row(jobs.save_job(draft))})


@bp.post("/api/jobs/run")
@admin_required
def job_run():
    body = request.json or {}
    # 画面から押した実行は、裏のスケジューラと区別できるように印を付けて履歴に残す
    who = getattr(g.user, "username", None)
    job = jobs.get_job(body.get("id", ""))
    if job is None:
        return jsonify({"error": "ジョブが見つかりません。"}), 404
    blocked = jobs.manual_run_blocked(job)
    if blocked:
        return jsonify({"error": blocked}), 400
    results = [(job, jobs.run_job(job, kind="job", user=who))]
    for j, r in results:
        if r["ok"]:
            catalog.profile_db(config.DATA_DIR / j["db_file"], force=True)
    return jsonify({"ok": True,
                    "results": [{"name": j.get("name"), **r} for j, r in results],
                    "jobs": [_job_row(x) for x in jobs.list_jobs()]})


@bp.post("/api/jobs/update")
@admin_required
def job_update():
    body = request.json or {}
    job = jobs.get_job(body.get("id", ""))
    if job is None:
        return jsonify({"error": "ジョブが見つかりません。"}), 404
    if "enabled" in body:
        job["enabled"] = bool(body["enabled"])
    if body.get("interval"):
        job["interval_minutes"] = jobs.INTERVALS.get(body["interval"], 0)
    # 開始日時は触らないので過去チェックはしない（登録時に済んでいる）
    errors = jobs.validate(job, check_start=False)
    if errors:
        return jsonify({"error": " / ".join(errors)}), 400
    jobs.save_job(job)
    return jsonify({"ok": True, "jobs": [_job_row(x) for x in jobs.list_jobs()]})


@bp.post("/api/jobs/delete")
@admin_required
def job_delete():
    jobs.delete_job((request.json or {}).get("id", ""))
    return jsonify({"ok": True, "jobs": [_job_row(x) for x in jobs.list_jobs()]})


