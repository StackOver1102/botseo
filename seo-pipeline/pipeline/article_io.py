"""Chuẩn hoá bài nhánh A dán từ CMS/Word: thường là văn bản thường, heading không có dấu #, có thể không có tiêu đề."""
from __future__ import annotations

import re

_BULLET = re.compile(r"^\s*([-*•+]|\d+[.)])\s+")
_END_PUNCT = tuple(".!?:;,…\"”)")


def _short(line: str) -> bool:
    s = line.strip()
    return bool(s) and len(s) <= 120 and len(s.split()) <= 20 and not s.endswith(_END_PUNCT) and not _BULLET.match(s)


def normalize_article(text: str) -> str:
    """Giữ nguyên nếu đã là Markdown có heading; không thì đoán heading và thêm '## '. Chuẩn hoá xuống dòng."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    lines = text.split("\n")
    if any(re.match(r"^#{1,6}\s", ln) for ln in lines):
        return text
    nonempty = [i for i, ln in enumerate(lines) if ln.strip()]
    out = list(lines)
    # Duyệt từ cuối lên: heading = dòng ngắn không kết thúc bằng dấu câu, theo sau là đoạn văn dài hơn hoặc một heading
    # khác (vd. "Mục tiêu - Tầm nhìn - Sứ mệnh" rồi tới "Mục tiêu")
    heading_after = False
    for pos in range(len(nonempty) - 1, -1, -1):
        i = nonempty[pos]
        nxt = lines[nonempty[pos + 1]] if pos + 1 < len(nonempty) else None
        is_h = _short(lines[i]) and nxt is not None and (len(nxt.strip()) > len(lines[i].strip()) or heading_after)
        if is_h:
            out[i] = f"## {lines[i].strip()}"
        heading_after = is_h
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out))


def first_heading(md: str) -> str | None:
    m = re.search(r"^#{1,6}\s+(.+)$", md, re.M)
    return m.group(1).strip() if m else None


class FetchError(Exception):
    pass


def fetch_article(url: str, http=None) -> tuple[str | None, str]:
    """Tải bài cũ từ link: (tiêu đề H1 nếu có, nội dung Markdown không kèm H1). Lỗi → FetchError có lý do."""
    from .urlcheck import UrlVerifier
    status, md = UrlVerifier(http).article_markdown(url.strip())
    if status is None:
        raise FetchError("không tải được link (sai địa chỉ hoặc lỗi mạng)")
    if status != 200:
        raise FetchError(f"trang trả HTTP {status}")
    if len(md.split()) < 80:
        raise FetchError("không trích được nội dung bài (trang cần JavaScript, bị chặn, hoặc không phải trang bài viết) — "
                         "hãy dán nội dung bài thay cho link")
    title = None
    m = re.match(r"^#\s+(.+?)\s*\n", md)
    if m:
        title, md = m.group(1).strip(), md[m.end():].strip()
    return title, md
