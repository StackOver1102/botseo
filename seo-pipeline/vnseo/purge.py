"""Xoá khỏi bài các câu / dòng bảng còn chứa dữ kiện không được giữ (số chưa kiểm chứng sau fact-check).

Chạy bằng code sau lượt sửa của model (P9): model xoá sót, hoặc cả lô edit bị từ chối vì vượt ngưỡng, thì bài vẫn không
còn số liệu chưa kiểm chứng. Nguyên tắc: xoá trọn một đơn vị đọc được (một câu, một bullet, một dòng bảng), không cắt
giữa câu; bảng không còn dòng dữ liệu thì bỏ cả bảng và câu dẫn kết thúc bằng dấu hai chấm ngay trước nó.
Heading không bị xoá (xoá heading làm lệch outline) — số nằm trong heading để bước kiểm tra sau báo.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from .vn_text.sentences import split_sentences

_TABLE_SEPARATOR = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


@dataclass
class PurgeResult:
    md: str
    removed: list[str] = field(default_factory=list)  # nội dung đã xoá, để ghi nhật ký


def _row_key(text: str) -> str:
    """Dòng bảng và claim trích từ bảng so khớp theo nội dung ô, bỏ dấu | và khoảng trắng thừa."""
    return re.sub(r"\s+", " ", text.replace("|", " ")).strip().lower()


def _lines(md: str) -> list[tuple[int, int, str]]:
    """(start, end không gồm \\n, text) của từng dòng."""
    out, offset = [], 0
    for line in md.split("\n"):
        out.append((offset, offset + len(line), line))
        offset += len(line) + 1
    return out


def purge_unverified(md: str, bad_spans: Iterable[tuple[int, int]], removed_rows: Iterable[str] = ()) -> PurgeResult:
    """bad_spans: vị trí (start, end) trong `md` của các số không được giữ. removed_rows: verbatim của claim bị xoá —
    dòng bảng có nội dung trùng một claim như vậy cũng bị xoá (dòng bảng chỉ có số thường, vd. "La Liga | 12,8")."""
    bad = sorted(bad_spans)
    removed_keys = {_row_key(r) for r in removed_rows if "|" in r}

    def hit(start: int, end: int) -> bool:
        return any(s < end and e > start for s, e in bad)

    lines = _lines(md)
    cuts: list[tuple[int, int]] = []
    removed: list[str] = []
    sentences = split_sentences(md)

    def cut_line(i: int) -> None:
        start, end, _ = lines[i]
        cuts.append((start, min(end + 1, len(md))))

    # Câu và bullet chứa số không được giữ
    starts = [ln[0] for ln in lines]

    def line_index(pos: int) -> int:
        lo, hi = 0, len(starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if starts[mid] <= pos:
                lo = mid
            else:
                hi = mid - 1
        return lo

    for s in sentences:
        if s.kind == "heading" or not hit(s.start, s.end):
            continue
        removed.append(s.text)
        if s.kind == "bullet":
            cut_line(line_index(s.start))
        else:
            end = s.end
            while end < len(md) and md[end] in " \t":  # nuốt khoảng trắng sau câu để không để lại hai dấu cách
                end += 1
            cuts.append((s.start, end))

    # Bảng: xoá dòng dữ liệu chứa số không được giữ hoặc trùng claim bị xoá; bảng hết dòng dữ liệu thì bỏ cả bảng
    i = 0
    while i < len(lines):
        if not lines[i][2].lstrip().startswith("|"):
            i += 1
            continue
        block = []
        while i < len(lines) and lines[i][2].lstrip().startswith("|"):
            block.append(i)
            i += 1
        has_header = len(block) >= 2 and bool(_TABLE_SEPARATOR.match(lines[block[1]][2].strip()))
        data = block[2:] if has_header else block
        dropped = [j for j in data if hit(lines[j][0], lines[j][1]) or _row_key(lines[j][2]) in removed_keys]
        empty = has_header and not data  # bảng đã chỉ còn tiêu đề (model xoá hết dòng từ trước)
        if not dropped and not empty:
            continue
        removed += [lines[j][2].strip() for j in dropped]
        if has_header and len(dropped) == len(data):
            for j in block:
                cut_line(j)
            removed.append("(bảng không còn dòng dữ liệu: bỏ cả bảng) " + lines[block[0]][2].strip())
            # Câu dẫn kết thúc bằng ":" ngay trước bảng ("…bức tranh trở nên rõ ràng hơn:") mất nghĩa khi bảng bị bỏ
            k = block[0] - 1
            while k >= 0 and not lines[k][2].strip():
                k -= 1
            if k >= 0 and lines[k][2].rstrip().endswith(":"):
                lead = [s for s in sentences if lines[k][0] <= s.start < lines[k][1] and s.kind == "text"]
                if lead:
                    removed.append(lead[-1].text)
                    cuts.append((lead[-1].start, lines[k][1]))
        else:
            for j in dropped:
                cut_line(j)

    if not cuts:
        return PurgeResult(md)
    out, pos = [], 0
    for start, end in sorted(cuts):
        if start < pos:  # vùng chồng nhau (câu dẫn vừa có số vừa đứng trước bảng bị bỏ)
            pos = max(pos, end)
            continue
        out.append(md[pos:start])
        pos = end
    out.append(md[pos:])
    text = "".join(out)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text)
    return PurgeResult(text, removed)
