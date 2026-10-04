"""Tiện ích Markdown dùng chung: che comment/code block, định vị section theo marker <!-- Sxx -->."""
from __future__ import annotations

import bisect
import re

SECTION_MARKER = re.compile(r"<!--\s*(S\d+)\s*-->")
INTRO_SECTION = "S00"  # đoạn trước H2 đầu tiên
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_CODE_FENCE = re.compile(r"^(```|~~~).*?^\1[^\n]*$", re.S | re.M)


def _blank(m: re.Match) -> str:
    # Giữ nguyên độ dài và xuống dòng để offset không đổi
    return re.sub(r"[^\n]", " ", m.group(0))


def mask_non_content(md: str) -> str:
    """Thay comment HTML và code block bằng khoảng trắng cùng độ dài."""
    md = _CODE_FENCE.sub(_blank, md)
    return _HTML_COMMENT.sub(_blank, md)


class SectionIndex:
    """Tra section_id theo offset ký tự trong bài."""

    def __init__(self, md: str):
        self._starts: list[int] = []
        self._ids: list[str] = []
        for m in SECTION_MARKER.finditer(md):
            self._starts.append(m.start())
            self._ids.append(m.group(1))

    def at(self, offset: int) -> str:
        i = bisect.bisect_right(self._starts, offset) - 1
        return self._ids[i] if i >= 0 else INTRO_SECTION

    @property
    def ids(self) -> list[str]:
        return list(self._ids)


def split_sections(md: str) -> dict[str, str]:
    """Tách bài thành {section_id: text}; phần trước marker đầu tiên là S00."""
    out: dict[str, str] = {}
    matches = list(SECTION_MARKER.finditer(md))
    first = matches[0].start() if matches else len(md)
    if md[:first].strip():
        out[INTRO_SECTION] = md[:first]
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(md)
        out[m.group(1)] = md[m.end():end]
    return out


def strip_section_markers(md: str) -> str:
    return re.sub(r"[ \t]*<!--\s*S\d+\s*-->[ \t]*\n?", "", md)
