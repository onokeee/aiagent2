"""定期取り込み（ジョブ）の定義と実行。

1ジョブ = 「どのファイルを / どう読んで / どのテーブルへ / どの方式で / どの間隔で」。
定義は data/import_jobs.yaml に置く（DBファイル自体が全ユーザー共通なのでジョブも共通）。

実行の入口は3つ。中身はすべて run_job() に集約してある。
  - 画面の「▶ 今すぐ更新」
  - 画面を開いたときの自動実行（config.IMPORT_AUTO_REFRESH が true のとき）
  - cron から refresh.py    ← 本番はこれが確実（誰も画面を開かなくても動く）
"""
from __future__ import annotations

import threading
import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import yaml

import config
import history
import importer

# 定義ファイルの書き換えとジョブ実行を直列化する。
# 裏で回るスケジューラと、画面からの「▶ 今すぐ更新」が同時に走りうるため。
_lock = threading.RLock()

# 画面に出す更新間隔。値は分。0 は「手動のみ」。
INTERVALS = {
    "手動のみ": 0,
    "15分ごと": 15,
    "1時間ごと": 60,
    "3時間ごと": 180,
    "6時間ごと": 360,
    "1日ごと": 1440,
    "1週間ごと": 10080,
}
MODES = {
    "replace": "洗い替え（毎回まるごと入れ替える）",
    "append": "追記（前回までのデータを残して足す）",
}

# 追記のとき「何回分の取り込みを残すか」。これを超えた古い回は消す。
# 上限を決めておかないと、日次で回すだけでもテーブルが際限なく膨らむ。
MAX_KEEP_RUNS = 800
# 既定値は置かない。何回分残すかは業務ごとに違うので、必ず自分で決めてもらう。
DEFAULT_KEEP_RUNS = None
# 開始日時の判定に使う許容。送信のタイムラグで「今」が過去扱いになるのを防ぐ。
START_GRACE_MINUTES = 2


def parse_dt(value) -> datetime | None:
    """画面から来る日時文字列（'2026-08-10T09:00' など）を datetime に。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def validate(job: dict, check_start: bool = True) -> list[str]:
    """保存前の点検。画面にそのまま出せる日本語で返す。

    check_start=False にすると開始日時が過去でも通す（登録済みジョブの
    間隔変更や停止/再開など、開始日時を触らない更新のため）。
    """
    errors = []
    if not (job.get("db_file") and job.get("table")):
        errors.append("取り込み先のDBとテーブルを指定してください。")
    if not job.get("source"):
        errors.append("取り込み元のファイルを選んでください。")

    raw_start = str(job.get("start_at") or "").strip()
    if raw_start:
        start = parse_dt(raw_start)
        if start is None:
            errors.append("開始日時の形式が正しくありません。")
        elif check_start and start < datetime.now() - timedelta(minutes=START_GRACE_MINUTES):
            errors.append(f"開始日時に過去の時刻は指定できません"
                          f"（指定: {start:%Y-%m-%d %H:%M}）。今より後の日時にしてください。")

    # 取得日時列は更新の仕方によらず必須。洗い替えでも「いつ時点のデータか」が
    # 分からないと、取り込み後の分析で断面を説明できない。
    if not str(job.get("timestamp_column") or "").strip():
        errors.append("取得日時の列名が必須です。")

    if job.get("mode") == "append":
        keep = job.get("keep_runs")
        if keep in (None, ""):
            errors.append("追記のときは保存回数が必須です。")
        else:
            try:
                keep = int(keep)
            except (TypeError, ValueError):
                errors.append("保存回数は数値で指定してください。")
            else:
                if not (1 <= keep <= MAX_KEEP_RUNS):
                    errors.append(f"保存回数は 1〜{MAX_KEEP_RUNS} の範囲で指定してください。")
    return errors


def manual_run_blocked(job: dict) -> str | None:
    """手で走らせてはいけない設定なら、その理由を返す（問題なければ None）。

    定期実行 × 追記 の組み合わせだけは止める。この2つが重なると、
      ・次回予定が「前回実行＋間隔」で決まるので、手で走らせた分だけ後ろにずれる
      ・保存回数を1回ぶん余計に使い、その回だけ間隔の違うデータが混ざる
    となって、せっかく決めた更新頻度が崩れる。
    洗い替えや「手動のみ」の設定は、何度走らせても頻度の意味が変わらないので通す。
    """
    if int(job.get("interval_minutes") or 0) <= 0:
        return None
    if (job.get("mode") or "replace") != "append":
        return None
    label = interval_label(job.get("interval_minutes", 0))
    return (f"「{job.get('name') or 'この設定'}」は定期実行（{label}）＋追記です。"
            "手動で動かすと次回の実行時刻がずれ、保存回数も1回ぶん余計に使うため、"
            "手動実行はできません。どうしても今すぐ入れたいときは、"
            "更新の頻度を「手動のみ」に変えてから実行してください。")


def interval_label(minutes: int) -> str:
    for k, v in INTERVALS.items():
        if v == int(minutes or 0):
            return k
    return f"{minutes}分ごと"


# =============================================================================
# 保存と読み出し
# =============================================================================

def _read() -> list[dict]:
    p = config.IMPORT_JOBS_FILE
    if not p.exists():
        return []
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[jobs] 読めませんでした: {p} ({e})")
        return []
    items = data.get("jobs") if isinstance(data, dict) else data
    return [j for j in (items or []) if isinstance(j, dict) and j.get("id")]


def _write(items: list[dict]) -> None:
    p = config.IMPORT_JOBS_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump({"jobs": items}, allow_unicode=True, sort_keys=False),
                 encoding="utf-8")


def list_jobs() -> list[dict]:
    return sorted(_read(), key=lambda j: (j.get("name") or ""))


def get_job(job_id: str) -> dict | None:
    return next((j for j in _read() if j.get("id") == job_id), None)


def _same_target(a: dict, b: dict) -> bool:
    """同じ取り込み元（ファイル＋シート）を同じDBの同じテーブルへ入れる設定か。"""
    def norm_path(p):
        return os.path.normcase(os.path.normpath(str(p or "").strip()))
    return (norm_path(a.get("source")) == norm_path(b.get("source"))
            and (a.get("sheet") or None) == (b.get("sheet") or None)
            and (a.get("db_file") or "") == (b.get("db_file") or "")
            and (a.get("table") or "") == (b.get("table") or ""))


def find_duplicate(job: dict) -> dict | None:
    """同じ取り込み元→同じテーブルの設定がすでにあれば、それを返す（自分自身は除く）。

    同じ設定が2つあると、同じ時刻に2回追記されて全行が二重になる
    （「保持N回」は取得日時で数えるので、同時刻の2バッチを1回分とみなして両方残す）。
    登録時に止めるためのもの。
    """
    for j in _read():
        if j.get("id") != job.get("id") and _same_target(j, job):
            return j
    return None


def save_job(job: dict) -> dict:
    with _lock:
        job = dict(job)
        # setdefault では駄目。呼び出し側が id=None を明示的に入れてくることがあり、
        # そのまま保存すると読み出し時に落とされて「保存したのに消える」ことになる。
        if not job.get("id"):
            job["id"] = uuid.uuid4().hex[:12]
        if not job.get("created_at"):
            job["created_at"] = datetime.now().isoformat(timespec="seconds")
        items = [j for j in _read() if j.get("id") != job["id"]]
        items.append(job)
        _write(items)
        return job


def delete_job(job_id: str) -> bool:
    with _lock:
        items = _read()
        left = [j for j in items if j.get("id") != job_id]
        if len(left) == len(items):
            return False
        _write(left)
        return True


# =============================================================================
# 実行タイミング
# =============================================================================

def next_run_at(job: dict) -> datetime | None:
    """次に動く予定の時刻。手動のみなら None。

    開始日時が設定されていれば、それより前には動かさない。
    """
    minutes = int(job.get("interval_minutes") or 0)
    if minutes <= 0:
        return None
    start = parse_dt(job.get("start_at"))
    last = parse_dt(job.get("last_run"))
    if last is None:
        # 一度も動いていない。開始日時があればその時刻、無ければすぐ対象。
        return start or datetime.now()
    nxt = last + timedelta(minutes=minutes)
    return max(nxt, start) if start else nxt


def is_due(job: dict, now: datetime | None = None) -> bool:
    if not job.get("enabled", True):
        return False
    nxt = next_run_at(job)
    return nxt is not None and nxt <= (now or datetime.now())


def due_jobs(now: datetime | None = None) -> list[dict]:
    return [j for j in list_jobs() if is_due(j, now)]


# =============================================================================
# 「設定どおりに更新できていない」ジョブ
#
# 失敗は履歴を見に行かないと分からず、日次の取り込みが月曜から失敗して金曜まで
# 誰も気づかない、が起こり得る。そこで「いま健全でないジョブ」を1か所で判定し、
#   ・チャットのサイドバー（DB名・テーブル名に警告マーク）
#   ・AIの回答（そのテーブルを使う質問に、データが古い可能性を添える）
#   ・管理者へのメール通知
# の3つが同じ判断を使う。
# =============================================================================

def problems() -> list[dict]:
    """設定どおりに更新できていない定期取り込み。

    3種類ある:
      failed   … 前回の実行が失敗した（ファイルが無い・シート名や列が変わった等）
      degraded … 取り込めたが、数値列に文字が混ざって文字として保存した
                 （合計・平均がずれる。元ファイルの値を直すべき）
      overdue  … 有効な自動実行なのに、予定の2周期ぶん以上動いていない
                 （スケジューラが止まっている・アプリが落ちていた等）
    戻り値: [{id, name, db_file, table, kind, since, message}, ...]
    """
    now = datetime.now()
    out = []
    for j in list_jobs():
        if not j.get("enabled", True):
            continue
        if j.get("last_status") == "error":
            out.append({"id": j.get("id"), "name": j.get("name"),
                        "db_file": j.get("db_file"), "table": j.get("table"),
                        "kind": "failed", "since": j.get("last_run") or "",
                        "message": j.get("last_message") or "前回の実行が失敗しました。"})
            continue
        if j.get("last_degraded"):
            cols = "、".join(str(c) for c in j["last_degraded"])
            out.append({"id": j.get("id"), "name": j.get("name"),
                        "db_file": j.get("db_file"), "table": j.get("table"),
                        "kind": "degraded", "since": j.get("last_run") or "",
                        "message": (f"前回の取り込みで、数値の列（{cols}）に数値でない値が混ざり、"
                                    "文字として保存しました。合計や平均がずれる可能性があります。"
                                    "元ファイルの値を確認してください。")})
            continue
        minutes = int(j.get("interval_minutes") or 0)
        last = parse_dt(j.get("last_run"))
        if minutes > 0 and last and (now - last) > timedelta(minutes=minutes * 2):
            out.append({"id": j.get("id"), "name": j.get("name"),
                        "db_file": j.get("db_file"), "table": j.get("table"),
                        "kind": "overdue", "since": j.get("last_run") or "",
                        "message": (f"{interval_label(minutes)}の予定ですが、"
                                    f"{last:%m/%d %H:%M} から更新されていません。"
                                    "自動実行が止まっている可能性があります。")})
    return out


def problems_by_table() -> dict:
    """{(db_file, table): [problem, ...]}。画面やAIの注記で引きやすい形。"""
    out: dict = {}
    for p in problems():
        out.setdefault((p["db_file"], p["table"]), []).append(p)
    return out


# =============================================================================
# 実行
# =============================================================================

def source_path(job: dict) -> Path:
    return Path(job.get("source", ""))


def run_job(job: dict, kind: str = "auto", user: str | None = None) -> dict:
    """1ジョブを実行して、結果を定義ファイルに書き戻す。

    kind は履歴に残す実行のきっかけ。"auto"=スケジューラ、"job"=画面の「▶ 今すぐ更新」。

    例外は投げず、結果を dict で返す（1本こけても他を止めないため）。
      {"ok": bool, "rows": int, "message": str, "degraded": [...]}
    """
    with _lock:                       # 同じテーブルへ同時に書かないように直列化する
        return _run_job_locked(job, kind, user)


def _run_job_locked(job: dict, kind: str = "auto", user: str | None = None) -> dict:
    started = datetime.now()
    result = {"ok": False, "rows": 0, "message": "", "degraded": []}
    removed = 0
    kept = None
    try:
        path = source_path(job)
        if not importer.is_allowed(path):
            raise importer.ImportError_(
                "取り込み元のファイルが見つかりません（移動・削除、または許可フォルダの設定変更）。")

        df = importer.read_table(
            path,
            sheet=job.get("sheet") or None,
            header_row=int(job.get("header_row") or 0),
            delimiter=job.get("delimiter") or None,
        )
        cols = [dict(c) for c in (job.get("columns") or [])]
        if not cols:
            cols = importer.plan_columns(df)
        missing = [c["元の列名"] for c in cols if c["元の列名"] not in df.columns]
        if missing:
            if len(missing) == len(cols):
                # 1列も合わない＝列名の変更ではなく、区切り文字か見出し行の位置が変わった
                found = "、".join(str(c) for c in list(df.columns)[:5])
                raise importer.ImportError_(
                    f"設定した列が1つも見つかりません（ファイル側の見出し: {found}"
                    f"{' …' if len(df.columns) > 5 else ''}）。"
                    "区切り文字・見出し行の位置・シートが変わった可能性があります。"
                    "取り込み画面で開き直して設定を作り直してください。")
            raise importer.ImportError_(
                f"ファイル側に無い列があります: {', '.join(missing)}。"
                "列構成が変わった可能性があります。設定を作り直してください。")

        db_path = config.DATA_DIR / job["db_file"]
        mode = job.get("mode") or "replace"
        ts_col = job.get("timestamp_column") or config.IMPORT_TIMESTAMP_COLUMN
        if len(df) == 0:
            # 見出しだけのファイル（上流の出力が失敗した等）で洗い替えると、
            # テーブルが空になり「成功」で終わる。前回の内容を残して止める。
            # 本当に0件にしたいときは、取り込み画面から手で洗い替える。
            raise importer.ImportError_(
                f"{path.name} にデータ行がありません（見出しだけ）。"
                f"{'テーブルを空にしないため、前回の内容を残しました。' if mode != 'append' else '追記する行が無いため何もしていません。'}"
                "本当に0件なら、取り込み画面から手で洗い替えてください。")
        n, degraded = importer.import_dataframe(
            db_path, job["table"], df, cols, mode=mode,
            timestamp_col=ts_col,
            timestamp_value=started.isoformat(timespec="seconds"),
        )
        message = f"{n:,}行を{'追記' if mode == 'append' else '洗い替え'}しました。"
        if degraded:
            # 数値列に文字が混ざった。取り込み自体は通るが集計がずれるので、黙って通さない
            message += (f" ⚠ 数値にできない値があったため文字として保存した列: {', '.join(degraded)}"
                        "（元ファイルの値を確認してください）")

        # 追記のときは、保存回数を超えた古い取り込み分を落とす
        if mode == "append" and ts_col and job.get("keep_runs"):
            removed = importer.prune_runs(db_path, job["table"], ts_col, job["keep_runs"])
            kept = importer.run_count(db_path, job["table"], ts_col)
            result["removed"], result["kept"] = removed, kept
            message += f" 保持 {kept}/{job['keep_runs']}回"
            if removed:
                message += f"（古い {removed:,}行を削除）"
        result.update(ok=True, rows=n, degraded=degraded, message=message)
    except importer.ImportError_ as e:
        result["message"] = str(e)
    except Exception as e:                        # 想定外でもジョブ一覧は壊さない
        result["message"] = f"想定外のエラー: {e}"

    saved = get_job(job.get("id", "")) or dict(job)
    saved.update({
        "last_run": started.isoformat(timespec="seconds"),
        "last_status": "ok" if result["ok"] else "error",
        "last_message": result["message"],
        "last_rows": result["rows"],
        "last_degraded": list(result.get("degraded") or []),
    })
    save_job(saved)
    history.add(job.get("db_file", ""), job.get("table", ""),
                result["ok"], result["message"], kind=kind,
                mode=job.get("mode") or "replace", rows=result["rows"],
                removed=removed, kept=kept, keep=job.get("keep_runs"),
                source=job.get("source", ""), sheet=job.get("sheet"),
                job_id=job.get("id"), job_name=job.get("name"),
                user=user, started=started)
    return result


def run_due(now: datetime | None = None, kind: str = "auto",
            user: str | None = None) -> list[tuple[dict, dict]]:
    """期限が来たジョブをまとめて実行する。戻り値は (ジョブ, 結果) の一覧。"""
    return [(j, run_job(j, kind, user)) for j in due_jobs(now)]
