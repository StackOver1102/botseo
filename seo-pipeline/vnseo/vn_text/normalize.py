"""Chuẩn hoá văn bản tiếng Việt (§5.1)."""
from __future__ import annotations

import re
import unicodedata
from typing import Literal

ToneStyle = Literal["old", "new"]

_INVISIBLE = re.compile("[​‌‍﻿­]")
_SPACES = re.compile(r"[ \t\f\v ]+")
_ALL_WS = re.compile(r"\s+")
_WORD = re.compile(r"\w+")

# Dấu thanh ở dạng combining (NFD)
_TONES = {"̀", "́", "̉", "̃", "̣"}
# Vần có hai kiểu bỏ dấu khi đứng cuối từ (không có phụ âm cuối)
_AMBIGUOUS_RHYMES = ("oa", "oe", "uy")


def _retone_word(word: str, style: ToneStyle) -> str:
    decomposed = unicodedata.normalize("NFD", word)
    tones = [c for c in decomposed if c in _TONES]
    if len(tones) != 1:
        return word
    base = unicodedata.normalize("NFC", "".join(c for c in decomposed if c not in _TONES))
    low = base.lower()
    for rhyme in _AMBIGUOUS_RHYMES:
        if not low.endswith(rhyme):
            continue
        # "quy": u thuộc phụ âm đầu qu, dấu luôn ở y
        if rhyme == "uy" and low[:-2].endswith("q"):
            return word
        idx = len(base) - 2 if style == "old" else len(base) - 1
        chars = list(base)
        chars[idx] = unicodedata.normalize("NFC", chars[idx] + tones[0])
        return "".join(chars)
    return word


def normalize_tone(s: str, style: ToneStyle = "new") -> str:
    """Đặt dấu thanh của các vần oa/oe/uy cuối từ về một kiểu: old = hòa/thủy, new = hoà/thuỷ."""
    return _WORD.sub(lambda m: _retone_word(m.group(0), style), s)


def normalize(s: str, tone_style: ToneStyle = "new", keep_newlines: bool = False) -> str:
    """NFC + bỏ ký tự vô hình + thống nhất kiểu bỏ dấu + gộp khoảng trắng.

    keep_newlines=True dùng cho Markdown: chỉ gộp khoảng trắng trong từng dòng, giữ cấu trúc dòng.
    """
    s = _INVISIBLE.sub("", s)
    s = unicodedata.normalize("NFC", s)
    s = normalize_tone(s, tone_style)
    s = unicodedata.normalize("NFC", s)
    if keep_newlines:
        lines = [_SPACES.sub(" ", line).strip() for line in s.replace("\r\n", "\n").split("\n")]
        return "\n".join(lines).strip()
    return _ALL_WS.sub(" ", s).strip()


def strip_diacritics(s: str) -> str:
    """Khoá không dấu (key_khongdau): bỏ dấu, đ→d, lowercase."""
    decomposed = unicodedata.normalize("NFD", s)
    out = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
    out = out.replace("đ", "d").replace("Đ", "D")
    return unicodedata.normalize("NFC", out).lower()


# Chữ chỉ có trong tiếng Việt: dùng để đoán decode cp1258 có ra văn bản Việt thật không
_VIET_MARKERS = set("ơưđăƠƯĐĂ") | _TONES


def detect_legacy_encoding(raw: bytes) -> Literal["utf8", "cp1258", "vni", "tcvn3", "unknown"]:
    """Nhận diện encoding. VNI/TCVN3 chưa hỗ trợ [CXM]; chúng cũng decode được bằng cp1258
    nhưng ra chữ rác, nên decode() gắn cờ needs_review khi không thấy chữ đặc trưng tiếng Việt."""
    try:
        raw.decode("utf-8")
        return "utf8"
    except UnicodeDecodeError:
        pass
    try:
        raw.decode("cp1258")
        return "cp1258"
    except UnicodeDecodeError:
        return "unknown"


def decode(raw: bytes) -> tuple[str, str, bool]:
    """Trả (text, encoding, needs_review)."""
    enc = detect_legacy_encoding(raw)
    if enc == "utf8":
        return raw.decode("utf-8"), enc, False
    if enc == "cp1258":
        text = raw.decode("cp1258")
        looks_viet = any(c in _VIET_MARKERS for c in text)  # xét text thô: NFD sẽ tách cả é của tiếng Pháp
        return unicodedata.normalize("NFC", text), enc, not looks_viet
    return raw.decode("latin-1"), enc, True
