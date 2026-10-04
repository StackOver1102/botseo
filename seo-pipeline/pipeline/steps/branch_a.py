"""Nhánh A (làm mới bài của chính site) trong luồng full.

Khác nhánh B: bài gốc là của site, không vướng ADR-6, nên outline (P4) và writer (P6-A) được thấy bài gốc:
- P4 nhận khung bài gốc (section O00…On) và phải phủ mọi section cũ (`source_sections`), thêm section cho IG.
- Writer viết lại TỪNG section từ đoạn gốc tương ứng + fact đã kiểm chứng + IG (một lượt cả bài thì model cắt ý).
- Code kiểm tra từng section: dài ≥ bản gốc, câu chữ không trùng quá ngưỡng — lỗi thì viết lại riêng section đó.
"""
from __future__ import annotations

import re

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.M)
_WORD = re.compile(r"\w+", re.U)


def words(text: str) -> int:
    return len(_WORD.findall(text))


def original_sections(md: str | None) -> list[dict]:
    """Bài gốc → [{id: O00 (mở bài, nếu có) / O01…, level, heading, words, text}]; mỗi heading mở một section."""
    if not md or not md.strip():
        return []
    out = []
    matches = list(_HEADING.finditer(md))
    intro = md[:matches[0].start()] if matches else md
    if intro.strip():
        out.append({"id": "O00", "level": 0, "heading": "(mở bài)", "text": intro.strip()})
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(md)
        out.append({"id": f"O{i + 1:02d}", "level": len(m.group(1)), "heading": m.group(2).strip(),
                    "text": md[m.start():end].strip()})
    for s in out:
        s["words"] = words(_HEADING.sub("", s["text"]))
    return out


def outline_brief(sections: list[dict]) -> list[dict]:
    """Khung bài gốc đưa cho P4 (không gồm nội dung)."""
    return [{"id": s["id"], "level": s["level"], "heading": s["heading"], "words": s["words"]} for s in sections]


def cover_original(outline: list[dict], sections: list[dict]) -> tuple[list[dict], list[str]]:
    """Mọi section gốc phải thuộc một section outline. Section gốc bị bỏ sót → gán vào section outline đã nhận section gốc
    đứng ngay trước nó (theo thứ tự bài gốc); không có thì gán vào section đầu. Bỏ mã không tồn tại."""
    valid = [s["id"] for s in sections]
    notes = []
    outline = [{**s, "source_sections": [x for x in s.get("source_sections") or [] if x in valid]} for s in outline]
    owner = {x: i for i, s in enumerate(outline) for x in s["source_sections"]}
    for pos, sid in enumerate(valid):
        if sid in owner:
            continue
        prev = next((owner[valid[j]] for j in range(pos - 1, -1, -1) if valid[j] in owner), None)
        target = prev if prev is not None else 0
        # gán cho H2 (writer viết theo H2): leo lên H2 gần nhất phía trên
        while target > 0 and outline[target]["level"] != 2:
            target -= 1
        outline[target]["source_sections"].append(sid)
        owner[sid] = target
        notes.append(f"{sid} gán vào {outline[target]['section_id']}")
    for s in outline:
        s["source_sections"] = sorted(set(s["source_sections"]), key=valid.index)
    return outline, notes


def section_original(outline: list[dict], idx: int, sections: list[dict]) -> str:
    """Đoạn gốc của H2 outline[idx] và các H3 con của nó (theo source_sections), giữ thứ tự bài gốc."""
    ids = list(outline[idx].get("source_sections") or [])
    for s in outline[idx + 1:]:
        if s["level"] == 2:
            break
        ids += s.get("source_sections") or []
    by_id = {s["id"]: s for s in sections}
    # Heading gốc → nhãn [Mục gốc: …]: để nguyên dòng "## …" thì writer chép lại heading giữa section và viết "tiếp"
    # bằng "..." (lỗi job ac6bf725: S01 gộp mở bài O00 + O01). Heading của section mới do outline quyết định.
    text = "\n\n".join(by_id[i]["text"] for i in sorted(set(ids), key=list(by_id).index))
    return _HEADING.sub(lambda m: f"[Mục gốc: {m.group(2).strip()}]", text)


_LEAD_ELLIPSIS = re.compile(r"^(\s*)(?:\.{3}|…)\s*", re.M)


def dedupe_headings(md: str) -> str:
    """Dọn section writer trả về: bỏ dòng heading trùng chữ với heading đã có trong section, bỏ "..."/"…" ở đầu đoạn
    (writer viết "tiếp" sau heading chép lại). Viết hoa chữ đầu đoạn bị mất chữ hoa vì cắt dấu ba chấm."""
    md = _LEAD_ELLIPSIS.sub(lambda m: m.group(1), md)
    md = re.sub(r"^(\s*)([a-zà-ỹđ])", lambda m: m.group(1) + m.group(2).upper(), md, flags=re.M)
    seen, out = set(), []
    for line in md.split("\n"):
        m = _HEADING.match(line)
        if m:
            key = re.sub(r"\s+", " ", m.group(2)).strip().lower()
            if key in seen:
                continue
            seen.add(key)
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out))
