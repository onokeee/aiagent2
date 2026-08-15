"""このアプリ自身が、誰にどう使われているかを数える。

sqlusage.py が「どの結合が通ったか」だけを見るのに対し、こちらは利用そのものを見る。
見たいのは利用者数ではなく、次の3つ。

  伸びているか   … 使われ続けているのか、最初の週だけだったのか
  何に使われるか … よく呼ばれる機能と、まったく呼ばれない機能
  どこで転ぶか   … 失敗した質問。これがカタログを直す入口になる

失敗の中身は分けて数える。「列が無い」はカタログ不足で人間が直せるが、
「LLM呼び出しに失敗」は設定や回線の問題で、カタログをいくら直しても減らない。
混ぜて「エラー率5%」と出すと、直せないものを直そうとして時間を溶かす。

材料は data/users/<ユーザー>/chats/*.json（会話の実体）と、
data/import_history.jsonl（取り込みの記録）。どちらも読むだけで書き換えない。

戻り値の形は advanced.py / business.py と同じ {"title", "tables", "notes", "meta"}。
画面もLLMも同じ入れ物で受け取れる。

注意: 発言ごとの時刻は保存していないので、時系列は会話の開始時刻で数える。
      1本の会話が日をまたいでも、始めた日の1件として扱う。
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import config
import history

#: 質問文・エラー文をそのまま並べるときの上限。多すぎると読まれない。
MAX_LIST = 40

METHODS = {
    "summary": "全体像（期間・利用者・会話数・失敗率）",
    "users": "利用者ごとの利用量",
    "trend": "日ごと・曜日・時間帯の推移",
    "tools": "呼ばれた機能の回数",
    "databases": "対象データ（DB）別の利用",
    "errors": "失敗の内訳と、直し方の当たり",
    "questions": "実際に聞かれた質問",
}

#: 失敗の分類。上から順に当てる（先に当たったものを採る）。
#: 「誰が直せるか」で分ける。カタログ担当・管理者・利用者では打ち手が違う。
_ERROR_KINDS = (
    ("カタログ不足（列・テーブルの取り違え）",
     r"no such (table|column)|列名が違います|テーブルが見つかりません",
     "describe_table で確認できる情報が足りていない。"
     "カタログの列説明・コード値・結合定義を書き足すと減る。"),
    ("SQLの誤り（構文・集計）",
     r"SQL実行エラー|syntax error|ambiguous|misuse of aggregate",
     "例文（Q&SQL）を足すと、AIが型を真似るので減る。"),
    ("分析に足りるデータが無い",
     r"行しかなく|0行でした|データが0行|足りません",
     "抽出条件が狭すぎる。期間を広げるか、分析の指定（説明変数など）を減らす。"),
    ("ツールの引数不足",
     r"(には|は).{0,30}(必要|指定してください)|引数|列が結果にありません|指定列",
     "ツールの説明文を具体的にすると、AIの指定ミスが減る。"),
    ("LLM・API側の問題",
     r"LLM呼び出しに失敗|Error code:|timeout|接続",
     "カタログでは直らない。モデル設定・APIキー・回線を確認する。"),
    ("実行時間切れ",
     r"時間がかかりすぎ|タイムアウト|interrupted",
     "対象データを絞るか、集計済みのユーザー定義ツールを用意する。"),
)

_WEEKDAYS = ("月", "火", "水", "木", "金", "土", "日")


def _dt(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _table(name: str, columns: list, rows: list) -> dict:
    return {"name": name, "columns": columns, "rows": [tuple(r) for r in rows]}


def _out(title: str, tables: list, notes: list, meta: dict | None = None) -> dict:
    return {"title": title, "tables": tables, "notes": notes, "meta": meta or {}}


def classify_error(message: str) -> tuple[str, str]:
    """エラー文を「誰が直せるか」で分類する。戻り値: (分類, 打ち手)"""
    for name, pattern, fix in _ERROR_KINDS:
        if re.search(pattern, message, re.IGNORECASE):
            return name, fix
    return "その他", "内容を読んで個別に判断する。"


# =============================================================================
# 材料集め
# =============================================================================

def collect(days: int | None = None, user: str | None = None) -> list[dict]:
    """会話ファイルを1会話1レコードに畳む。

    days を指定すると、その日数より前に始まった会話は捨てる。
    捨てるのは開始時刻で判定するので、古い会話を今日まで続けていた場合も対象外。
    """
    root = Path(config.USER_META_DIR)
    if not root.exists():
        return []
    limit = datetime.now() - timedelta(days=days) if days else None

    out = []
    for f in root.glob("*/chats/*.json"):
        if f.name == "index.json":
            continue
        who = f.parent.parent.name
        if user and who.lower() != user.lower():
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue                      # 壊れた1本のために全体を止めない
        created = _dt(data.get("created_at"))
        if limit and created and created < limit:
            continue

        log = data.get("render_log") or []
        kinds = Counter(str(i.get("kind") or "") for i in log)
        tools = [(tc.get("function") or {}).get("name")
                 for m in (data.get("messages") or [])
                 for tc in (m.get("tool_calls") or [])]
        out.append({
            "user": who,
            "id": data.get("id") or f.stem,
            "title": str(data.get("title") or "").strip(),
            "created": created,
            "updated": _dt(data.get("updated_at")),
            "questions": [str(i.get("content") or "").strip() for i in log
                          if i.get("role") == "user" and i.get("kind") == "text"],
            "tools": [t for t in tools if t],
            "errors": [str(i.get("message") or "") for i in log
                       if i.get("kind") == "error"],
            "dbs": list(data.get("db_names") or []),
            "sqls": kinds.get("sql", 0),
            "charts": kinds.get("chart", 0),
            "tables": kinds.get("table", 0),
            "files": kinds.get("file", 0),
            "reports": kinds.get("report", 0),
        })
    out.sort(key=lambda r: r["created"] or datetime.min)
    return out


def _period_note(records: list[dict]) -> str:
    days = [r["created"] for r in records if r["created"]]
    if not days:
        return "期間: 不明"
    return f"期間: {min(days):%Y-%m-%d} 〜 {max(days):%Y-%m-%d}"


def _empty(days: int | None) -> dict:
    span = f"直近{days}日には" if days else ""
    return _out("利用状況", [], [f"{span}会話の記録がありませんでした。"
                                "まだ誰も使っていないか、data/users/ が空です。"])


# =============================================================================
# 分析
# =============================================================================

def summary(records: list[dict], days: int | None = None) -> dict:
    """全体像。まずこれを見て、気になった軸を他のメソッドで掘る。"""
    if not records:
        return _empty(days)

    n_chats = len(records)
    n_turns = sum(len(r["questions"]) for r in records)
    users = {r["user"] for r in records}
    errs = [e for r in records for e in r["errors"]]
    chats_with_err = sum(1 for r in records if r["errors"])
    active_days = {r["created"].date() for r in records if r["created"]}

    rows = [
        ("会話", f"{n_chats:,} 件"),
        ("質問", f"{n_turns:,} 回"),
        ("利用者", f"{len(users)} 人"),
        ("使われた日", f"{len(active_days)} 日"),
        ("1会話あたりの質問", f"{n_turns / n_chats:.1f} 回"),
        ("失敗を含む会話", f"{chats_with_err} 件（{chats_with_err / n_chats * 100:.0f}%）"),
        ("作った表・グラフ", f"表 {sum(r['tables'] for r in records):,} / "
                            f"グラフ {sum(r['charts'] for r in records):,}"),
        ("出したファイル", f"{sum(r['files'] for r in records):,} 件"),
    ]

    notes = [_period_note(records)]
    # 1回で終わった会話が多いなら、続けて聞ける場になっていない可能性がある
    one_shot = sum(1 for r in records if len(r["questions"]) <= 1)
    if n_chats >= 5:
        notes.append(
            f"1問だけで終わった会話が {one_shot}/{n_chats} 件"
            f"（{one_shot / n_chats * 100:.0f}%）。"
            + ("会話を続けて掘り下げる使い方が根づいています。"
               if one_shot / n_chats < 0.5 else
               "多くが単発です。最初の答えで満足したか、続きを諦めたかのどちらかなので、"
               "失敗の内訳（errors）も合わせて見てください。"))
    if errs:
        kinds = Counter(classify_error(e)[0] for e in errs)
        top, n = kinds.most_common(1)[0]
        notes.append(f"失敗 {len(errs)} 件のうち最も多いのは「{top}」{n} 件。"
                     "内訳は errors で確認できます。")
    else:
        notes.append("記録された失敗はありません。")

    heavy = Counter(r["user"] for r in records).most_common(1)
    if heavy and len(users) > 1:
        who, cnt = heavy[0]
        notes.append(f"最も使っているのは {who}（{cnt} 件 / 全体の "
                     f"{cnt / n_chats * 100:.0f}%）。")

    return _out("利用状況の全体像", [_table("全体", ["項目", "値"], rows)], notes,
                {"chats": n_chats, "turns": n_turns, "users": len(users),
                 "errors": len(errs), "active_days": len(active_days)})


def by_user(records: list[dict]) -> dict:
    """利用者ごと。誰が使っていて、誰が離れたかを見る。"""
    if not records:
        return _empty(None)

    per: dict[str, list] = {}
    for r in records:
        per.setdefault(r["user"], []).append(r)

    rows = []
    for who, rs in sorted(per.items(), key=lambda kv: -len(kv[1])):
        last = max((r["updated"] or r["created"] for r in rs
                    if (r["updated"] or r["created"])), default=None)
        turns = sum(len(r["questions"]) for r in rs)
        errs = sum(len(r["errors"]) for r in rs)
        rows.append((who, len(rs), turns, round(turns / len(rs), 1), errs,
                     f"{last:%Y-%m-%d}" if last else "—"))
    notes = [_period_note(records),
             "「最終利用」が古い人は、使えなかったのか、必要が無かったのかを直接聞くのが早いです。"]

    today = datetime.now()
    stale = [r[0] for r in rows
             if r[5] != "—" and (today - datetime.fromisoformat(r[5])).days >= 14]
    if stale:
        notes.append(f"2週間以上使っていないのは {len(stale)} 人（{'、'.join(stale[:5])}"
                     f"{' ほか' if len(stale) > 5 else ''}）。")
    return _out("利用者ごとの利用量",
                [_table("利用者別", ["利用者", "会話", "質問", "1会話あたり", "失敗", "最終利用"],
                        rows)],
                notes, {"users": len(rows)})


def trend(records: list[dict]) -> dict:
    """日ごと・曜日・時間帯。定着したのか、一度きりだったのかを見る。"""
    if not records:
        return _empty(None)

    daily = Counter(r["created"].date() for r in records if r["created"])
    dow = Counter(r["created"].weekday() for r in records if r["created"])
    hour = Counter(r["created"].hour for r in records if r["created"])

    day_rows = [(str(d), n) for d, n in sorted(daily.items())]
    dow_rows = [(_WEEKDAYS[i], dow.get(i, 0)) for i in range(7)]
    hour_rows = [(f"{h:02d}時", hour.get(h, 0)) for h in range(24) if hour.get(h)]

    notes = [_period_note(records),
             "日付は会話を始めた時刻で数えています（発言ごとの時刻は保存していません）。"]
    if len(daily) >= 2:
        days_sorted = sorted(daily)
        span = (days_sorted[-1] - days_sorted[0]).days + 1
        notes.append(f"{span} 日のうち {len(daily)} 日に利用がありました"
                     f"（{len(daily) / span * 100:.0f}%）。")
        half = len(days_sorted) // 2
        first = sum(daily[d] for d in days_sorted[:half])
        last = sum(daily[d] for d in days_sorted[half:])
        if first:
            notes.append(f"前半 {first} 件 → 後半 {last} 件（{(last - first) / first * 100:+.0f}%）。"
                         + ("使われ方が伸びています。" if last > first else
                            "落ちています。失敗の内訳（errors）と合わせて見てください。"))
    if hour_rows:
        peak = max(hour_rows, key=lambda t: t[1])
        notes.append(f"最も使われる時間帯は {peak[0]}（{peak[1]} 件）。")

    return _out("利用の推移",
                [_table("日ごと", ["日付", "会話"], day_rows),
                 _table("曜日", ["曜日", "会話"], dow_rows),
                 _table("時間帯", ["時間", "会話"], hour_rows)],
                notes, {"active_days": len(daily)})


def by_tool(records: list[dict]) -> dict:
    """呼ばれた機能。使われていない機能は、説明文が悪いか、要らないかのどちらか。"""
    if not records:
        return _empty(None)

    calls = Counter(t for r in records for t in r["tools"])
    total = sum(calls.values())
    rows = [(name, n, f"{n / total * 100:.1f}%") for name, n in calls.most_common()]

    notes = [_period_note(records)]
    if total:
        notes.append(f"ツール呼び出しは合計 {total:,} 回。"
                     f"種類は {len(calls)} 種です。")
        top = calls.most_common(3)
        notes.append("よく使われるのは " +
                     "、".join(f"{n}（{c}回）" for n, c in top) + "。")
    try:
        import tools as _tools
        unused = sorted(set(_tools._HANDLERS) - set(calls))
        if unused:
            notes.append(f"一度も呼ばれていない組み込みツールが {len(unused)} 種あります"
                         f"（{'、'.join(unused[:8])}"
                         f"{' ほか' if len(unused) > 8 else ''}）。"
                         "要らないなら「ツール」タブで無効にすると、AIの選択肢が減って"
                         "呼び分けが安定します。使ってほしいなら説明文を具体的に書き直します。")
    except Exception:
        pass
    return _out("呼ばれた機能", [_table("ツール別", ["ツール", "回数", "割合"], rows)],
                notes, {"total_calls": total, "kinds": len(calls)})


def by_database(records: list[dict]) -> dict:
    """対象データ別。使われないDBは、選ばれていないのか、選べていないのか。"""
    if not records:
        return _empty(None)

    per = Counter(name for r in records for name in set(r["dbs"]))
    rows = [(name, n, f"{n / len(records) * 100:.0f}%") for name, n in per.most_common()]
    widths = Counter(len(set(r["dbs"])) for r in records)
    width_rows = [(f"{k} DB", v) for k, v in sorted(widths.items())]

    notes = [_period_note(records),
             "「選択された会話数」なので、実際にSQLが当たったかまでは見ていません"
             "（実際に通った結合はカタログの「利用状況」で見られます）。"]
    multi = sum(v for k, v in widths.items() if k >= 2)
    if records:
        notes.append(f"2つ以上のDBを選んだ会話は {multi}/{len(records)} 件"
                     f"（{multi / len(records) * 100:.0f}%）。"
                     + ("DBをまたぐ分析が実際に行われています。" if multi else
                        "横断分析がまだ使われていません。"
                        "またぎの結合定義と例文を足すと使われやすくなります。"))
    return _out("対象データ別の利用",
                [_table("DB別", ["DB", "会話数", "割合"], rows),
                 _table("同時に選んだDBの数", ["選択数", "会話"], width_rows)],
                notes, {"dbs": len(per)})


def errors(records: list[dict]) -> dict:
    """失敗の内訳。カタログを直して減るものと、そうでないものを分ける。"""
    if not records:
        return _empty(None)

    items = [(r["user"], r["created"], e, r["questions"][0] if r["questions"] else "")
             for r in records for e in r["errors"]]
    if not items:
        return _out("失敗の内訳", [],
                    [_period_note(records),
                     "記録された失敗はありません。"], {"errors": 0})

    kinds: Counter = Counter()
    fixes: dict[str, str] = {}
    for _, _, msg, _ in items:
        kind, fix = classify_error(msg)
        kinds[kind] += 1
        fixes.setdefault(kind, fix)
    kind_rows = [(name, n, f"{n / len(items) * 100:.0f}%", fixes.get(name, ""))
                 for name, n in kinds.most_common()]

    detail_rows = [(f"{dt:%m-%d}" if dt else "—", who, (q or "")[:40],
                    msg.splitlines()[0][:80])
                   for who, dt, msg, q in items[-MAX_LIST:]]

    notes = [_period_note(records),
             f"失敗 {len(items)} 件。分類は「誰が直せるか」で分けています。"]
    # 「カタログ画面で直せるもの」だけを足す。打ち手はいちばん多い分類のものを出す
    # （合計だけ言われても、何から手を付ければよいか分からないため）。
    fixable = {k: n for k, n in kinds.items()
               if k.startswith(("カタログ", "SQL", "ツール"))}
    if fixable:
        catalog_side = sum(fixable.values())
        top = max(fixable.items(), key=lambda kv: kv[1])
        notes.append(f"うち {catalog_side} 件（{catalog_side / len(items) * 100:.0f}%）は"
                     "カタログ画面での手当てで減らせます。"
                     f"いちばん多いのは「{top[0]}」{top[1]} 件で、{fixes.get(top[0], '')}")
    outside = kinds.get("LLM・API側の問題", 0)
    if outside:
        notes.append(f"{outside} 件はモデル・API側の問題で、カタログを直しても減りません。")
    return _out("失敗の内訳",
                [_table("分類", ["分類", "件数", "割合", "打ち手"], kind_rows),
                 _table(f"直近の失敗（最大{MAX_LIST}件）",
                        ["日付", "利用者", "質問", "内容"], detail_rows)],
                notes, {"errors": len(items), "catalog_fixable": catalog_side})


def questions(records: list[dict]) -> dict:
    """実際に聞かれた質問。例文とカタログに反映するための材料。"""
    if not records:
        return _empty(None)

    asked = [(r["created"], r["user"], q, bool(r["errors"]))
             for r in records for q in r["questions"] if q]
    if not asked:
        return _out("聞かれた質問", [], [_period_note(records), "質問の記録がありません。"])

    rows = [(f"{dt:%m-%d}" if dt else "—", who, q[:60], "×" if bad else "")
            for dt, who, q, bad in asked[-MAX_LIST:]]

    # 何を聞かれがちかを、語で大づかみに見る（形態素解析は入れない。傾向が分かれば足りる）
    words = Counter()
    for _, _, q, _ in asked:
        for w in re.findall(r"[一-龥ぁ-んァ-ヶa-zA-Z0-9]{2,}", q):
            if w not in ("教えて", "ください", "して", "この", "その", "どの", "です"):
                words[w] += 1
    word_rows = [(w, n) for w, n in words.most_common(20) if n > 1]

    notes = [_period_note(records),
             f"質問 {len(asked)} 件を記録しています。",
             "うまく答えられた質問は、チャットの⭐から例文としてカタログに登録できます。"
             "例文が増えるほど、同じ聞き方への精度が上がります。"]
    failed = sum(1 for *_, bad in asked if bad)
    if failed:
        notes.append(f"失敗を含む会話の質問が {failed} 件（× 印）。"
                     "この質問文がそのまま、カタログに足りない語彙の一覧になります。")
    return _out("聞かれた質問",
                [_table(f"直近の質問（最大{MAX_LIST}件）",
                        ["日付", "利用者", "質問", "失敗"], rows),
                 _table("よく出る語", ["語", "回数"], word_rows)],
                notes, {"questions": len(asked)})


def imports(days: int | None = None) -> dict:
    """取り込みの実績。チャットとは別系統なので、まとめてここから見えるようにする。"""
    recs = history.recent(limit=2000)
    if not recs:
        return _out("取り込みの実績", [], ["取り込みの記録がありません。"], {"runs": 0})

    limit = datetime.now() - timedelta(days=days) if days else None
    picked = []
    for r in recs:
        at = _dt(r.get("at") or r.get("started"))
        if limit and at and at < limit:
            continue
        picked.append((at, r))
    if not picked:
        return _out("取り込みの実績", [], [f"直近{days}日に取り込みの記録がありません。"],
                    {"runs": 0})

    ok = sum(1 for _, r in picked if r.get("ok"))
    kinds = Counter(history.KINDS.get(str(r.get("kind")), str(r.get("kind")))
                    for _, r in picked)
    rows = [(k, n) for k, n in kinds.most_common()]
    tables = Counter(f"{r.get('db_file')} / {r.get('table')}" for _, r in picked)
    tbl_rows = [(name, n) for name, n in tables.most_common(20)]

    notes = [f"取り込み {len(picked)} 回。成功 {ok} 件 / 失敗 {len(picked) - ok} 件"
             f"（成功率 {ok / len(picked) * 100:.0f}%）。"]
    fails = [r for _, r in picked if not r.get("ok")]
    if fails:
        notes.append("直近の失敗: " + str(fails[-1].get("message", ""))[:100])
    return _out("取り込みの実績",
                [_table("実行のしかた別", ["種別", "回数"], rows),
                 _table("テーブル別", ["DB / テーブル", "回数"], tbl_rows)],
                notes, {"runs": len(picked), "ok": ok})


#: メソッド名 -> 実処理。records を取らない imports だけ形が違う。
_METHOD_FUNCS = {
    "summary": summary,
    "users": by_user,
    "trend": trend,
    "tools": by_tool,
    "databases": by_database,
    "errors": errors,
    "questions": questions,
}


def analyze(method: str = "summary", days: int | None = None,
            user: str | None = None) -> dict:
    """入口。method はこのモジュールの METHODS のいずれか。"""
    method = (method or "summary").strip().lower()
    if method == "imports":
        return imports(days)
    fn = _METHOD_FUNCS.get(method)
    if fn is None:
        raise ValueError(f"method は {'、'.join([*METHODS, 'imports'])} "
                         f"のいずれかです（受け取った値: {method}）")
    records = collect(days=days, user=user)
    res = fn(records, days) if fn is summary else fn(records)
    if user:
        res["notes"] = [f"対象: {user} のみ", *res.get("notes", [])]
    return res
