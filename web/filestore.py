"""生成ファイル（Excel/CSV/テキスト）の一時置き場。

ツールが作るのはバイト列なので、ブラウザに渡すには一度サーバ側に置いて
ダウンロードURLを発行する必要がある。ディスクには書かない（Streamlit版と同じ方針）。
"""
from __future__ import annotations

import secrets
import threading
from collections import OrderedDict

_MAX_ITEMS = 200          # 保持する本数。古いものから捨てる
_lock = threading.Lock()
_files: OrderedDict[str, dict] = OrderedDict()


def put(data: bytes, filename: str, mime: str, owner: str) -> str:
    token = secrets.token_urlsafe(16)
    with _lock:
        _files[token] = {"data": data, "filename": filename, "mime": mime, "owner": owner}
        while len(_files) > _MAX_ITEMS:
            _files.popitem(last=False)
    return token


def get(token: str, owner: str) -> dict | None:
    """本人が作ったファイルだけ返す（URLを推測されても他人のものは渡さない）。"""
    with _lock:
        item = _files.get(token)
    if item is None or item["owner"] != owner:
        return None
    return item
