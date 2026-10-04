"""Đọc danh sách keyword từ nhiều định dạng: export Ahrefs (UTF-16, tab), CSV `keyword,volume`, hoặc mỗi dòng một keyword."""
from __future__ import annotations

import csv
import io


def decode(data: bytes) -> str:
    """Ahrefs xuất UTF-16 có BOM; file tự làm thường UTF-8."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("utf-16")


def _num(v: str | None) -> int | None:
    v = (v or "").strip().replace(",", "").replace(".", "")
    return int(v) if v.isdigit() else None


def parse_keywords(text: str, limit: int = 100) -> list[dict]:
    """[{keyword, volume}] theo volume giảm dần, bỏ trùng, tối đa `limit` (P1 chạy trên web: 300 dòng làm prompt quá dài).
    Có header Keyword/Volume thì đọc theo cột; hỗ trợ tab / dấu phẩy / chấm phẩy và ô trong ngoặc kép (Ahrefs, Semrush)."""
    text = text.strip().lstrip("\ufeff")
    if not text:
        return []
    first = text.splitlines()[0]
    delim = "\t" if "\t" in first else ";" if first.count(";") > first.count(",") else ","
    reader = list(csv.reader(io.StringIO(text), delimiter=delim))
    head = [h.strip().strip('"').lower() for h in reader[0]]
    rows: list[dict] = []
    if "keyword" in head:
        ki, vi = head.index("keyword"), head.index("volume") if "volume" in head else None
        for r in reader[1:]:
            if len(r) > ki and (kw := r[ki].strip()):
                rows.append({"keyword": kw, "volume": _num(r[vi]) if vi is not None and len(r) > vi else None})
    else:
        for r in reader:
            if r and r[0].strip():
                rows.append({"keyword": r[0].strip(), "volume": _num(r[1]) if len(r) > 1 else None})
    # dòng rác (số thứ tự, ký hiệu) không phải keyword
    rows = [r for r in rows if any(ch.isalpha() for ch in r["keyword"])]
    seen, out = set(), []
    for r in sorted(rows, key=lambda r: -(r["volume"] or 0)):
        if r["keyword"].lower() not in seen:
            seen.add(r["keyword"].lower())
            out.append(r)
    return out[:limit]
