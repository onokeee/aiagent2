"""ユーザーごとのチャット履歴。

  data/users/<ユーザー>/chats/index.json … 一覧（タイトルと日時だけ）
  data/users/<ユーザー>/chats/<ID>.json  … 会話の中身

会話1件に保存するのは次の2つ。

  messages    … LLMに送るメッセージ列。これが無いと「続きから」会話できない
  render_log  … 画面に描くアイテム（テキスト・SQL・表・グラフ・作成ファイル）

一覧を別ファイルにしているのは、サイドバーを描くたびに全会話を読み込まないため。
index.json が壊れた/消えた場合は、置いてある会話ファイルから作り直す。

古い会話は2つの条件で自動的に消える。
  本数   … CHAT_HISTORY_LIMIT を超えたぶん（古い順）
  保存期間 … 最後に使った日から CHAT_HISTORY_DAYS を過ぎたもの（既定90日）
掃除は一覧を読むついでに行う。常駐の掃除役を置かずに済ませるため。

Excel等の作成ファイルはバイト列なのでJSONに入らない。上限までは base64 で埋め込み、
超えるものは本体を捨てる（過去の会話を開いても再ダウンロードはできない）。
"""
from __future__ import annotations

import base64
import json
import re
import uuid
from datetime import datetime
from pathlib import Path

import config

_INDEX_NAME = "index.json"
_ID_RE = re.compile(r"[^0-9a-zA-Z_-]")
_TITLE_MAX = 40


# --- 場所 ---------------------------------------------------------------------

def chats_dir(user) -> Path:
    key = getattr(user, "safe_key", None) or str(user)
    return config.USER_META_DIR / key / "chats"


def _safe_id(chat_id: str) -> str:
    """ファイル名に使う前に無害化する（.. や / を混ぜられないように）。"""
    return _ID_RE.sub("", str(chat_id))[:64]


def _chat_file(user, chat_id: str) -> Path:
    return chats_dir(user) / f"{_safe_id(chat_id)}.json"


def new_id() -> str:
    # 先頭に日時を置いて、ファイル名を見ただけで新しい順に並ぶようにする
    return datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# --- ファイル入出力 -------------------------------------------------------------

def _read_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[chats] 読めませんでした: {p} ({e})")
        return None


def _write_json(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")


# --- 一覧 ---------------------------------------------------------------------

def _index_path(user) -> Path:
    return chats_dir(user) / _INDEX_NAME


def _rebuild_index(user) -> list[dict]:
    """会話ファイルから一覧を作り直す（index.json を失った場合の保険）。"""
    items = []
    for p in chats_dir(user).glob("*.json"):
        if p.name == _INDEX_NAME:
            continue
        data = _read_json(p)
        if not isinstance(data, dict) or not data.get("id"):
            continue
        items.append(_summary(data))
    items.sort(key=lambda c: c.get("updated_at") or "", reverse=True)
    items = _drop_expired(user, items)
    if items:
        _write_json(_index_path(user), {"chats": items})
    return items


def _expired(summary: dict) -> bool:
    """保存期間を過ぎた会話か。

    数えるのは「最後に使った日」から。開いて続きを話した会話は寿命が延びる。
    日付が読めないものは、消して困る方が大きいので残す。
    """
    if config.CHAT_HISTORY_DAYS <= 0:
        return False
    stamp = summary.get("updated_at") or summary.get("created_at") or ""
    try:
        used = datetime.fromisoformat(str(stamp))
    except ValueError:
        return False
    return (datetime.now() - used).days > config.CHAT_HISTORY_DAYS


def _drop_expired(user, items: list[dict]) -> list[dict]:
    """期限切れの会話を実体ごと消して、残ったぶんを返す。"""
    keep, gone = [], 0
    for c in items:
        if _expired(c):
            f = _chat_file(user, c.get("id", ""))
            if f.exists():
                f.unlink()
            gone += 1
        else:
            keep.append(c)
    if gone:
        print(f"[chats] 保存期間({config.CHAT_HISTORY_DAYS}日)を過ぎた会話を"
              f"{gone}件削除しました（{_key_label(user)}）")
    return keep


def _key_label(user) -> str:
    return getattr(user, "username", None) or str(user)


def list_chats(user) -> list[dict]:
    """会話の一覧（新しい順）。中身は読まない。"""
    if user is None or not chats_dir(user).exists():
        return []
    data = _read_json(_index_path(user)) if _index_path(user).exists() else None
    items = data.get("chats") if isinstance(data, dict) else data
    if not isinstance(items, list):
        items = None
    if not items:
        return _rebuild_index(user)
    # 実体が消えているものは一覧からも落とす
    items = [c for c in items
             if isinstance(c, dict) and c.get("id") and _chat_file(user, c["id"]).exists()]
    items.sort(key=lambda c: c.get("updated_at") or "", reverse=True)
    # 期限切れは、一覧を出すついでに片付ける（掃除専用の仕組みを持たない）
    kept = _drop_expired(user, items)
    if len(kept) != len(items):
        _save_index(user, kept)
    return kept


def _summary(chat: dict) -> dict:
    return {
        "id": chat.get("id"),
        "title": chat.get("title") or "（無題）",
        "created_at": chat.get("created_at") or "",
        "updated_at": chat.get("updated_at") or "",
        "db_names": list(chat.get("db_names") or []),
        "n_turns": sum(1 for m in (chat.get("messages") or []) if m.get("role") == "user"),
    }


def _save_index(user, items: list[dict]) -> None:
    _write_json(_index_path(user), {"chats": items})


def _upsert_index(user, summary: dict) -> list[dict]:
    items = [c for c in list_chats(user) if c.get("id") != summary["id"]]
    items.insert(0, summary)
    items.sort(key=lambda c: c.get("updated_at") or "", reverse=True)

    # 上限を超えた古い会話は実体ごと削除
    for old in items[config.CHAT_HISTORY_LIMIT:]:
        f = _chat_file(user, old.get("id", ""))
        if f.exists():
            f.unlink()
    items = items[:config.CHAT_HISTORY_LIMIT]
    _save_index(user, items)
    return items


# --- 作成ファイル(bytes)の出し入れ ------------------------------------------------

def _encode_item(item: dict) -> dict:
    out = dict(item)
    data = out.get("data")
    if isinstance(data, (bytes, bytearray)):
        if len(data) <= config.CHAT_EMBED_FILE_MAX_BYTES:
            out["data"] = base64.b64encode(bytes(data)).decode("ascii")
            out["_b64"] = True
        else:                              # 大きすぎるので中身は保存しない
            out.pop("data", None)
            out["_no_data"] = True
    return out


def _decode_item(item: dict) -> dict:
    out = dict(item)
    if out.pop("_b64", False):
        try:
            out["data"] = base64.b64decode(out.get("data") or "")
        except Exception:
            out.pop("data", None)
            out["_no_data"] = True
    return out


# --- 読み書き -------------------------------------------------------------------

def make_title(messages: list[dict]) -> str:
    """最初のユーザー発言をタイトルにする。"""
    for m in messages or []:
        if m.get("role") != "user" or not m.get("content"):
            continue
        content = m["content"]
        if isinstance(content, list):
            # 画像つきの発言は content が配列。文字の部分だけ拾う。
            content = "".join(p.get("text", "") for p in content
                              if isinstance(p, dict) and p.get("type") == "text")
        t = " ".join(str(content).split())
        if t:
            return t[:_TITLE_MAX] + ("…" if len(t) > _TITLE_MAX else "")
    return "（無題）"


def save_chat(user, chat_id: str, messages: list[dict], render_log: list[dict],
              db_names=None, tables=None, title: str = "", created_at: str = "") -> dict:
    """会話を保存し、一覧用の要約を返す。"""
    chat = {
        "id": chat_id,
        "title": title or make_title(messages),
        "created_at": created_at or _now(),
        "updated_at": _now(),
        "db_names": list(db_names or []),
        "tables": dict(tables or {}),
        # system prompt は開くたびに作り直すので保存しない（カタログ変更に追従させる）
        "messages": [m for m in (messages or []) if m.get("role") != "system"],
        "render_log": [_encode_item(i) for i in (render_log or [])],
    }
    _write_json(_chat_file(user, chat_id), chat)
    summary = _summary(chat)
    _upsert_index(user, summary)
    return summary


def load_chat(user, chat_id: str) -> dict | None:
    p = _chat_file(user, chat_id)
    if not p.exists():
        return None
    data = _read_json(p)
    if not isinstance(data, dict):
        return None
    data["render_log"] = [_decode_item(i) for i in (data.get("render_log") or [])]
    data.setdefault("messages", [])
    return data


def rename_chat(user, chat_id: str, title: str) -> bool:
    chat = load_chat(user, chat_id)
    if chat is None:
        return False
    chat["title"] = (title or "").strip()[:_TITLE_MAX] or make_title(chat.get("messages"))
    chat["render_log"] = [_encode_item(i) for i in (chat.get("render_log") or [])]
    _write_json(_chat_file(user, chat_id), chat)
    _upsert_index(user, _summary(chat))
    return True


def delete_chat(user, chat_id: str) -> bool:
    p = _chat_file(user, chat_id)
    existed = p.exists()
    if existed:
        p.unlink()
    _save_index(user, [c for c in list_chats(user) if c.get("id") != chat_id])
    return existed


def label(summary: dict) -> str:
    """サイドバーのプルダウンに出す1行。"""
    stamp = (summary.get("updated_at") or "")[5:16].replace("T", " ")   # MM-DD HH:MM
    title = summary.get("title") or "（無題）"
    return f"{title}　（{stamp}）" if stamp else title
