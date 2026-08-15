"""ダウンロードさせるファイル（CSV / テキスト / ZIP）の組み立てと、自動保存用HTML。

xlsx の組み立ては excel.py。いずれもディスクには書かず、メモリ上のバイト列を返す。
"""
from __future__ import annotations

import csv
import datetime as _dt
import io
import re
import zipfile

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
CSV_MIME = "text/csv"
TEXT_MIME = "text/plain"
MD_MIME = "text/markdown"
ZIP_MIME = "application/zip"

# CSVの文字コード。Excelでそのまま開ける utf-8-sig を既定にする。
ENCODINGS = {
    "utf-8-sig": "UTF-8（BOM付き／Excelで文字化けしない・推奨）",
    "utf-8": "UTF-8（BOMなし）",
    "cp932": "Shift_JIS（cp932／古いWindows向け）",
}
DEFAULT_ENCODING = "utf-8-sig"
DELIMITERS = {"comma": ",", "tab": "\t", "semicolon": ";"}


def safe_filename(name: str | None, ext: str, default: str = "export") -> str:
    """ダウンロード用のファイル名を整える（末尾に日時、指定の拡張子）。"""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name or "")).strip().strip(".")
    s = re.sub(r"\.(xlsx|csv|txt|md|zip)$", "", s, flags=re.IGNORECASE) or default
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M")
    return f"{s[:80]}_{stamp}.{ext.lstrip('.')}"


def _encode(text: str, encoding: str) -> bytes:
    enc = encoding if encoding in ENCODINGS else DEFAULT_ENCODING
    # Shift_JIS に無い文字（絵文字や一部の漢字）で落ちないよう置換する
    return text.encode(enc, errors="replace" if enc == "cp932" else "strict")


def build_csv(columns: list, rows: list, encoding: str = DEFAULT_ENCODING,
              delimiter: str = "comma") -> bytes:
    """1つの結果セットを CSV のバイト列にする。"""
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=DELIMITERS.get(delimiter, ","),
                   lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    w.writerow([str(c) for c in columns])
    for r in rows:
        w.writerow(["" if v is None else v for v in r])
    return _encode(buf.getvalue(), encoding)


def build_zip(files: list[dict]) -> bytes:
    """[{"filename": str, "data": bytes}, ...] を1つのZIPにまとめる。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        used = set()
        for f in files:
            name = str(f.get("filename") or "file")
            base, i = name, 2
            while name.lower() in used:
                stem, _, ext = base.rpartition(".")
                name = f"{stem}_{i}.{ext}" if stem else f"{base}_{i}"
                i += 1
            used.add(name.lower())
            z.writestr(name, f["data"])
    return buf.getvalue()


def table_to_text(columns: list, rows: list, style: str = "markdown") -> str:
    """結果セットを本文に埋め込めるテキスト表にする。"""
    cols = [str(c) for c in columns]
    body = [["" if v is None else str(v) for v in r] for r in rows]
    if style == "markdown":
        out = ["| " + " | ".join(cols) + " |",
               "| " + " | ".join("---" for _ in cols) + " |"]
        out += ["| " + " | ".join(r) + " |" for r in body]
        return "\n".join(out)
    if style == "tsv":
        return "\n".join(["\t".join(cols)] + ["\t".join(r) for r in body])
    # 等幅（プレーンテキスト用に桁を揃える）
    widths = [max(len(cols[i]), *(len(r[i]) for r in body)) if body else len(cols[i])
              for i in range(len(cols))]
    line = "-+-".join("-" * w for w in widths)
    out = [" | ".join(c.ljust(widths[i]) for i, c in enumerate(cols)), line]
    out += [" | ".join(v.ljust(widths[i]) for i, v in enumerate(r)) for r in body]
    return "\n".join(out)


def build_text(body: str, encoding: str = DEFAULT_ENCODING) -> bytes:
    return _encode(str(body or ""), encoding)


