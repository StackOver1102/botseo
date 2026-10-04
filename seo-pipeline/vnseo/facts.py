"""Quét số liệu trong bài và đối chiếu với bảng fact đã duyệt (§5.7)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable, Literal

from .vn_text.markdown import SectionIndex, mask_non_content

# Thứ tự nhánh: 1.000.000(,5) kiểu Việt | 1,000,000 kiểu Anh (≥2 nhóm nên không lẫn với thập phân "1,000")
# | 2.5 thập phân dấu chấm (1–2 chữ số lẻ; 3 chữ số lẻ là phân nghìn) | 2,5 hoặc số nguyên
CORE = r"\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d{1,3}(?:,\d{3}){2,}|\d+\.\d{1,2}(?!\d)|\d+(?:,\d+)?"
L, R = r"(?<![\w.,])", r"(?![\w])"
NUM = rf"{L}(?:{CORE}){R}"
PCT = rf"{L}(?:{CORE})\s?%"
# Đơn vị không phân biệt hoa/thường ("50K", "5 Tỷ"). Từ ghép mở đầu bằng âm tiết đơn vị không phải tiền:
# "5 triệu chứng", "2 đồng hồ", "3 đồng nghiệp", "2 tỉ lệ" — loại bằng lookahead theo từng đơn vị.
_NOT_DONG = r"(?!\s+(?:hồ|nghiệp|ý|thời|bộ|đội|phục|minh|hành|bằng|tình|loạt|thuận|chí|nghĩa|môn|hương|bào|tác)(?!\w))"
_NOT_TY = r"(?!\s+(?:lệ|số|giá|trọng|phú|suất)(?!\w))"
UNIT = rf"(?i:đồng{_NOT_DONG}|đ|VNĐ|VND|nghìn|ngàn|k|triệu(?!\s+chứng(?!\w))|tr|tỷ{_NOT_TY}|tỉ{_NOT_TY}|USD)"
CUR = rf"{L}(?:{CORE})\s?{UNIT}{R}|\$\s?(?:{CORE}){R}"
DATE = (
    r"\b(?:0?[1-9]|[12]\d|3[01])[/-](?:0?[1-9]|1[0-2])[/-](?:19|20)\d{2}\b"
    r"|\bngày\s+\d{1,2}\s+tháng\s+\d{1,2}\s+năm\s+(?:19|20)\d{2}\b"
)
YEAR = r"\b(?:năm\s)?(?:19|20)\d{2}\b"

Kind = Literal["DATE", "CUR", "PCT", "YEAR", "NUM"]
# Thứ tự quét: đoạn đã khớp ở pattern trước không quét lại
_ORDER: list[tuple[Kind, re.Pattern]] = [
    ("DATE", re.compile(DATE)),
    ("CUR", re.compile(CUR)),
    ("PCT", re.compile(PCT)),
    ("YEAR", re.compile(YEAR)),
    ("NUM", re.compile(NUM)),
]
# Số chưa khớp thuộc các loại này → code override P8d.1 = NO
OVERRIDE_KINDS = {"DATE", "CUR", "PCT"}

_MULTIPLIER = {
    "nghìn": 10**3, "ngàn": 10**3, "k": 10**3,
    "triệu": 10**6, "tr": 10**6,
    "tỷ": 10**9, "tỉ": 10**9,
}
_CORE_RE = re.compile(CORE)
_DATE_NUMERIC = re.compile(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})")
_DATE_WORDS = re.compile(r"ngày\s+(\d{1,2})\s+tháng\s+(\d{1,2})\s+năm\s+(\d{4})")


@dataclass(frozen=True)
class NumberHit:
    kind: Kind
    raw: str
    key: str  # giá trị chuẩn hoá để so khớp: "num:3000000000" hoặc "date:2026-09-15"
    start: int
    end: int
    section_id: str


_EN_THOUSANDS = re.compile(r"\d{1,3}(?:,\d{3}){2,}")
_DOT_DECIMAL = re.compile(r"\d+\.\d{1,2}")


def parse_core(s: str) -> Decimal:
    """'1.000.000' → 1000000; '2,5' → 2.5 (dấu . phân nghìn, dấu , thập phân).
    Kiểu Anh: '1,000,000' → 1000000; '2.5' → 2.5 (1–2 chữ số sau dấu chấm)."""
    if _EN_THOUSANDS.fullmatch(s):
        return Decimal(s.replace(",", ""))
    if _DOT_DECIMAL.fullmatch(s):
        return Decimal(s)
    return Decimal(s.replace(".", "").replace(",", "."))


def _num_key(value: Decimal) -> str:
    return "num:" + format(value.normalize(), "f")


def _value_key(kind: Kind, raw: str) -> str:
    if kind == "DATE":
        m = _DATE_NUMERIC.search(raw) or _DATE_WORDS.search(raw)
        d, mo, y = (int(x) for x in m.groups())
        return f"date:{y:04d}-{mo:02d}-{d:02d}"
    core = _CORE_RE.search(raw).group(0)
    value = parse_core(core)
    if kind == "CUR":
        unit = raw[raw.index(core) + len(core):].strip().lower()
        value *= _MULTIPLIER.get(unit, 1)
    return _num_key(value)


def _spans(patterns: Iterable[str], text: str) -> list[tuple[int, int]]:
    out = []
    for p in patterns:
        for m in re.finditer(p, text, re.M):
            out.append(m.span())
    return out


def _literal_spans(literals: Iterable[str], text: str) -> list[tuple[int, int]]:
    out = []
    low = text.lower()
    for lit in literals:
        if not lit:
            continue
        for m in re.finditer(re.escape(lit.lower()), low):
            out.append(m.span())
    return out


def scan_numbers(
    md: str,
    ignore_patterns: Iterable[str] = (),
    ignore_literals: Iterable[str] = (),
    ignore_years: Iterable[int] = (),
) -> list[NumberHit]:
    """Quét DATE → CUR → PCT → YEAR → NUM trên bài (bỏ comment HTML và code block).

    ignore_patterns: regex số thứ tự/"Bước 2"/"top 10" (config.number_ignore_patterns).
    ignore_literals: keyword và heading của outline — số nằm trong đó không phải dữ kiện.
    ignore_years: năm hiện tại, năm trong title.
    """
    text = mask_non_content(md)
    sections = SectionIndex(md)
    ignored = _spans(ignore_patterns, text) + _literal_spans(ignore_literals, text)
    years = {str(y) for y in ignore_years}
    taken = [False] * len(text)
    hits: list[NumberHit] = []
    for kind, pattern in _ORDER:
        for m in pattern.finditer(text):
            s, e = m.span()
            if any(taken[s:e]):
                continue
            for i in range(s, e):
                taken[i] = True
            if any(a <= s and e <= b for a, b in ignored):
                continue
            raw = m.group(0)
            if kind == "YEAR" and raw[-4:] in years:
                continue
            hits.append(NumberHit(kind, raw, _value_key(kind, raw), s, e, sections.at(s)))
    hits.sort(key=lambda h: h.start)
    return hits


def claim_value_keys(text: str) -> set[str]:
    """Các giá trị số/ngày trong một chuỗi claim (new_value hoặc verbatim)."""
    return {h.key for h in scan_numbers(text)}


def approved_value_keys(claims: Iterable[dict]) -> set[str]:
    """Claim DA_DUYET, không BO. Có new_value thì chỉ dùng new_value (giá trị cũ không còn hợp lệ)."""
    keys: set[str] = set()
    for c in claims:
        if c.get("status") != "DA_DUYET" or c.get("action") == "BO":
            continue
        source = c.get("new_value") or c.get("verbatim") or ""
        keys |= claim_value_keys(source)
    return keys


@dataclass
class NumberMatchReport:
    matched: list[NumberHit] = field(default_factory=list)
    unmatched: list[NumberHit] = field(default_factory=list)

    @property
    def override_fail(self) -> bool:
        """Còn số %, tiền hoặc ngày chưa khớp claim nào → P8d.1 = NO bất kể LLM chấm gì."""
        return any(h.kind in OVERRIDE_KINDS for h in self.unmatched)

    def to_json(self) -> dict:
        def row(h: NumberHit) -> dict:
            return {"kind": h.kind, "raw": h.raw, "key": h.key, "section_id": h.section_id}
        return {
            "matched": [row(h) for h in self.matched],
            "unmatched": [row(h) for h in self.unmatched],
            "override_fail": self.override_fail,
        }


def match_numbers(hits: list[NumberHit], claims: Iterable[dict]) -> NumberMatchReport:
    keys = approved_value_keys(claims)
    report = NumberMatchReport()
    for h in hits:
        (report.matched if h.key in keys else report.unmatched).append(h)
    return report


def knowledge_facts_to_claims(entry_id: str, facts: list[dict], start_index: int) -> list[dict]:
    """Chuyển facts[] của mục kho thành claim DA_DUYET (origin = kho_tac_gia) để number_scan khớp được."""
    claims = []
    for i, f in enumerate(facts):
        value = f"{f['value']} {f.get('unit') or ''}".strip()
        claims.append({
            "claim_id": f"C{start_index + i:03d}",
            "verbatim": value,
            "type": "so_lieu",
            "in_article_source": f"kho:{entry_id}",
            "maybe_outdated": False,
            "outdated_reason": "",
            "action": "GIU",
            "new_value": None,
            "verification_source": f"kho:{entry_id}",
            "status": "DA_DUYET",
            "origin": "kho_tac_gia",
        })
    return claims


def uncovered_numbers(content: str, facts: list[dict]) -> list[str]:
    """Số trong content của mục kho mà facts[] chưa khai báo — dùng khi nhập kho để bắt khai thiếu."""
    declared = set()
    for f in facts:
        declared |= claim_value_keys(f"{f['value']} {f.get('unit') or ''}")
    return [h.raw for h in scan_numbers(content) if h.kind in OVERRIDE_KINDS and h.key not in declared]
