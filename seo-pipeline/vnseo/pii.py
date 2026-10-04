"""Redact PII trước khi lưu kho tác giả hoặc gửi LLM (§9).

Khác spec gốc: số điện thoại được nhận theo HÌNH DẠNG nhóm số (3-3-3, 2-4-4…) rồi đếm chữ số, không phải "0 + 9–10 chữ
số bất kỳ có dấu cách". Cách cũ vừa sót ("(+84) 912 345 678", "0912 - 345 - 678", hai số cách nhau dấu phẩy) vừa bắt
nhầm ("hệ số 0.123456789", "0912 345 678 2 lần" nuốt luôn số 2, "10.000.000.000 đồng").
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

# Ngăn cách giữa các nhóm số của một số điện thoại: khoảng trắng, chấm, gạch (kể cả " - ")
_SEP = r"(?:\s{0,2}[.-]\s{0,2}|\s{1,2})?"
# Phần số sau đầu 0 / mã nước: liền nhau, hoặc 3 nhóm (912 345 678, 28 3822 1234), hoặc 4 nhóm (912 34 56 78, 91 234 56 78)
_BODY = rf"(?:\d{{9,10}}|\d{{2,3}}{_SEP}\d{{3,4}}{_SEP}\d{{3,4}}|\d{{2,3}}{_SEP}\d{{2,3}}{_SEP}\d{{2}}{_SEP}\d{{2}})"
_PHONE = re.compile(
    r"(?<!\d)(?<!\d\.)"                       # không nằm giữa một con số ("10.000.000.000")
    rf"(?:(?P<intl>\(\+?84\)|\+84|0084|84(?=[\s.-]?\d)){_SEP}{_BODY}"
    rf"|(?P<area>\(0\d{{2,3}}\)){_SEP}\d{{3,4}}{_SEP}\d{{3,4}}"
    rf"|(?P<local>0(?=\d)){_BODY})"           # 0 phải đi liền chữ số: "0.123456789" là số thập phân
    r"(?!\d)"
)


def _phone_ok(m: re.Match) -> bool:
    digits = len(re.sub(r"\D", "", m.group(0)))
    if m.group("intl"):
        code = len(re.sub(r"\D", "", m.group("intl")))
        return digits - code in (9, 10)
    return digits in (10, 11)  # 0 + 9 số (di động) hoặc 0 + mã vùng + số máy bàn


_PATTERNS: list[tuple[str, re.Pattern, Callable[[re.Match], bool] | None]] = [
    ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), None),
    # "stk" / "số tài khoản" cho phép tên ngân hàng chen giữa; "tài khoản" trần phải đi liền số (tránh nuốt số tiền)
    ("STK", re.compile(r"(?i)(?:(?:số tài khoản|stk)[^\d\n]{0,30}|tài khoản\s*:?\s*)\d[\d .-]{6,22}\d"), None),
    ("CCCD", re.compile(r"(?i)(?:cccd|cmnd|căn cước(?: công dân)?|chứng minh(?: nhân dân)?)[^\d\n]{0,15}\d(?:[ .]?\d){8,11}(?!\d)"),
     None),
    ("SĐT", _PHONE, _phone_ok),
    ("CCCD", re.compile(r"(?<![\d.,])\d{12}(?![\d]|[.,]\d)"), None),
]


@dataclass(frozen=True)
class PiiFinding:
    kind: str
    start: int
    end: int


def find_pii(text: str) -> list[PiiFinding]:
    found: list[PiiFinding] = []
    taken: list[tuple[int, int]] = []
    for kind, pattern, check in _PATTERNS:
        for m in pattern.finditer(text):
            if check and not check(m):
                continue
            s, e = m.span()
            if any(s < b and a < e for a, b in taken):
                continue
            taken.append((s, e))
            found.append(PiiFinding(kind, s, e))
    return sorted(found, key=lambda f: f.start)


def redact(text: str) -> tuple[str, list[PiiFinding]]:
    findings = find_pii(text)
    out = text
    for f in reversed(findings):
        out = out[:f.start] + f"[{f.kind}]" + out[f.end:]
    return out, findings
