"""Tách câu tiếng Việt trên Markdown (§5.2)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

import regex

from .markdown import SectionIndex, mask_non_content

# Không dùng khoảng À-Ỹ vì U+00C0–U+1EF8 chứa cả chữ thường có dấu
_BOUNDARY = regex.compile(r"(?<=[.!?…])\s+(?=[\p{Lu}\"“(])")
_ABBREVIATIONS = ("TP.", "TS.", "ThS.", "PGS.", "GS.", "BS.", "v.v.", "St.", "Q.", "P.")
_HEADING = re.compile(r"^#{1,6}\s+")
_BULLET = re.compile(r"^(?:[-*+]|\d+[.)])\s+")

SentenceKind = Literal["heading", "bullet", "text"]


@dataclass(frozen=True)
class Sentence:
    id: int
    section_id: str
    text: str
    start: int
    end: int
    kind: SentenceKind


def _split_line(line: str) -> list[tuple[int, int]]:
    """Trả các khoảng (start, end) của câu trong một dòng."""
    spans: list[tuple[int, int]] = []
    start = 0
    for m in _BOUNDARY.finditer(line):
        before = line[start:m.start()]
        if before.endswith(_ABBREVIATIONS):
            continue
        spans.append((start, m.start()))
        start = m.end()
    spans.append((start, len(line)))
    return spans


def split_sentences(md: str) -> list[Sentence]:
    """Heading và từng mục bullet tính là một câu; bỏ qua code block, comment HTML, bảng."""
    masked = mask_non_content(md)
    sections = SectionIndex(md)
    out: list[Sentence] = []
    offset = 0
    for line in masked.split("\n"):
        line_start = offset
        offset += len(line) + 1
        stripped = line.strip()
        if not stripped or stripped.startswith("|"):
            continue
        lead = len(line) - len(line.lstrip())
        if _HEADING.match(stripped):
            kind: SentenceKind = "heading"
            spans = [(lead, len(line.rstrip()))]
        elif _BULLET.match(stripped):
            kind = "bullet"
            spans = [(lead, len(line.rstrip()))]
        else:
            kind = "text"
            spans = _split_line(line)
        for s, e in spans:
            segment = line[s:e]
            text = segment.strip()
            if not text:
                continue
            abs_start = line_start + s + len(segment) - len(segment.lstrip())
            out.append(Sentence(len(out), sections.at(abs_start), text, abs_start, abs_start + len(text), kind))
    return out


def sentence_texts(md: str) -> list[str]:
    return [s.text for s in split_sentences(md)]
