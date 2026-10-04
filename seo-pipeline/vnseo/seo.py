"""Đếm ký tự title/meta, ước lượng pixel, slug (§5.5)."""
from __future__ import annotations

import re
import unicodedata

import regex

from .vn_text.normalize import strip_diacritics

# Độ rộng xấp xỉ (px) của Arial 20px — heuristic, chỉ để cảnh báo
_NARROW = {c: 6 for c in "ijlt.,:;!|'\"()[]f "}
_NARROW.update({"r": 7, "I": 6, "J": 10})
_WIDE = {"m": 17, "w": 15, "M": 17, "W": 19}
_DEFAULT_LOWER = 11
_DEFAULT_UPPER = 13
_DEFAULT_DIGIT = 11
_DEFAULT_OTHER = 11


def graphemes(s: str) -> list[str]:
    return regex.findall(r"\X", unicodedata.normalize("NFC", s))


def char_count(s: str) -> int:
    """Đếm theo grapheme cluster trên chuỗi NFC."""
    return len(graphemes(s))


def _base_char(g: str) -> str:
    c = unicodedata.normalize("NFD", g)[0]
    return {"đ": "d", "Đ": "D"}.get(c, c)


def pixel_width(s: str) -> int:
    """Ước lượng độ rộng hiển thị; chữ có dấu tính theo chữ gốc."""
    total = 0
    for g in graphemes(s):
        c = _base_char(g)
        if c in _WIDE:
            total += _WIDE[c]
        elif c in _NARROW:
            total += _NARROW[c]
        elif c.isdigit():
            total += _DEFAULT_DIGIT
        elif c.isupper():
            total += _DEFAULT_UPPER
        elif c.isalpha():
            total += _DEFAULT_LOWER
        else:
            total += _DEFAULT_OTHER
    return total


_SLUG_OK = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def slugify(s: str, max_words: int = 6) -> str:
    words = re.findall(r"[a-z0-9]+", strip_diacritics(s))
    return "-".join(words[:max_words])


def slug_is_valid(slug: str, max_words: int = 6) -> bool:
    return bool(_SLUG_OK.match(slug)) and len(slug.split("-")) <= max_words
