"""Đo trùng lặp bằng shingle containment (§5.4).

containment = |S(new) ∩ S(src)| / |S(new)|, S là tập n-gram âm tiết.
Nhánh B chỉ giữ tập hash 5-gram của nguồn (text nguồn bị xoá sau FACT_GATE),
nên các hàm *_hashes là đường chính; containment() trên text chỉ dùng khi còn text nguồn.
"""
from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field

from .vn_text.markdown import mask_non_content, split_sections
from .vn_text.syllables import syllables


def _hash(gram: tuple[str, ...]) -> int:
    digest = hashlib.blake2b("\x1f".join(gram).encode("utf-8"), digest_size=8).digest()
    return struct.unpack("<Q", digest)[0]


def ngrams(tokens: list[str], n: int) -> list[tuple[str, ...]]:
    return [tuple(tokens[i:i + n]) for i in range(len(tokens) - n + 1)]


def shingle_hashes(text: str, n: int = 5) -> set[int]:
    return {_hash(g) for g in ngrams(syllables(mask_non_content(text)), n)}


def pack_hashes(hashes: set[int]) -> bytes:
    """Lưu vào source_fingerprints.shingle_hashes (bytea)."""
    return b"".join(struct.pack("<Q", h) for h in sorted(hashes))


def unpack_hashes(blob: bytes) -> set[int]:
    return {struct.unpack_from("<Q", blob, i)[0] for i in range(0, len(blob), 8)}


@dataclass
class OverlapReport:
    ratio: float
    n: int
    matched: int
    total: int
    top_passages: list[str] = field(default_factory=list)
    by_section: dict[str, float] = field(default_factory=dict)


def _passages(tokens: list[str], hit: list[bool], n: int, limit: int) -> list[str]:
    """Ghép các n-gram trùng liên tiếp thành đoạn, lấy `limit` đoạn dài nhất."""
    runs: list[tuple[int, int]] = []
    i = 0
    while i < len(hit):
        if hit[i]:
            j = i
            while j + 1 < len(hit) and hit[j + 1]:
                j += 1
            runs.append((i, j + n))
            i = j + 1
        else:
            i += 1
    runs.sort(key=lambda r: r[1] - r[0], reverse=True)
    return [" ".join(tokens[s:e]) for s, e in runs[:limit]]


def _ratio(tokens: list[str], source: set[int], n: int) -> tuple[int, int, list[bool]]:
    grams = ngrams(tokens, n)
    hit = [_hash(g) in source for g in grams]
    unique = {g for g in grams}
    matched = sum(1 for g in unique if _hash(g) in source)
    return matched, len(unique), hit


def containment_hashes(new_md: str, source_hashes: set[int], n: int = 5, top: int = 20) -> OverlapReport:
    """So bài mới với tập hash nguồn. Bài ngắn hơn n âm tiết hoặc rỗng → 0."""
    tokens = syllables(mask_non_content(new_md))
    matched, total, hit = _ratio(tokens, source_hashes, n)
    ratio = matched / total if total else 0.0
    by_section = {}
    for sid, text in split_sections(new_md).items():
        m, t, _ = _ratio(syllables(mask_non_content(text)), source_hashes, n)
        by_section[sid] = m / t if t else 0.0
    return OverlapReport(ratio, n, matched, total, _passages(tokens, hit, n, top), by_section)


def containment(new: str, source: str, n: int = 5, top: int = 20) -> OverlapReport:
    """Bản có text nguồn. Văn bản mới ít hơn n âm tiết thì dùng n = số âm tiết đó."""
    new_tokens = syllables(mask_non_content(new))
    if not new_tokens:
        return OverlapReport(0.0, n, 0, 0)
    n_eff = min(n, len(new_tokens))
    return containment_hashes(new, shingle_hashes(source, n_eff), n_eff, top)
