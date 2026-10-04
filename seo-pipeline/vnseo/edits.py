"""Áp edit find/replace và đo tỷ lệ câu thay đổi (§5.6)."""
from __future__ import annotations

import difflib
import unicodedata
from dataclasses import dataclass, field
from typing import Mapping

from .vn_text.markdown import SECTION_MARKER, SectionIndex
from .vn_text.sentences import sentence_texts

GLOBAL = "GLOBAL"


@dataclass(frozen=True)
class Edit:
    check_id: str
    error_type: int
    find: str
    replace: str
    reason: str = ""


@dataclass
class ApplyResult:
    md: str
    applied: list[Edit] = field(default_factory=list)
    rejected: list[tuple[Edit, str]] = field(default_factory=list)
    ratio: float = 0.0
    batch_rejected: bool = False
    diff: str = ""


def changed_sentence_ratio(old: list[str], new: list[str]) -> float:
    if not old:
        return 0.0 if not new else 1.0
    sm = difflib.SequenceMatcher(None, old, new, autojunk=False)
    changed = sum(max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal")
    return changed / len(old)


def apply_edits(
    md: str,
    edits: list[Edit],
    locations: Mapping[str, str] | None = None,
    max_ratio: float | None = None,
) -> ApplyResult:
    """Áp lần lượt từng edit.

    - `find` phải xuất hiện đúng 1 lần (so sau NFC), không được chạm marker <!-- Sxx -->.
    - `locations` (check_id → section_id | GLOBAL): edit nằm ngoài section của check bị loại (P9).
    - Tỷ lệ câu đổi > max_ratio → loại cả lô, trả lại bài gốc.
    """
    md = unicodedata.normalize("NFC", md)
    current = md
    result = ApplyResult(md=md)
    for e in edits:
        find = unicodedata.normalize("NFC", e.find)
        replace = unicodedata.normalize("NFC", e.replace)
        if not find:
            result.rejected.append((e, "find_empty"))
            continue
        count = current.count(find)
        if count != 1:
            result.rejected.append((e, f"find_count={count}"))
            continue
        if SECTION_MARKER.search(find) or SECTION_MARKER.search(replace):
            result.rejected.append((e, "touches_section_marker"))
            continue
        pos = current.index(find)
        if locations is not None:
            loc = locations.get(e.check_id)
            if loc is None:
                result.rejected.append((e, "unknown_check_id"))
                continue
            section = SectionIndex(current).at(pos)
            if loc != GLOBAL and section != loc:
                result.rejected.append((e, f"outside_location={section}!={loc}"))
                continue
        current = current[:pos] + replace + current[pos + len(find):]
        result.applied.append(e)

    result.ratio = changed_sentence_ratio(sentence_texts(md), sentence_texts(current))
    if max_ratio is not None and result.ratio > max_ratio:
        result.batch_rejected = True
        return result
    result.md = current
    result.diff = "".join(
        difflib.unified_diff(md.splitlines(keepends=True), current.splitlines(keepends=True), "before", "after")
    )
    return result
