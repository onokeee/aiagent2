"""取り込みの更新履歴。

ジョブ定義（import_jobs.yaml）が持っているのは直前1回ぶんの結果だけなので、
「先週の火曜は何行入ったのか」「いつから失敗し続けているのか」を追えない。
そこで、1回の取り込みにつき1件をここに追記していく。

置き場所は data/import_history.jsonl（1行1件のJSON）。
YAML ではなく追記型にしているのは、実行のたびに全件を書き直したくないため。
手動の取り込みも定期取り込みも同じ形で残し、kind で区別する。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

import config

_lock = threading.Lock()
# 追記のたびに全件を数え直さないよう、行数はプロセス内で覚えておく。
# 別プロセス（refresh.py など）が書くとずれるが、間引きは後追いで効けばよい。
_count: int | None = None

KINDS = {"manual": "手動", "auto": "定期", "job": "定期（手動実行）"}


def _path() -> Path:
    return config.IMPORT_HISTORY_FILE


def add(db_file: str, table: str, ok: bool, message: str, *,
        kind: str = "manual", mode: str = "replace", rows: int = 0,
        removed: int = 0, kept=None, keep=None, source: str = "",
        sheet: str | None = None, job_id: str | None = None,
        job_name: str | None = None, user: str | None = None,
        started: datetime | None = None) -> dict:
    """1回ぶんの結果を残す。記録に失敗しても取り込み自体は止めない。"""
    now = datetime.now()
    rec = {
        "at": (started or now).isoformat(timespec="seconds"),
        "db_file": db_file, "table": table,
        "ok": bool(ok), "kind": kind, "mode": mode,
        "rows": int(rows or 0), "removed": int(removed or 0),
        "kept": kept, "keep": keep,
        "source": str(source or ""), "sheet": sheet,
        "job_id": job_id, "job_name": job_name, "user": user,
        "message": message,
        "seconds": round((now - started).total_seconds(), 1) if started else None,
    }
    global _count
    try:
        with _lock:
            p = _path()
            p.parent.mkdir(parents=True, exist_ok=True)
            if _count is None:
                _count = _line_count(p)
            with p.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            _count += 1
            _trim_if_needed(p)
    except Exception as e:                       # 履歴が書けなくても取り込みは成功扱い
        print(f"[history] 記録できませんでした: {e}")
    return rec


def _line_count(p: Path) -> int:
    if not p.exists():
        return 0
    with p.open("rb") as f:
        return sum(1 for line in f if line.strip())


def _trim_if_needed(p: Path) -> None:
    """行数が上限を超えたら、新しい方から上限ぶんだけ残す。

    毎回書き直すと重いので、1割ぶん超えてからまとめて間引く。
    """
    global _count
    limit = max(1, config.IMPORT_HISTORY_MAX)
    if (_count or 0) <= limit * 1.1:
        return
    lines = [x for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    keep = lines[-limit:]
    p.write_text("\n".join(keep) + "\n", encoding="utf-8")
    _count = len(keep)


def _read_all() -> list[dict]:
    p = _path()
    if not p.exists():
        return []
    out = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue                          # 壊れた行は飛ばす
            if isinstance(rec, dict):
                out.append(rec)
    except Exception as e:
        print(f"[history] 読めませんでした: {p} ({e})")
    return out


def _newest_first(items: list[dict]) -> list[dict]:
    """新しい順に並べる。

    at は秒までしか持たないので、同じ秒の中は「後に書いた方が新しい」で決める。
    先に並びを逆にしてから安定ソートすると、同着がその順で残る。
    """
    items = list(reversed(items))
    items.sort(key=lambda r: r.get("at") or "", reverse=True)
    return items


def for_table(db_file: str, table: str, limit: int = 30) -> list[dict]:
    """あるテーブルの履歴を新しい順で。"""
    hit = _newest_first([r for r in _read_all()
                         if r.get("db_file") == db_file and r.get("table") == table])
    return hit[:limit] if limit else hit


def recent(limit: int = 100) -> list[dict]:
    """テーブルを問わず、新しい順に。取り込み全体の傾向を見るとき用。"""
    hit = _newest_first(_read_all())
    return hit[:limit] if limit else hit


def counts() -> dict[tuple, int]:
    """(DB, テーブル) ごとの件数。"""
    out: dict[tuple, int] = {}
    for r in _read_all():
        key = (r.get("db_file"), r.get("table"))
        out[key] = out.get(key, 0) + 1
    return out


def latest_by_source() -> dict[str, dict]:
    """取り込み元ファイルごとの、いちばん新しい記録。

    「このファイルはもう取り込んだのか」「いつ・どのテーブルに入ったのか」を
    ファイルの一覧と突き合わせるために使う。キーはファイルパス。
    """
    out: dict[str, dict] = {}
    for r in _newest_first(_read_all()):
        src = str(r.get("source") or "")
        if src and src not in out:
            out[src] = r
    return out
