"""ログインユーザーごとの画面の状態。

  data/users/<ユーザー>/prefs.yaml

覚えておくのは次の2つ。

  selection … 対象データの選択（{DBファイル名: [テーブル名, ...]}）
  model     … 使うモデル

セッション（Cookie）に置くとログアウトやブラウザを閉じたときに消えてしまう。
毎回選び直すのは手間なので、そのユーザーのフォルダにファイルとして残す。
カタログやチャット履歴と同じ場所に置くので、退職者のデータを消すときは
そのユーザーのフォルダごと消せばよい。
"""
from __future__ import annotations

import threading

import yaml

import config

_lock = threading.Lock()

# ここに挙げたキーだけを読み書きする（余計なものが混ざっても無視する）
KEYS = ("selection", "model")


def _key(user) -> str:
    """保存先のフォルダ名。catalog / chats と同じ決め方にする。"""
    return getattr(user, "safe_key", None) or str(user)


def _path(user):
    return config.USER_META_DIR / _key(user) / "prefs.yaml"


def load(user) -> dict:
    if user is None:
        return {}
    p = _path(user)
    if not p.exists():
        return {}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as e:
        print(f"[prefs] 読めませんでした: {p} ({e})")
        return {}
    return {k: v for k, v in data.items() if k in KEYS} if isinstance(data, dict) else {}


def _save(user, data: dict) -> None:
    p = _path(user)
    p.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        p.write_text(yaml.safe_dump({k: data[k] for k in KEYS if k in data},
                                    allow_unicode=True, sort_keys=False),
                     encoding="utf-8")


def set_value(user, key: str, value) -> None:
    """1項目だけ更新する。他の項目は触らない。"""
    if user is None or key not in KEYS:
        return
    data = load(user)
    data[key] = value
    _save(user, data)


# --- 対象データの選択 -------------------------------------------------------------

def get_selection(user) -> dict:
    sel = load(user).get("selection")
    if not isinstance(sel, dict):
        return {}
    # 保存後にDBやテーブルが消えている場合もあるが、そこは build_scope 側で弾かれる
    return {str(k): [str(t) for t in (v or [])] for k, v in sel.items()}


def set_selection(user, selection: dict) -> dict:
    sel = {str(k): [str(t) for t in (v or [])] for k, v in (selection or {}).items()}
    set_value(user, "selection", sel)
    return sel


# --- モデルの選択 ----------------------------------------------------------------

def get_model(user) -> str:
    return str(load(user).get("model") or "").strip()


def set_model(user, model: str) -> None:
    set_value(user, "model", str(model or "").strip())
