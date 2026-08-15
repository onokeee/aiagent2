"""用語集・例文の変更履歴。誰が・いつ・何を・どう変えたかを残す。

カタログは全員共通の土台で、チャットからは一般ユーザーも書けるようにした。
書けるようにした以上、「いつの間にか定義が変わっていた」が起きるので、
変更のたびに1件を追記して、後から辿れるようにする。

置き場所は data/catalog_history.jsonl（1行1件のJSON・追記型）。
import_history と同じ考え方で、YAMLに混ぜない（メタ情報は「現在の定義」だけを
持ち、履歴で膨らませない。normalize が知らないキーを消す作りとも衝突しない）。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

import config

_lock = threading.Lock()

#: 表示用のラベル
KINDS = {"glossary": "用語", "example": "例文"}
OPS = {"add": "新規", "update": "変更", "remove": "削除"}


def _path() -> Path:
    return config.CATALOG_HISTORY_FILE


def add(kind: str, op: str, db_file: str, name: str, *,
        user: str | None = None, table: str | None = None,
        before=None, after=None, source: str = "chat") -> None:
    """1件追記する。失敗しても本体の保存は止めない（履歴は本体より弱い）。"""
    rec = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "kind": kind, "op": op, "db": db_file, "table": table or "",
        "name": name, "user": user or "不明", "source": source,
        "before": before, "after": after,
    }
    try:
        with _lock:
            p = _path()
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            _trim(p)
    except Exception as e:
        print(f"[catalog_history] 書けませんでした: {e}")


def _trim(p: Path) -> None:
    """上限を超えたら古い行から捨てる（毎回数えず、たまに間引く）。"""
    lines = p.read_text(encoding="utf-8").splitlines()
    if len(lines) > config.CATALOG_HISTORY_MAX * 1.2:
        keep = lines[-config.CATALOG_HISTORY_MAX:]
        p.write_text("\n".join(keep) + "\n", encoding="utf-8")


def recent(limit: int = 50) -> list[dict]:
    """新しい順に。カタログ画面の「変更履歴」に出す。"""
    p = _path()
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    out.reverse()
    return out[:limit] if limit else out


def summarize(rec: dict) -> str:
    """1件を画面向けの短い日本語に。"""
    where = f"{rec.get('db', '')}" + (f" の {rec['table']}" if rec.get("table") else "")
    label = f"{KINDS.get(rec.get('kind'), rec.get('kind'))}「{rec.get('name', '')}」"
    op = OPS.get(rec.get("op"), rec.get("op"))
    return f"{where}: {label} を{op}"
