"""アプリ内スケジューラ。cron や常駐サービスを別に用意せず、Pythonだけで定期実行する。

アプリ起動時にデーモンスレッドを1本立て、一定間隔で「期限が来たジョブ」を実行する。
Streamlit のスクリプトは操作のたびに再実行されるが、スレッドはプロセスに1本だけ。
画面を誰も開いていなくても、アプリのプロセスが生きていれば動く。

  app.py ──start()──▶ [aiagent-import-scheduler スレッド]
                          └─ 60秒ごと: jobs.due_jobs() → jobs.run_job()

このスレッドから Streamlit の API（st.*）は呼ばない。
画面の描画コンテキストが無いので、状態は _state に置いて画面側から読む。
"""
from __future__ import annotations

import atexit
import threading
import traceback
from datetime import datetime

import config
import jobs

# 名前でスレッドの生存を確認する。モジュールが再読込されてもフラグに頼らず
# 二重起動を防げる（開発中にファイルを保存するとStreamlitが読み直すため）。
_THREAD_NAME = "aiagent-import-scheduler"

# 終了時に眠っているスレッドを起こして片付けるための合図。
# sleep() で寝かせたままプロセスを終わらせると、後始末中に標準出力を掴んだままになり
# 「_enter_buffered_busy ... daemon threads」で異常終了することがある。
_stop = threading.Event()

_state: dict = {
    "started_at": None,
    "last_tick": None,
    "tick_count": 0,
    "last_ran": [],        # 直近に実行したジョブ [{name, ok, message, at}]
    "last_error": None,
}


def is_running() -> bool:
    return any(t.name == _THREAD_NAME and t.is_alive() for t in threading.enumerate())


def status() -> dict:
    return {**_state, "running": is_running(),
            "tick_sec": config.IMPORT_SCHEDULER_TICK_SEC,
            "enabled": config.IMPORT_SCHEDULER}


def _log(msg: str) -> None:
    if _stop.is_set():                 # 終了処理中は標準出力に触らない
        return
    try:
        print(f"[scheduler {datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)
    except (ValueError, OSError):      # 出力先が既に閉じられている
        pass


def tick() -> list:
    """期限が来たジョブを実行する（スレッドの1周分。テストからも呼べる）。"""
    ran = []
    for job in jobs.due_jobs():
        res = jobs.run_job(job)
        ran.append({"name": job.get("name") or job.get("id"), "ok": res["ok"],
                    "message": res["message"],
                    "at": datetime.now().isoformat(timespec="seconds")})
        _log(("OK  " if res["ok"] else "NG  ") + f"{job.get('name')}: {res['message']}")
    _state["last_tick"] = datetime.now().isoformat(timespec="seconds")
    _state["tick_count"] += 1
    if ran:
        _state["last_ran"] = ran[-10:]
    return ran


def _loop() -> None:
    _log(f"開始（{config.IMPORT_SCHEDULER_TICK_SEC}秒ごとに確認）")
    while not _stop.is_set():
        try:
            tick()
            _state["last_error"] = None
        except Exception as e:
            # 1周こけても止めない。止まると以後ずっと更新されなくなるため。
            _state["last_error"] = f"{e}"
            _log("巡回でエラー: " + traceback.format_exc(limit=3).replace("\n", " "))
        # sleep ではなく wait。停止の合図が来たら即座に抜ける。
        _stop.wait(max(5, config.IMPORT_SCHEDULER_TICK_SEC))


def stop(timeout: float = 2.0) -> None:
    """スレッドを止める（プロセス終了時に自動で呼ばれる）。"""
    _stop.set()
    for t in threading.enumerate():
        if t.name == _THREAD_NAME and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=timeout)


def start() -> bool:
    """スケジューラを起動する。何度呼んでも1本しか立たない。"""
    if not config.IMPORT_SCHEDULER:
        return False
    if is_running():
        return False
    _stop.clear()
    t = threading.Thread(target=_loop, name=_THREAD_NAME, daemon=True)
    t.start()
    atexit.register(stop)
    _state["started_at"] = datetime.now().isoformat(timespec="seconds")
    return True
