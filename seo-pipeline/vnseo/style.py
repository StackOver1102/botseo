"""Quét cụm sáo rỗng và đo nhịp câu (§5.10)."""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from typing import Literal

from .vn_text.markdown import mask_non_content
from .vn_text.normalize import normalize
from .vn_text.sentences import split_sentences
from .vn_text.syllables import count_syllables

Level = Literal["high", "medium"]
DEFAULT_MAX = {"high": 0, "medium": 2}

_BLOCKQUOTE = re.compile(r"^[ \t]*>.*$", re.M)
_QUOTED = re.compile(r"“[^”\n]*”|\"[^\"\n]*\"")


@dataclass(frozen=True)
class StyleHit:
    level: Level
    rule: str
    text: str
    start: int
    context: str


def _blank(m: re.Match) -> str:
    return " " * len(m.group(0))


def _scannable(md: str, whitelist: list[str]) -> str:
    """Che comment, code, trích dẫn và whitelist bằng khoảng trắng (giữ offset)."""
    text = mask_non_content(md)
    text = _BLOCKQUOTE.sub(_blank, text)
    text = _QUOTED.sub(_blank, text)
    for w in whitelist:
        text = re.sub(re.escape(w), lambda m: " " * len(m.group(0)), text, flags=re.I)
    return text


def _context(md: str, start: int, end: int, width: int = 40) -> str:
    return md[max(0, start - width):end + width].replace("\n", " ")


def _phrase_re(phrase: str) -> re.Pattern:
    # Bài đã normalize nên cụm trong config cũng phải normalize (kiểu bỏ dấu có thể khác)
    return re.compile(rf"(?<!\w){re.escape(normalize(phrase))}(?!\w)", re.I)


def _collect(md: str, text: str, level: Level, rule: str, pattern: re.Pattern, max_per_article: int) -> list[StyleHit]:
    found = list(pattern.finditer(text))
    if len(found) <= max_per_article:
        return []
    return [StyleHit(level, rule, md[m.start():m.end()], m.start(), _context(md, m.start(), m.end())) for m in found]


def blacklist_scan(md: str, cfg: dict) -> list[StyleHit]:
    """cfg = config['blacklist']. Một cụm bị tính là hit khi số lần xuất hiện > max_per_article."""
    md = normalize(md, keep_newlines=True)
    text = _scannable(md, cfg.get("whitelist", []))
    hits: list[StyleHit] = []
    for level in ("high", "medium"):
        for phrase in cfg.get(level, []):
            hits += _collect(md, text, level, phrase, _phrase_re(phrase), DEFAULT_MAX[level])
        for item in cfg.get(f"{level}_patterns", []):
            if isinstance(item, str):
                item = {"re": item}
            limit = item.get("max_per_article", DEFAULT_MAX[level])
            hits += _collect(md, text, level, item["re"], re.compile(item["re"], re.I), limit)

    openers = cfg.get("medium_sentence_openers")
    if openers:
        words = openers["words"]
        limit = openers.get("max_per_article", DEFAULT_MAX["medium"])
        starts = [
            s for s in split_sentences(md)
            if s.kind == "text" and text[s.start:s.end].strip() and any(
                re.match(rf"{re.escape(w)}(?!\w)", s.text, re.I) for w in words
            )
        ]
        if len(starts) > limit:
            hits += [StyleHit("medium", "sentence_opener", s.text[:40], s.start, _context(md, s.start, s.start)) for s in starts]
    hits.sort(key=lambda h: h.start)
    return hits


@dataclass
class SentenceStats:
    count: int
    mean: float
    stdev: float
    cv: float
    long_share: float


def sentence_stats(md: str, max_syllables: int = 45) -> SentenceStats:
    """Chỉ tính câu văn thường (bỏ heading, bullet). CV = độ lệch chuẩn / trung bình số âm tiết."""
    lengths = [count_syllables(s.text) for s in split_sentences(md) if s.kind == "text"]
    lengths = [n for n in lengths if n > 0]
    if not lengths:
        return SentenceStats(0, 0.0, 0.0, 0.0, 0.0)
    mean = statistics.fmean(lengths)
    stdev = statistics.pstdev(lengths)
    long_share = sum(1 for n in lengths if n > max_syllables) / len(lengths)
    return SentenceStats(len(lengths), mean, stdev, stdev / mean if mean else 0.0, long_share)
