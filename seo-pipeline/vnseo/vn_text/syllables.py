"""Tách âm tiết (§5.3): token theo khoảng trắng sau khi bỏ dấu câu, số được chuẩn hoá."""
from __future__ import annotations

import regex

from .normalize import normalize

_TOKEN = regex.compile(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+(?:[.,]\d+)?|[\p{L}\p{M}\p{N}_]+")
_THOUSANDS = regex.compile(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?")


def _canon_number(tok: str) -> str:
    if _THOUSANDS.fullmatch(tok):
        tok = tok.replace(".", "")
    return tok.replace(",", ".")


def syllables(text: str) -> list[str]:
    """'Lãi suất tăng 0,5 %' → ['lãi', 'suất', 'tăng', '0.5']."""
    s = normalize(text).lower()
    out = []
    for tok in _TOKEN.findall(s):
        out.append(_canon_number(tok) if tok[0].isdigit() else tok)
    return out


def count_syllables(text: str) -> int:
    return len(syllables(text))
