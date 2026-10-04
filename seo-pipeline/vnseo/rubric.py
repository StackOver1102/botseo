"""Tổng hợp rubric từ verdict P8a–d và các kiểm tra của code (§5.11).

Khác spec gốc: tiêu chí có nguồn bị bỏ theo mode được trả score = None (N/A),
vì schema RubricResult v5 chỉ cho 0/1/2 nên không biểu diễn được Lite.
"""
from __future__ import annotations

from dataclasses import dataclass, field

CRITERIA = [
    "intent", "information_gain", "accuracy", "uniqueness", "eeat",
    "voice", "seo_onpage", "geo_readiness", "readability",
]
_ZERO = {"chan", "cao"}


@dataclass
class Item:
    check_id: str
    verdict: str  # YES | NO
    severity: str  # chan | cao | trung_binh | thap


@dataclass
class CodeChecks:
    number_override_fail: bool = False
    blacklist_high_hits: int = 0
    containment: float = 0.0
    title_meta_ok: bool = True
    jsonld_ok: bool = True
    long_sentence_share: float = 0.0


@dataclass
class JobInfo:
    branch: str  # A | B
    mode: str  # lite | standard | heavy
    ymyl: bool


@dataclass
class Thresholds:
    rubric_pass_ratio: float = 0.77
    branch_b_containment_max: float = 0.10
    long_sentence_share_max: float = 0.10


@dataclass
class Score:
    criterion: str
    score: int | None
    blocking: bool
    sources: list[str] = field(default_factory=list)


@dataclass
class RubricResult:
    scores: list[Score]
    total: int
    max_total: int
    pass_: bool
    blocking_zero: list[str]
    report_md: str

    def to_json(self) -> dict:
        return {
            "scores": [s.__dict__ for s in self.scores],
            "total": self.total,
            "max_total": self.max_total,
            "pass": self.pass_,
            "blocking_zero": self.blocking_zero,
            "report_md": self.report_md,
        }


def _items(verdicts: dict[str, dict]) -> dict[str, Item]:
    out = {}
    for v in verdicts.values():
        for it in v["items"]:
            out[it["check_id"]] = Item(it["check_id"], it["verdict"], it["severity"])
    return out


def _score(items: list[Item]) -> int:
    if all(i.verdict == "YES" for i in items):
        return 2
    if any(i.verdict == "NO" and i.severity in _ZERO for i in items):
        return 0
    return 1


def _need(items: dict[str, Item], ids: list[str]) -> list[Item]:
    missing = [i for i in ids if i not in items]
    if missing:
        raise ValueError(f"verdict thiếu check: {missing}")
    return [items[i] for i in ids]


def aggregate(verdicts: dict[str, dict], code: CodeChecks, job: JobInfo, th: Thresholds | None = None) -> RubricResult:
    """verdicts: {pass_id: Verdict JSON}. Pass không chạy theo mode thì không có key → tiêu chí liên quan N/A."""
    th = th or Thresholds()
    items = _items(verdicts)
    ran = set(verdicts)

    # Code override chạy trước khi tính
    if "P8d" in ran and code.number_override_fail:
        items["P8d.1"] = Item("P8d.1", "NO", "chan")
    if "P8b" in ran and code.blacklist_high_hits > 0:
        items["P8b.B1"] = Item("P8b.B1", "NO", "cao")

    code_item = lambda cid, ok, sev="trung_binh": Item(cid, "YES" if ok else "NO", sev)  # noqa: E731
    scores: list[Score] = []

    def add(criterion: str, parts: list[Item] | None, blocking: bool, force_zero: bool = False) -> None:
        if parts is None:
            scores.append(Score(criterion, None, blocking))
            return
        s = 0 if force_zero else _score(parts)
        scores.append(Score(criterion, s, blocking, [p.check_id for p in parts]))

    add("intent", _need(items, ["P8c.1"]) if "P8c" in ran else None, True)
    add("information_gain", _need(items, ["P8a.1", "P8a.2"]) if "P8a" in ran else None, True)

    acc = None
    if "P8d" in ran:
        acc = _need(items, ["P8d.1", "P8d.2", "P8d.3"]) + [code_item("code.number_scan", not code.number_override_fail, "chan")]
    add("accuracy", acc, True)

    uniq_parts = (_need(items, ["P8a.4"]) if "P8a" in ran else [])
    over = job.branch == "B" and code.containment >= th.branch_b_containment_max
    add("uniqueness", uniq_parts + [code_item("code.containment", not over, "chan")], job.branch == "B", force_zero=over)

    eeat = []
    if "P8a" in ran:
        eeat += _need(items, ["P8a.3"])
    if "P8d" in ran and job.ymyl:
        eeat += _need(items, ["P8d.4"])
    add("eeat", eeat or None, job.ymyl)

    voice = None
    if "P8b" in ran:
        voice = [i for cid, i in items.items() if cid.startswith("P8b.V")] + _need(items, ["P8b.B1"])
    add("voice", voice, False)

    seo = [code_item("code.title_meta", code.title_meta_ok), code_item("code.jsonld", code.jsonld_ok)]
    if "P8c" in ran:
        seo = _need(items, ["P8c.3", "P8c.5"]) + seo
    add("seo_onpage", seo, False)

    add("geo_readiness", _need(items, ["P8c.2", "P8c.4"]) if "P8c" in ran else None, False)

    read = [code_item("code.long_sentences", code.long_sentence_share <= th.long_sentence_share_max)]
    if "P8b" in ran:
        read = _need(items, ["P8b.B2"]) + read
    add("readability", read, False)

    scored = [s for s in scores if s.score is not None]
    total = sum(s.score for s in scored)
    max_total = 2 * len(scored)
    blocking_zero = [s.criterion for s in scored if s.blocking and s.score == 0]
    passed = max_total > 0 and total / max_total >= th.rubric_pass_ratio and not blocking_zero
    return RubricResult(scores, total, max_total, passed, blocking_zero, _report(scores, total, max_total, passed))


def _report(scores: list[Score], total: int, max_total: int, passed: bool) -> str:
    rows = ["| Tiêu chí | Điểm | Blocking | Nguồn |", "|---|---|---|---|"]
    for s in scores:
        score = "N/A" if s.score is None else str(s.score)
        rows.append(f"| {s.criterion} | {score} | {'✓' if s.blocking else ''} | {', '.join(s.sources)} |")
    rows.append(f"\n**Tổng:** {total}/{max_total} — {'ĐẠT' if passed else 'TRƯỢT'}")
    return "\n".join(rows)


def failed_items(verdicts: dict[str, dict]) -> list[dict]:
    """Các mục NO để đưa vào P9 (kèm location, fix_hint)."""
    return [it for v in verdicts.values() for it in v["items"] if it["verdict"] == "NO"]
