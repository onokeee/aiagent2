"""メールの下書きと送信（SMTP）。

方針: 「作る」と「送る」を必ず分ける。
LLMは compose（下書き作成）までしかできず、実際の送信は
画面でユーザーが本文と宛先を見て承認したときだけ実行される。
宛先を間違えた1通は取り消せないので、AIの判断だけでは外に出さない。

宛先はDBのテーブルから探す。人の情報がどのテーブルにあるかは
DBごとに違うので、列名と実際の値（@を含むか等）から推測する。
"""
from __future__ import annotations

import mimetypes
import re
import smtplib
import ssl
import threading
from dataclasses import dataclass, field
from datetime import datetime
from email.header import Header
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr

import yaml

import config
import db

# ざっくりだが実用上これで十分。厳密なRFC準拠より、明らかな入力ミスを弾く方が大事。
EMAIL_RE = re.compile(r"^[^@\s,;:<>\"]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")

# 宛先探しで手がかりにする列名（部分一致・大文字小文字は無視）
MAIL_HINTS = ("mail", "メール", "eメール", "address", "アドレス", "宛先", "email")
NAME_HINTS = ("name", "氏名", "名前", "担当", "社員名", "person", "user", "顧客名", "得意先")
DEPT_HINTS = ("部署", "部門", "所属", "課", "dept", "department", "division", "組織", "拠点")

_lock = threading.Lock()
_sent_log: list[dict] = []          # 直近の送信記録（画面表示用）
_MAX_LOG = 200


class MailError(Exception):
    """送信できない理由（そのまま画面に出す）。"""


# =============================================================================
# 設定
# =============================================================================

@dataclass
class SmtpSettings:
    host: str = ""
    port: int = 25
    security: str = "none"          # none / starttls / ssl
    user: str = ""
    password: str = ""
    sender: str = ""
    sender_name: str = ""
    timeout: int = 20
    allow_addresses: list = field(default_factory=list)
    senders: list = field(default_factory=list)      # 画面で選べる差出人の候補
    max_recipients: int = 20
    dry_run: bool = True
    alert_to: list = field(default_factory=list)     # 定期取り込みの失敗を知らせる管理者

    @property
    def configured(self) -> bool:
        return bool(self.host and self.sender)

    def problems(self) -> list[str]:
        out = []
        if not self.host:
            out.append("送信サーバのホスト名が未設定です。「メール設定」画面で登録してください。")
        if not self.sender:
            out.append("差出人アドレスが未設定です。「メール設定」画面で登録してください。")
        if not self.allow_addresses:
            out.append("送信できる宛先が1件も登録されていません。"
                       "登録するまでメールは送れません（「メール設定」画面で追加してください）。")
        elif not EMAIL_RE.match(self.sender):
            out.append(f"差出人アドレスの形式が正しくありません: {self.sender}")
        if self.security not in ("none", "starttls", "ssl"):
            out.append(f"暗号化は none / starttls / ssl のいずれかです: {self.security}")
        if self.user and not self.password:
            out.append("認証ユーザー名を設定したのに、パスワードが空です。")
        return out

    def allows(self, address: str) -> bool:
        """この宛先に送ってよいか。

        許可リストに載っているアドレスだけに送れる。**空のときは誰にも送れない。**
        「未設定なら全員に送れる」だと、設定を忘れたまま社外へ出てしまうため。
        """
        if not self.allow_addresses:
            return False
        return str(address).strip().lower() in [a.lower() for a in self.allow_addresses]

    @property
    def restricted(self) -> bool:
        """常に True。許可リスト方式なので、制限が外れることはない。"""
        return True


# =============================================================================
# 画面から変えられる設定（data/mail_settings.yaml）
#
# env の値を初期値として、このファイルの内容を上書きで重ねる。
# この設定ファイルは中身をそのまま画面に出すので、秘密は入れないこと。
# =============================================================================

# 送信サーバ（接続先）。暗号化と認証は社内リレー前提で画面に出さないので、
# 変えたい環境では env の SMTP_SECURITY / SMTP_USER / SMTP_PASSWORD を使う。
SERVER_KEYS = ("host", "port", "timeout")
# 差出人と宛先まわり
EDITABLE_KEYS = SERVER_KEYS + ("sender", "sender_name", "senders",
                               "allow_addresses", "max_recipients", "dry_run",
                               "alert_to")


def _read_overrides() -> dict:
    p = config.SMTP_SETTINGS_FILE
    if not p.exists():
        return {}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[mailer] 設定を読めませんでした: {p} ({e})")
        return {}
    return {k: v for k, v in (data or {}).items() if k in EDITABLE_KEYS} \
        if isinstance(data, dict) else {}


def _write_overrides(data: dict) -> None:
    p = config.SMTP_SETTINGS_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        p.write_text(yaml.safe_dump({k: data[k] for k in EDITABLE_KEYS if k in data},
                                    allow_unicode=True, sort_keys=False),
                     encoding="utf-8")


def settings() -> SmtpSettings:
    """env の値に、画面から保存した設定を重ねて返す。"""
    ov = _read_overrides()
    senders = [str(s).strip() for s in (ov.get("senders") or []) if str(s).strip()]
    sender = str(ov.get("sender") or config.SMTP_SENDER or "").strip()
    if not sender and senders:
        sender = senders[0]
    return SmtpSettings(
        host=str(ov.get("host", config.SMTP_HOST) or "").strip(),
        port=int(ov.get("port", config.SMTP_PORT) or 25),
        security=str(config.SMTP_SECURITY or "none").lower(),
        user=str(config.SMTP_USER or "").strip(),
        password=config.SMTP_PASSWORD,
        sender=sender,
        sender_name=str(ov.get("sender_name", config.SMTP_SENDER_NAME) or "").strip(),
        timeout=int(ov.get("timeout", config.SMTP_TIMEOUT) or 20),
        allow_addresses=[str(a).strip() for a in (ov.get("allow_addresses") or [])
                         if str(a).strip()],
        senders=senders,
        max_recipients=int(ov.get("max_recipients", config.SMTP_MAX_RECIPIENTS) or 20),
        dry_run=bool(ov["dry_run"]) if "dry_run" in ov else config.SMTP_DRY_RUN,
        alert_to=[str(a).strip() for a in (ov.get("alert_to") or []) if str(a).strip()],
    )


# --- 宛先に登録してよいドメイン（env の SEND_OK_MAIL_DOMAIN）------------------------

def domain_ok(address: str) -> bool:
    """このアドレスを許可リストに登録してよいか。

    env が空なら制限なし（それでも許可リストへの登録自体は必要）。
    サブドメイン（sales.example.co.jp）も対象に含める。
    """
    allowed = config.SEND_OK_MAIL_DOMAIN
    if not allowed:
        return True
    dom = str(address).strip().lower().rsplit("@", 1)[-1]
    return any(dom == d or dom.endswith("." + d) for d in allowed)


def allowed_domains_label() -> str:
    """画面に出す「登録できるドメイン」の表記。"""
    return "、".join("@" + d for d in config.SEND_OK_MAIL_DOMAIN) or "すべてのドメイン"


_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$")


def _validate_server(data: dict) -> list[str]:
    """送信サーバ（接続情報）の点検。"""
    errors = []
    host = str(data.get("host") or "").strip()
    if not host:
        errors.append("SMTPサーバのホスト名を入力してください。")
    elif not _HOSTNAME_RE.match(host):
        errors.append(f"ホスト名の形式が正しくありません: {host}")

    try:
        port = int(data.get("port") or 0)
    except (TypeError, ValueError):
        errors.append("ポート番号は数値で指定してください。")
    else:
        if not (1 <= port <= 65535):
            errors.append("ポート番号は 1〜65535 で指定してください。")

    try:
        t = int(data.get("timeout") or 0)
    except (TypeError, ValueError):
        errors.append("タイムアウトは数値で指定してください。")
    else:
        if not (1 <= t <= 300):
            errors.append("タイムアウトは 1〜300 秒で指定してください。")
    return errors


def validate_settings(data: dict) -> list[str]:
    """画面から来た設定の点検。1つでも返ったら保存しない。"""
    errors = _validate_server(data)
    senders = [str(s).strip() for s in (data.get("senders") or []) if str(s).strip()]
    for s in senders:
        if not EMAIL_RE.match(s):
            errors.append(f"差出人アドレスの形式が正しくありません: {s}")
    sender = str(data.get("sender") or "").strip()
    if not sender:
        errors.append("使用する差出人アドレスを選んでください。")
    elif not EMAIL_RE.match(sender):
        errors.append(f"差出人アドレスの形式が正しくありません: {sender}")
    elif senders and sender not in senders:
        errors.append(f"{sender} は差出人の候補に入っていません。先に候補へ追加してください。")

    for a in (data.get("allow_addresses") or []):
        addr = str(a).strip()
        if not EMAIL_RE.match(addr):
            errors.append(f"宛先として登録できない形式です: {a}")
        elif not domain_ok(addr):
            errors.append(f"{addr} は登録できません。"
                          f"登録できるのは {allowed_domains_label()} のアドレスだけです"
                          "（env の SEND_OK_MAIL_DOMAIN）。")
    # 通知先の管理者も同じ縛り（許可ドメイン）。社外へは飛ばさない
    for a in (data.get("alert_to") or []):
        addr = str(a).strip()
        if not EMAIL_RE.match(addr):
            errors.append(f"通知先として登録できない形式です: {a}")
        elif not domain_ok(addr):
            errors.append(f"{addr} は通知先に登録できません。"
                          f"登録できるのは {allowed_domains_label()} のアドレスだけです。")

    try:
        n = int(data.get("max_recipients") or 0)
    except (TypeError, ValueError):
        errors.append("一度に送れる宛先数は数値で指定してください。")
    else:
        if not (1 <= n <= 500):
            errors.append("一度に送れる宛先数は 1〜500 で指定してください。")
    return errors


def _with_current(data: dict) -> dict:
    """省略されたキーを現在の値で埋める。

    画面は毎回すべて送ってくるが、一部だけ変えたい呼び出し方もできるように
    しておく。埋めずに検証すると「送っていない項目」で弾かれてしまう。
    """
    s = settings()
    merged = {"host": s.host, "port": s.port, "timeout": s.timeout,
              "sender": s.sender, "sender_name": s.sender_name,
              "senders": s.senders, "allow_addresses": s.allow_addresses,
              "max_recipients": s.max_recipients, "dry_run": s.dry_run,
              "alert_to": s.alert_to}
    # None は「指定なし」。空文字や空リストは「消したい」なので通す。
    merged.update({k: v for k, v in (data or {}).items()
                   if k in merged and v is not None})
    return merged


def save_settings(data: dict, user: str | None = None) -> SmtpSettings:
    """画面からの保存。検証してから書く。"""
    merged = _with_current(data)
    errors = validate_settings(merged)
    if errors:
        raise MailError(" / ".join(errors))
    keep = _read_overrides()
    keep.update({
        "host": str(merged["host"] or "").strip(),
        "port": int(merged["port"] or 25),
        "timeout": int(merged["timeout"] or 20),
        "sender": str(merged["sender"] or "").strip(),
        "sender_name": str(merged["sender_name"] or "").strip(),
        "senders": [str(s).strip() for s in (merged["senders"] or []) if str(s).strip()],
        "allow_addresses": [str(a).strip() for a in (merged["allow_addresses"] or [])
                            if str(a).strip()],
        "max_recipients": int(merged["max_recipients"] or 20),
        "dry_run": bool(merged["dry_run"]),
        "alert_to": [str(a).strip() for a in (merged.get("alert_to") or [])
                     if str(a).strip()],
    })
    _write_overrides(keep)
    print(f"[mailer] 設定を更新しました（{user or '不明'}）: "
          f"サーバ={keep['host']}:{keep['port']} / "
          f"差出人={keep['sender']} / 宛先制限="
          f"{len(keep['allow_addresses'])}アドレス / "
          f"テスト送信={keep['dry_run']}")
    return settings()


def status() -> dict:
    """画面に渡す現在の設定。認証情報は含めない。"""
    s = settings()
    return {"configured": s.configured, "host": s.host, "port": s.port,
            "sender": s.sender,
            "sender_name": s.sender_name, "dry_run": s.dry_run,
            "senders": s.senders,
            "allow_addresses": s.allow_addresses,
            "alert_to": s.alert_to,
            "restricted": s.restricted, "timeout": s.timeout,
            "allowed_domains": list(config.SEND_OK_MAIL_DOMAIN),
            "allowed_domains_label": allowed_domains_label(),
            "max_recipients": s.max_recipients, "problems": s.problems(),
            "settings_file": str(config.SMTP_SETTINGS_FILE)}


# =============================================================================
# 宛先を探す
# =============================================================================

def _hit(name: str, hints) -> bool:
    low = str(name).lower()
    return any(h.lower() in low for h in hints)


def _looks_like_email_column(conn, table: str, column: str) -> bool:
    """列名で分からないときは、実際の値に @ が入っているかで判断する。"""
    try:
        cur = conn.execute(f'SELECT "{column}" FROM "{table}" '
                           f'WHERE "{column}" IS NOT NULL LIMIT 20')
        vals = [str(r[0]) for r in cur.fetchall()]
    except Exception:
        return False
    if not vals:
        return False
    return sum(1 for v in vals if "@" in v and "." in v.split("@")[-1]) >= max(1, len(vals) // 2)


def address_tables(scope: list[dict]) -> list[dict]:
    """選択中のDBから「人とメールアドレスが載っていそうな表」を探す。"""
    found = []
    for s in scope:
        try:
            conn = db.connect_ro(s["path"])
        except Exception:
            continue
        try:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            for t in tables:
                if s.get("tables") and t not in s["tables"]:
                    continue
                try:
                    cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')]
                except Exception:
                    continue
                mail_cols = [c for c in cols if _hit(c, MAIL_HINTS)]
                if not mail_cols:
                    mail_cols = [c for c in cols if _looks_like_email_column(conn, t, c)]
                if not mail_cols:
                    continue
                found.append({
                    "alias": s["alias"], "table": t, "columns": cols,
                    "mail_columns": mail_cols,
                    "name_columns": [c for c in cols if _hit(c, NAME_HINTS)],
                    "dept_columns": [c for c in cols if _hit(c, DEPT_HINTS)],
                })
        finally:
            conn.close()
    return found


def find_recipients(scope: list[dict], query: str = "", *, limit: int = 50,
                    table: str | None = None) -> dict:
    """名前・部署・アドレスの断片から宛先候補を探す。

    どの表を見ればよいかは address_tables() が推測する。
    query が空なら、その表の先頭から候補を出す（一覧確認のため）。
    """
    sources = address_tables(scope)
    if table:
        sources = [s for s in sources
                   if s["table"] == table or f"{s['alias']}.{s['table']}" == table]
    if not sources:
        return {"ok": False, "candidates": [], "sources": [],
                "message": "メールアドレスが入っていそうな表が、選択中のDBに見つかりません。"
                           "サイドバーで名簿のあるDBを選ぶか、"
                           "アドレスを直接指定してください。"}

    conf = settings()
    q = str(query or "").strip()
    out, seen = [], set()
    for src in sources:
        cols = src["columns"]
        search_cols = list(dict.fromkeys(
            src["mail_columns"] + src["name_columns"] + src["dept_columns"]
            + [c for c in cols if c not in src["mail_columns"]]))[:12]
        where, params = "", {}
        if q:
            # 値はプレースホルダで渡す。列名は実在するものだけを使うので識別子として安全。
            where = " WHERE " + " OR ".join(f'CAST("{c}" AS TEXT) LIKE :q'
                                            for c in search_cols)
            params["q"] = f"%{q}%"
        sql = (f'SELECT {", ".join(chr(34) + c + chr(34) for c in cols)} '
               f'FROM "{src["table"]}"{where} LIMIT {int(limit)}')
        try:
            conn = db.connect_ro(next(s["path"] for s in scope
                                      if s["alias"] == src["alias"]))
        except Exception:
            continue
        try:
            rows = conn.execute(sql, params).fetchall()
        except Exception:
            rows = []
        finally:
            conn.close()

        for r in rows:
            rec = dict(zip(cols, r))
            mail = next((str(rec[c]).strip() for c in src["mail_columns"]
                         if rec.get(c) and "@" in str(rec[c])), "")
            if not mail or mail.lower() in seen:
                continue
            seen.add(mail.lower())
            out.append({
                "email": mail,
                "name": next((str(rec[c]) for c in src["name_columns"] if rec.get(c)), ""),
                "dept": next((str(rec[c]) for c in src["dept_columns"] if rec.get(c)), ""),
                "source": f"{src['alias']}.{src['table']}",
                "valid": bool(EMAIL_RE.match(mail)),
                # 許可リストの外なら、下書きを作る前に分かるようにしておく
                "allowed": conf.allows(mail),
                "row": {k: (str(v) if v is not None else "") for k, v in list(rec.items())[:8]},
            })
            if len(out) >= limit:
                break
        if len(out) >= limit:
            break

    msg = (f"{len(out)}件の宛先候補が見つかりました。" if out
           else f"「{q}」に一致する宛先が見つかりませんでした。"
                f"探した表: {', '.join(s['alias'] + '.' + s['table'] for s in sources)}")
    return {"ok": bool(out), "candidates": out, "message": msg,
            "sources": [{"table": f"{s['alias']}.{s['table']}",
                         "mail_columns": s["mail_columns"],
                         "name_columns": s["name_columns"],
                         "dept_columns": s["dept_columns"]} for s in sources]}


# =============================================================================
# 下書きの検証
# =============================================================================

def _norm_addresses(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"[,;\n]", value)
    else:
        parts = list(value)
    out = []
    for p in parts:
        p = str(p).strip()
        if not p:
            continue
        name, addr = parseaddr(p)
        out.append(addr or p)
    return out


def validate_draft(draft: dict, *, system: bool = False) -> list[str]:
    """送る前の点検。1つでも返ったら送信できない。

    system=True は、アプリ自身が管理者へ送る通知（定期取り込みの失敗など）。
    宛先は「メール設定」の通知先（alert_to）そのものなので、利用者向けの
    許可リスト（allow_addresses）とは独立に通す。サーバ・差出人の設定は同じく必要。
    """
    s = settings()
    errors = [e for e in s.problems()
              if not (system and "送信できる宛先" in e)]
    to = _norm_addresses(draft.get("to"))
    cc = _norm_addresses(draft.get("cc"))
    bcc = _norm_addresses(draft.get("bcc"))
    if not to:
        errors.append("宛先(To)が空です。")
    for addr in to + cc + bcc:
        if not EMAIL_RE.match(addr):
            errors.append(f"アドレスの形式が正しくありません: {addr}")
    total = len(to) + len(cc) + len(bcc)
    if total > s.max_recipients:
        errors.append(f"宛先が {total} 件あります。一度に送れるのは "
                      f"{s.max_recipients} 件までです（SMTP_MAX_RECIPIENTS）。")
    if system:
        # 通知先として登録したアドレスにだけ送る
        ok = {a.lower() for a in s.alert_to}
        for addr in to + cc + bcc:
            if addr.lower() not in ok:
                errors.append(f"{addr} は通知先に登録されていません。")
    elif not s.allow_addresses:
        errors.append("送信できる宛先が1件も登録されていません。"
                      "「メール設定」画面で登録するまで、どこにも送信できません。")
    else:
        for addr in to + cc + bcc:
            if not s.allows(addr):
                errors.append(f"{addr} は送信が許可されていません"
                              f"（許可済みは {len(s.allow_addresses)}件）。"
                              "メール設定で追加してください。")
    if not str(draft.get("subject") or "").strip():
        errors.append("件名が空です。")
    if not str(draft.get("body") or "").strip():
        errors.append("本文が空です。")
    return errors


def build_message(draft: dict, attachments: list[dict] | None = None) -> EmailMessage:
    """EmailMessage を組み立てる（送信せずに中身を確認するのにも使う）。"""
    s = settings()
    msg = EmailMessage()
    msg["From"] = formataddr((str(Header(s.sender_name, "utf-8")), s.sender)) \
        if s.sender_name else s.sender
    msg["To"] = ", ".join(_norm_addresses(draft.get("to")))
    if _norm_addresses(draft.get("cc")):
        msg["Cc"] = ", ".join(_norm_addresses(draft.get("cc")))
    if draft.get("reply_to"):
        msg["Reply-To"] = str(draft["reply_to"])
    msg["Subject"] = str(draft.get("subject") or "")
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.set_content(str(draft.get("body") or ""))

    for a in (attachments or []):
        data, name = a.get("data"), a.get("filename") or "attachment"
        if not data:
            continue
        mime = a.get("mime") or mimetypes.guess_type(name)[0] or "application/octet-stream"
        maintype, _, subtype = mime.partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype or "octet-stream",
                           filename=name)
    return msg


def preview(draft: dict, attachments: list[dict] | None = None) -> dict:
    """送信せずに、送られる内容をそのまま見せる。"""
    s = settings()
    to = _norm_addresses(draft.get("to"))
    cc = _norm_addresses(draft.get("cc"))
    bcc = _norm_addresses(draft.get("bcc"))
    body = str(draft.get("body") or "")
    return {
        "from": (f"{s.sender_name} <{s.sender}>" if s.sender_name else s.sender),
        "to": to, "cc": cc, "bcc": bcc,
        "subject": str(draft.get("subject") or ""),
        "body": body,
        "body_lines": len(body.splitlines()),
        "attachments": [{"filename": a.get("filename"),
                         "size": len(a.get("data") or b"")}
                        for a in (attachments or [])],
        "errors": validate_draft(draft),
        "dry_run": s.dry_run,
        "smtp": f"{s.host}:{s.port} ({s.security})",
    }


# =============================================================================
# 送信
# =============================================================================

def _connect(s: SmtpSettings):
    if s.security == "ssl":
        server = smtplib.SMTP_SSL(s.host, s.port, timeout=s.timeout,
                                  context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(s.host, s.port, timeout=s.timeout)
        if s.security == "starttls":
            server.starttls(context=ssl.create_default_context())
    if s.user:
        server.login(s.user, s.password)
    return server


def send(draft: dict, attachments: list[dict] | None = None,
         user: str | None = None, *, system: bool = False) -> dict:
    """実際に送る。呼ぶ前に必ずユーザーの承認を取ること。

    SMTP_DRY_RUN=true のあいだは接続せず、組み立てた内容だけ返す
    （本番のSMTPを教えてもらう前に画面を試せるようにするため）。
    system=True はアプリ自身からの管理者通知（validate_draft 参照）。
    """
    errors = validate_draft(draft, system=system)
    if errors:
        raise MailError(" / ".join(errors))
    s = settings()
    msg = build_message(draft, attachments)
    recipients = (_norm_addresses(draft.get("to")) + _norm_addresses(draft.get("cc"))
                  + _norm_addresses(draft.get("bcc")))

    record = {"at": datetime.now().isoformat(timespec="seconds"),
              "to": _norm_addresses(draft.get("to")),
              "cc": _norm_addresses(draft.get("cc")),
              "bcc_count": len(_norm_addresses(draft.get("bcc"))),
              "subject": msg["Subject"], "user": user,
              "attachments": [a.get("filename") for a in (attachments or [])],
              "dry_run": s.dry_run, "ok": False, "message": ""}

    if s.dry_run:
        record.update(ok=True, message="下書きの確認のみ（SMTP_DRY_RUN=true のため送信していません）")
    else:
        try:
            server = _connect(s)
        except Exception as e:
            record["message"] = f"SMTPサーバに接続できません（{s.host}:{s.port}）: {e}"
            _log(record)
            raise MailError(record["message"]) from e
        try:
            server.send_message(msg, from_addr=s.sender, to_addrs=recipients)
            record.update(ok=True, message=f"{len(recipients)}件の宛先に送信しました。")
        except Exception as e:
            record["message"] = f"送信に失敗しました: {e}"
            _log(record)
            raise MailError(record["message"]) from e
        finally:
            try:
                server.quit()
            except Exception:
                pass
    _log(record)
    return record


def test_connection() -> dict:
    """設定の疎通確認だけ行う（メールは送らない）。"""
    s = settings()
    problems = s.problems()
    if problems:
        return {"ok": False, "message": " / ".join(problems)}
    try:
        server = _connect(s)
    except Exception as e:
        return {"ok": False, "message": f"接続できませんでした（{s.host}:{s.port}）: {e}"}
    try:
        server.noop()
        return {"ok": True, "message": f"{s.host}:{s.port} に接続できました"
                                       f"（{s.security}{'・認証あり' if s.user else ''}）。"}
    finally:
        try:
            server.quit()
        except Exception:
            pass


def _log(record: dict) -> None:
    with _lock:
        _sent_log.append(record)
        while len(_sent_log) > _MAX_LOG:
            _sent_log.pop(0)


def sent_log(limit: int = 50) -> list[dict]:
    with _lock:
        return list(reversed(_sent_log[-limit:]))


# =============================================================================
# 定期取り込みの失敗を管理者に知らせる
#
# 状態が「健全 → 失敗」に変わった瞬間に1回だけ送る。失敗が続くあいだ毎周期
# 送ると（15分ごとの設定なら1日96通）読まれなくなるので、直るまで黙る。
# 直ったら「復旧しました」を1回送る。宛先は「メール設定」の通知先（管理者）。
# =============================================================================

def alert_import_problems(current: list[dict], previous: list[dict]) -> dict | None:
    """定期取り込みの状態変化を管理者に送る。送らなかったときは None。

    current / previous は jobs.problems() の結果（今回と前回）。
    """
    s = settings()
    if not s.alert_to:
        return None
    now_ids = {p["id"]: p for p in current}
    prev_ids = {p["id"]: p for p in previous}
    newly = [now_ids[i] for i in now_ids if i not in prev_ids]
    fixed = [prev_ids[i] for i in prev_ids if i not in now_ids]
    if not newly and not fixed:
        return None

    lines = []
    if newly:
        lines.append("■ 設定どおりに更新できなくなった定期取り込み")
        for p in newly:
            lines.append(f"  ・{p['name']}（{p['db_file']} / {p['table']}）")
            lines.append(f"     {p['message']}")
        lines.append("")
        lines.append("  対処: 取り込み元のファイル・シート名・列構成を確認してください。")
        lines.append("  失敗している間、既存のデータは変わりません（前回の内容のまま残っています）。")
        lines.append("")
    if fixed:
        lines.append("■ 復旧した定期取り込み")
        for p in fixed:
            lines.append(f"  ・{p['name']}（{p['db_file']} / {p['table']}）")
        lines.append("")
    lines.append(f"確認: データカタログ > DB・テーブル > 各テーブルの「管理」")
    subject = ("[DB分析アシスタント] 定期取り込みが失敗しています"
               if newly else "[DB分析アシスタント] 定期取り込みが復旧しました")
    draft = {"to": list(s.alert_to), "subject": subject, "body": "\n".join(lines)}
    try:
        return send(draft, [], user="scheduler", system=True)
    except Exception as e:
        print(f"[mailer] 通知を送れませんでした: {e}")
        return None
