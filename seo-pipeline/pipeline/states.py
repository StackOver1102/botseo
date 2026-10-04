"""Máy trạng thái pipeline (§2) — hàm thuần, không đụng DB.

Bổ sung so với diagram trong spec:
- Mọi state chưa kết thúc: ESCALATE / FAIL / ERROR → NEEDS_HUMAN, CANCEL → CANCELLED.
- REVISE ở gate không có revise_max (QA, FACT_SIGNOFF, FINAL, TITLE, TRIAGE) hoặc đã hết lượt → NEEDS_HUMAN.
- KNOWLEDGE_SELECT và PREFLIGHT quay về GAP_IG_AI dùng chung bộ đếm "outline".
- PUBLISH: DEFER (vượt max_posts_per_week) → ở lại PUBLISH, worker hẹn giờ chạy lại.
- NEEDS_HUMAN: TIMEOUT → STALE. Người xử lý bằng HUMAN_APPROVE (đi tiếp như gate approve/pass)
  hoặc HUMAN_RERUN (chạy lại state đã dừng).
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum


class State(StrEnum):
    TRIAGE_AI = "TRIAGE_AI"
    TRIAGE_GATE = "TRIAGE_GATE"
    KW_NORMALIZE = "KW_NORMALIZE"
    KW_MAP_AI = "KW_MAP_AI"
    KW_GATE = "KW_GATE"
    FACT_VOICE_FANOUT = "FACT_VOICE_FANOUT"
    FACT_GATE = "FACT_GATE"
    SERP_INPUT = "SERP_INPUT"
    GAP_IG_AI = "GAP_IG_AI"
    KNOWLEDGE_SELECT = "KNOWLEDGE_SELECT"
    NEW_FACT_CHECK = "NEW_FACT_CHECK"
    OUTLINE_GATE = "OUTLINE_GATE"
    PREFLIGHT = "PREFLIGHT"
    WRITE = "WRITE"
    META_EDIT = "META_EDIT"
    TITLE_GATE = "TITLE_GATE"
    UNIQUENESS = "UNIQUENESS"
    QA_FANOUT = "QA_FANOUT"
    RUBRIC = "RUBRIC"
    FIX = "FIX"
    QA_RERUN_FAILED = "QA_RERUN_FAILED"
    QA_GATE = "QA_GATE"
    FACTCHECK = "FACTCHECK"
    FACT_SIGNOFF = "FACT_SIGNOFF"
    FACT_APPLY = "FACT_APPLY"
    FACT_RECHECK = "FACT_RECHECK"
    ASSEMBLE = "ASSEMBLE"
    FINAL_GATE = "FINAL_GATE"
    PUBLISH = "PUBLISH"
    GSC_28D = "GSC_28D"
    # Luồng rework (jobs.flow = 'rework'): viết lại bài có sẵn, code kiểm tra, xong
    REWORK = "REWORK"
    REWORK_CHECK = "REWORK_CHECK"
    DONE = "DONE"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    STALE = "STALE"
    CANCELLED = "CANCELLED"


class Outcome(StrEnum):
    DONE = "DONE"          # bước chạy xong
    APPROVE = "APPROVE"    # gate cho qua / kiểm tra pass
    REVISE = "REVISE"      # quay lại bước trước (có đếm lượt)
    APPLY = "APPLY"        # FACT_SIGNOFF: có claim cần tự sửa
    ESCALATE = "ESCALATE"
    FAIL = "FAIL"
    ERROR = "ERROR"        # REFUSED, PAUSED_BUDGET, lỗi hạ tầng hết retry
    DEFER = "DEFER"
    TIMEOUT = "TIMEOUT"
    CANCEL = "CANCEL"
    HUMAN_APPROVE = "HUMAN_APPROVE"
    HUMAN_RERUN = "HUMAN_RERUN"
    HUMAN_REVISE = "HUMAN_REVISE"  # người gửi gate về bước trước kèm hướng dẫn (không bị giới hạn revise_max)


S, O = State, Outcome
TERMINAL = {S.DONE, S.CANCELLED}
WAITING = {S.NEEDS_HUMAN, S.STALE}

TRANSITIONS: dict[tuple[State, Outcome], State] = {
    (S.TRIAGE_AI, O.DONE): S.TRIAGE_GATE,
    (S.TRIAGE_GATE, O.APPROVE): S.KW_NORMALIZE,
    (S.KW_NORMALIZE, O.DONE): S.KW_MAP_AI,
    (S.KW_MAP_AI, O.DONE): S.KW_GATE,
    (S.KW_GATE, O.APPROVE): S.FACT_VOICE_FANOUT,
    (S.KW_GATE, O.REVISE): S.KW_MAP_AI,
    (S.FACT_VOICE_FANOUT, O.DONE): S.FACT_GATE,
    (S.FACT_GATE, O.APPROVE): S.SERP_INPUT,
    (S.FACT_GATE, O.REVISE): S.FACT_VOICE_FANOUT,
    (S.SERP_INPUT, O.DONE): S.GAP_IG_AI,
    (S.GAP_IG_AI, O.DONE): S.KNOWLEDGE_SELECT,
    (S.KNOWLEDGE_SELECT, O.DONE): S.NEW_FACT_CHECK,
    (S.KNOWLEDGE_SELECT, O.REVISE): S.GAP_IG_AI,
    (S.NEW_FACT_CHECK, O.DONE): S.OUTLINE_GATE,
    (S.OUTLINE_GATE, O.APPROVE): S.PREFLIGHT,
    (S.OUTLINE_GATE, O.REVISE): S.GAP_IG_AI,
    (S.PREFLIGHT, O.APPROVE): S.WRITE,
    (S.PREFLIGHT, O.REVISE): S.GAP_IG_AI,
    (S.WRITE, O.DONE): S.META_EDIT,
    (S.META_EDIT, O.DONE): S.TITLE_GATE,
    (S.TITLE_GATE, O.APPROVE): S.UNIQUENESS,
    (S.UNIQUENESS, O.APPROVE): S.QA_FANOUT,
    (S.QA_FANOUT, O.DONE): S.RUBRIC,
    (S.RUBRIC, O.APPROVE): S.QA_GATE,
    (S.RUBRIC, O.REVISE): S.FIX,
    (S.FIX, O.DONE): S.QA_RERUN_FAILED,
    (S.QA_RERUN_FAILED, O.DONE): S.RUBRIC,
    (S.QA_GATE, O.APPROVE): S.FACTCHECK,
    (S.FACTCHECK, O.DONE): S.FACT_SIGNOFF,
    (S.FACT_SIGNOFF, O.APPROVE): S.ASSEMBLE,
    (S.FACT_SIGNOFF, O.APPLY): S.FACT_APPLY,
    (S.FACT_APPLY, O.DONE): S.FACT_RECHECK,
    (S.FACT_RECHECK, O.APPROVE): S.ASSEMBLE,
    (S.ASSEMBLE, O.DONE): S.FINAL_GATE,
    (S.FINAL_GATE, O.APPROVE): S.PUBLISH,
    (S.PUBLISH, O.DONE): S.GSC_28D,
    (S.PUBLISH, O.DEFER): S.PUBLISH,
    (S.GSC_28D, O.DONE): S.DONE,
    (S.NEEDS_HUMAN, O.TIMEOUT): S.STALE,
    (S.REWORK, O.DONE): S.REWORK_CHECK,
    (S.REWORK_CHECK, O.APPROVE): S.DONE,
    (S.REWORK_CHECK, O.REVISE): S.REWORK,
}

# State có vòng revise → key bộ đếm trong jobs.revise_count (và config gates.revise_max)
REVISE_COUNTER: dict[State, str] = {
    S.KW_GATE: "kw",
    S.FACT_GATE: "fact",
    S.OUTLINE_GATE: "outline",
    S.KNOWLEDGE_SELECT: "outline",
    S.PREFLIGHT: "outline",
    S.REWORK_CHECK: "rework",
}
# Vòng fix của RUBRIC dùng fix_round theo check_id (xử lý ở step RUBRIC, không đếm ở đây)
_UNCOUNTED_REVISE = {S.RUBRIC}


class NeedsHumanReason(StrEnum):
    GATE_ESCALATE = "GATE_ESCALATE"
    REVISE_EXHAUSTED = "REVISE_EXHAUSTED"
    RULE_FAIL = "RULE_FAIL"
    STEP_FAIL = "STEP_FAIL"
    REFUSED = "REFUSED"
    PAUSED_BUDGET = "PAUSED_BUDGET"
    ERROR = "ERROR"
    YMYL_FINAL = "YMYL_FINAL"


@dataclass(frozen=True)
class JobState:
    state: State
    resume_state: State | None = None
    needs_human_reason: str | None = None
    revise_count: dict[str, int] = field(default_factory=dict)


class InvalidTransition(Exception):
    pass


def _to_human(job: JobState, reason: str) -> JobState:
    return replace(job, state=S.NEEDS_HUMAN, resume_state=job.state, needs_human_reason=reason)


def next_state(job: JobState, outcome: Outcome, revise_max: dict[str, int], reason: str | None = None) -> JobState:
    """Tính trạng thái kế tiếp. `reason` ghi lý do khi vào NEEDS_HUMAN."""
    st = job.state
    if st in TERMINAL:
        raise InvalidTransition(f"{st} là trạng thái kết thúc")

    if outcome == O.CANCEL:
        return replace(job, state=S.CANCELLED)

    if st in WAITING:
        if outcome == O.TIMEOUT and st == S.NEEDS_HUMAN:
            return replace(job, state=S.STALE)
        if outcome in (O.HUMAN_APPROVE, O.HUMAN_RERUN, O.HUMAN_REVISE):
            stopped = job.resume_state
            if stopped is None:
                raise InvalidTransition("thiếu resume_state")
            if outcome == O.HUMAN_RERUN:
                target = stopped
            elif outcome == O.HUMAN_REVISE:
                key = REVISE_COUNTER.get(stopped)
                target = TRANSITIONS.get((stopped, O.REVISE))
                if key is None or target is None:
                    raise InvalidTransition(f"{stopped} không có vòng revise")
                # Vẫn đếm lượt để feedback_for() khớp đúng vòng; người được vượt revise_max
                counts = {**job.revise_count, key: job.revise_count.get(key, 0) + 1}
                return replace(job, state=target, resume_state=None, needs_human_reason=None, revise_count=counts)
            else:
                target = TRANSITIONS.get((stopped, O.APPROVE)) or TRANSITIONS.get((stopped, O.DONE))
                if target is None:
                    raise InvalidTransition(f"{stopped} không có đường approve")
            return replace(job, state=target, resume_state=None, needs_human_reason=None)
        raise InvalidTransition(f"{st} không nhận {outcome}")

    if outcome in (O.ESCALATE, O.FAIL, O.ERROR):
        default = {O.ESCALATE: NeedsHumanReason.GATE_ESCALATE, O.FAIL: NeedsHumanReason.STEP_FAIL}.get(
            outcome, NeedsHumanReason.ERROR
        )
        return _to_human(job, reason or default)

    if outcome == O.REVISE and st not in _UNCOUNTED_REVISE:
        key = REVISE_COUNTER.get(st)
        used = job.revise_count.get(key, 0) if key else 0
        if key is None or used >= revise_max.get(key, 0):
            return _to_human(job, reason or NeedsHumanReason.REVISE_EXHAUSTED)
        job = replace(job, revise_count={**job.revise_count, key: used + 1})

    target = TRANSITIONS.get((st, outcome))
    if target is None:
        raise InvalidTransition(f"{st} không nhận {outcome}")
    return replace(job, state=target)


def revise_remaining(job: JobState, st: State, revise_max: dict[str, int]) -> int:
    """Số lượt revise còn lại — đưa vào prompt G (<so_lan_da_revise>)."""
    key = REVISE_COUNTER.get(st)
    if key is None:
        return 0
    return max(0, revise_max.get(key, 0) - job.revise_count.get(key, 0))
