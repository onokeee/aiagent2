"""過去の分析で「実際に使われた」結合を数える。

ER図が描いているのは宣言された関連で、実際に通った道ではない。
チャット履歴には実行されたSQLが全部残っているので、そこからJOINを取り出して
数えると、次の3つが見えるようになる。

  よく通る道       … 太く描く。分析の主要動線
  誰も通らない道   … 灰色にする。検算されていない経路でもある
                     （実際、demo_sales で3,866万円の未請求が見つかったのは
                       一度も使われていなかった invoices への経路の上だった）
  登録の無い道     … AIが実際に結合しているのにカタログに無い。
                     「関連の候補」に実績つきで出す。登録すべきか、
                     AIが誤った結合をしているかのどちらかで、どちらでも知る価値がある

解析は正規表現＋カタログのプロファイル（テーブル・列の一覧）で行う。
SQLパーサは入れない。ここでの用途は「多い・少ない・ゼロ」が分かればよく、
多少の取りこぼしで結論が変わらないため。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import catalog
import config
import db

#: エイリアスとして解釈してはいけない語。
_RESERVED = {"on", "using", "where", "group", "order", "left", "right", "inner",
             "outer", "cross", "natural", "join", "as", "select", "from", "limit",
             "having", "union", "all", "and", "or", "not", "set", "by"}

_FROM_RE = re.compile(
    r'\b(?:from|join)\s+("?[\w一-龠ぁ-んァ-ヶ．.]+"?)(?:\s+(?:as\s+)?([A-Za-z_]\w*))?',
    re.IGNORECASE)
_USING_RE = re.compile(
    r'\bjoin\s+("?[\w一-龠ぁ-んァ-ヶ．.]+"?)(?:\s+(?:as\s+)?([A-Za-z_]\w*))?'
    r'\s+using\s*\(\s*"?(\w+)"?\s*\)', re.IGNORECASE)
_ON_RE = re.compile(
    r'\bon\s+("?[\w一-龠ぁ-んァ-ヶ．.]+"?)\s*=\s*("?[\w一-龠ぁ-んァ-ヶ．.]+"?)',
    re.IGNORECASE)


# =============================================================================
# 履歴からSQLを集める
# =============================================================================

def _walk_sql(node, acc: list) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "sql" and isinstance(v, str) and v.strip():
                acc.append(v)
            else:
                _walk_sql(v, acc)
    elif isinstance(node, list):
        for v in node:
            _walk_sql(v, acc)


def collect_sqls() -> tuple:
    """全ユーザーのチャット履歴から実行SQLを集める。

    同じSQLが画面用の写しとツール呼び出しの両方に残っているので、
    会話単位で重複を除く。戻り値: (SQLのリスト, 会話数)
    """
    sqls: list[str] = []
    users = Path(config.USER_META_DIR)
    chats = 0
    if not users.exists():
        return sqls, 0
    for f in users.glob("*/chats/*.json"):
        if f.name == "index.json":
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        chats += 1
        seen: set = set()
        for item in data.get("render_log") or []:
            if item.get("kind") == "sql" and item.get("sql"):
                seen.add(str(item["sql"]).strip())
        buf: list = []
        for m in data.get("messages") or []:
            for tc in (m.get("tool_calls") or []):
                try:
                    args = json.loads((tc.get("function") or {}).get("arguments") or "{}")
                except Exception:
                    continue
                _walk_sql(args, buf)
        seen |= {s.strip() for s in buf}
        sqls.extend(seen)
    return sqls, chats


# =============================================================================
# JOIN の解決
# =============================================================================

def _entries() -> list[dict]:
    """全DBのプロファイル（テーブル・列）。名前解決の台帳になる。"""
    out = []
    for p in db.list_db_files():
        try:
            out.append({"alias": db.alias_for(p), "path": p,
                        "profile": catalog.profile_db(p)})
        except Exception:
            continue
    return out


def _resolve_table(raw: str, entries: list[dict], hint_aliases: set):
    """'demo_sales.orders' や 'orders' を (DBエイリアス, テーブル) にする。"""
    name = raw.strip().strip('"')
    if "." in name:
        prefix, _, rest = name.partition(".")
        rest = rest.strip('"')
        for e in entries:
            if e["alias"].lower() == prefix.lower() and rest in e["profile"]["tables"]:
                return (e["alias"], rest)
        return None
    hits = [e["alias"] for e in entries if name in e["profile"]["tables"]]
    if len(hits) == 1:
        return (hits[0], name)
    if hits:
        # 同名テーブルが複数DBにある。同じSQLに出てきたDBを優先する
        for a in hits:
            if a in hint_aliases:
                return (a, name)
    return None


def _columns_of(entries: list[dict], alias: str, table: str) -> set:
    for e in entries:
        if e["alias"] == alias:
            t = e["profile"]["tables"].get(table) or {}
            return {c["name"] for c in t.get("columns", [])}
    return set()


def _edge_key(a: tuple, b: tuple) -> str:
    x, y = ".".join(a), ".".join(b)
    return f"{x}||{y}" if x <= y else f"{y}||{x}"


def joins_in(sql: str, entries: list[dict]) -> list[tuple]:
    """1本のSQLから、(端点, 端点) のリストを取り出す。端点 = (alias, table, column)。"""
    flat = " ".join(sql.split())

    # 出現順のテーブルと、エイリアス→テーブルの対応
    order: list[tuple] = []
    alias_map: dict = {}
    hint = {e["alias"] for e in entries
            if re.search(r'(?<![\w."])' + re.escape(e["alias"]) + r'\s*\.',
                         flat, re.IGNORECASE)}
    for m in _FROM_RE.finditer(flat):
        raw, al = m.group(1), (m.group(2) or "")
        resolved = _resolve_table(raw, entries, hint)
        if resolved is None:
            continue
        order.append(resolved)
        alias_map[resolved[1].lower()] = resolved          # テーブル名でも引ける
        if al and al.lower() not in _RESERVED:
            alias_map[al.lower()] = resolved

    out: list[tuple] = []

    # JOIN ... USING(col): 相手は「それより前に出た、同じ列を持つテーブル」
    for m in _USING_RE.finditer(flat):
        raw, col = m.group(1), m.group(3)
        right = _resolve_table(raw, entries, hint)
        if right is None:
            continue
        try:
            pos = order.index(right)
        except ValueError:
            continue
        left = next((t for t in reversed(order[:pos])
                     if col in _columns_of(entries, *t)), None)
        if left and col in _columns_of(entries, *right):
            out.append(((*left, col), (*right, col)))

    # JOIN ... ON a.x = b.y
    for m in _ON_RE.finditer(flat):
        ends = []
        for side in (m.group(1), m.group(2)):
            side = side.strip().strip('"')
            if "." not in side:
                break
            qual, _, col = side.rpartition(".")
            qual = qual.strip('"')
            t = None
            if "." in qual:                       # demo_sales.orders.customer_id
                t = _resolve_table(qual, entries, hint)
            else:                                 # o.customer_id / orders.customer_id
                t = alias_map.get(qual.lower())
            if t is None or col not in _columns_of(entries, *t):
                break
            ends.append((*t, col))
        if len(ends) == 2 and ends[0][:2] != ends[1][:2]:
            out.append((ends[0], ends[1]))
    return out


# =============================================================================
# 集計とAPI
# =============================================================================

def usage_counts() -> dict:
    """全履歴のJOINを数える。{edge_key: {"from","to","count"}}"""
    entries = _entries()
    sqls, chats = collect_sqls()
    edges: dict = {}
    tables: dict = {}
    for sql in sqls:
        for a, b in joins_in(sql, entries):
            key = _edge_key(a, b)
            hit = edges.setdefault(key, {"from": list(a), "to": list(b), "count": 0})
            hit["count"] += 1
        low = sql.lower()
        for e in entries:
            for t in e["profile"]["tables"]:
                if re.search(r'(?<![\w."])' + re.escape(t.lower()) + r'(?![\w])', low):
                    tables[f"{e['alias']}.{t}"] = tables.get(f"{e['alias']}.{t}", 0) + 1
    return {"edges": edges, "tables": tables,
            "scanned": {"chats": chats, "sqls": len(sqls)}}


def _declared_pairs(entries: list[dict]) -> set:
    """カタログ/FKに登録済みの結合（端点の組）。"""
    cat_entries = [{"alias": e["alias"], "profile": e["profile"],
                    "meta": catalog.load_meta(e["path"])} for e in entries]
    out = set()
    for edge in catalog.collect_edges(cat_entries):
        out.add(_edge_key(edge["from"], edge["to"]))
    return out


def usage_for(alias: str) -> dict:
    """ER図に重ねるためのデータ（このDBのキャンバス向け）。"""
    data = usage_counts()
    return {
        "edges": {k: v["count"] for k, v in data["edges"].items()},
        "tables": {k: n for k, n in data["tables"].items()
                   if k.startswith(alias + ".")},
        "scanned": data["scanned"],
    }


def suggestions_for(alias: str, profile: dict, meta: dict) -> list[dict]:
    """実際に使われているのにカタログに無い結合を「関連の候補」に出す。

    形は catalog.join_suggestions と同じ。from 側は必ずこのDBのテーブルにする
    （関連はそのDBの .meta.yaml に書かれるため）。
    """
    entries = _entries()
    if not entries:
        return []
    declared = _declared_pairs(entries)
    data = usage_counts()

    out = []
    for key, e in sorted(data["edges"].items(), key=lambda kv: -kv[1]["count"]):
        if key in declared:
            continue
        a, b = tuple(e["from"]), tuple(e["to"])
        # from 側をこのDBに揃える。どちらもこのDBでなければ、この画面では出さない
        if a[0] != alias and b[0] != alias:
            continue
        if a[0] != alias:
            a, b = b, a
        frm = f"{a[1]}.{a[2]}"
        to = f"{b[1]}.{b[2]}" if b[0] == alias else f"{b[0]}.{b[1]}.{b[2]}"
        out.append({"from": frm, "to": to, "cardinality": "N:1",
                    "reason": f"過去の分析で{e['count']}回使われています（未登録）"})
    return out[:8]
