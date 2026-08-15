"""ツールが取ったデータを短いあいだ覚えておく置き場。

同じSQLを何度も流し直さないための仕組み。「集計 → グラフ → レポート」と
進むとき、以前は各ツールが自分でSQLを実行していたので、1つの問いに対して
同じSQLが3回走っていた。往復の上限（config.MAX_AGENT_STEPS）も、そのぶん
無駄に消える。結果に名前（result_id）を付けて返し、後続のツールはSQLの
代わりにその名前を指せるようにする。

副産物として、表とグラフが必ず同じデータを見ることになる。
実行し直す方式では、その間にデータが入れ替わると数字がずれ得た。

置き方の約束:
  * プロセス内の辞書に持つ。ワーカーは1つで運用する前提（run.py参照）。
  * 古いものから捨てる。件数と総セル数の両方に上限を設ける。
  * 取り出すときは、預けたときと同じDBの組み合わせかを確かめる。
    IDを当てずっぽうで指されても、選んでいないDBの中身は出さない。
"""
from __future__ import annotations

import uuid
from collections import OrderedDict

#: 覚えておく結果の数。会話1本で使う量に対して十分な余裕を見た数。
MAX_ENTRIES = 40
#: 総セル数の上限（行×列の合計）。これを超えたら古いものから捨てる。
MAX_CELLS = 400_000

_store: "OrderedDict[str, dict]" = OrderedDict()


def scope_key(scope: list[dict]) -> str:
    """どのDBの組み合わせで取ったデータかを表す文字列。"""
    return "|".join(sorted(str((s or {}).get("path") or "") for s in (scope or [])))


def _cells(entry: dict) -> int:
    return len(entry["rows"]) * max(1, len(entry["columns"]))


def _evict() -> None:
    """上限を超えたぶんを、古い順に捨てる。"""
    while len(_store) > MAX_ENTRIES:
        _store.popitem(last=False)
    total = sum(_cells(e) for e in _store.values())
    while total > MAX_CELLS and len(_store) > 1:
        _, old = _store.popitem(last=False)
        total -= _cells(old)


def put(scope: list[dict], columns: list, rows: list, truncated: bool = False,
        sql: str | None = None, label: str | None = None) -> str:
    """結果を預けて result_id を返す。"""
    rid = "r_" + uuid.uuid4().hex[:8]
    _store[rid] = {
        "scope": scope_key(scope),
        "columns": list(columns),
        "rows": [tuple(r) for r in rows],
        "truncated": bool(truncated),
        "sql": sql,
        "label": label,
    }
    _evict()
    return rid


def get(scope: list[dict], rid: str) -> dict | None:
    """預けた結果を取り出す。無い・別のDBの組み合わせ、のときは None。"""
    entry = _store.get(str(rid or ""))
    if entry is None or entry["scope"] != scope_key(scope):
        return None
    _store.move_to_end(rid)          # 使ったものは新しい扱いにして残す
    return entry


def describe(rid: str) -> str:
    """LLMに返す一言。何のデータなのかを思い出せるようにする。"""
    entry = _store.get(str(rid or ""))
    if entry is None:
        return ""
    return entry.get("label") or (entry.get("sql") or "")[:80]


def clear() -> None:
    """テスト用。"""
    _store.clear()
