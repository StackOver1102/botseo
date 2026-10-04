"""Bảng handler theo state. State chưa có handler → worker chuyển job sang NEEDS_HUMAN (no_handler)."""
from __future__ import annotations

import functools

from ..executor import Handler, StepContext, StepResult
from ..states import NeedsHumanReason, Outcome, State
from . import m0, m1, rework
from .common import Services, StepFailed


def _guard(fn: Handler) -> Handler:
    """StepFailed = thiếu/sai dữ liệu vào: chuyển cho người với lý do rõ, không log như lỗi code."""
    @functools.wraps(fn)
    def wrapper(ctx: StepContext) -> StepResult:
        try:
            return fn(ctx)
        except StepFailed as e:
            return StepResult(Outcome.FAIL, f"{NeedsHumanReason.STEP_FAIL}: {ctx.state.state.value}: {e}")
    return wrapper


HANDLERS: dict[State, Handler] = {state: _guard(fn) for state, fn in {
    State.TRIAGE_AI: m0.triage_ai,
    State.TRIAGE_GATE: m0.triage_gate,
    State.KW_NORMALIZE: m0.kw_normalize,
    State.KW_MAP_AI: m0.kw_map_ai,
    State.KW_GATE: m0.kw_gate,
    State.FACT_VOICE_FANOUT: m0.fact_voice_fanout,
    State.FACT_GATE: m0.fact_gate,
    State.SERP_INPUT: m0.serp_input,
    State.GAP_IG_AI: m0.gap_ig_ai,
    State.KNOWLEDGE_SELECT: m0.knowledge_select,
    State.NEW_FACT_CHECK: m0.new_fact_check,
    State.OUTLINE_GATE: m0.outline_gate,
    State.PREFLIGHT: m0.preflight,
    State.WRITE: m0.write,
    State.META_EDIT: m0.meta_edit,
    State.TITLE_GATE: m0.title_gate,
    State.UNIQUENESS: m0.uniqueness,
    State.QA_FANOUT: m1.qa_fanout,
    State.RUBRIC: m1.rubric,
    State.FIX: m1.fix,
    State.QA_RERUN_FAILED: m1.qa_rerun_failed,
    State.QA_GATE: m1.qa_gate,
    State.FACTCHECK: m1.factcheck,
    State.FACT_SIGNOFF: m1.fact_signoff,
    State.FACT_APPLY: m1.fact_apply,
    State.FACT_RECHECK: m1.fact_recheck,
    State.ASSEMBLE: m1.assemble,
    State.FINAL_GATE: m1.final_gate,
    State.PUBLISH: m1.publish,
    State.GSC_28D: m1.gsc_28d,
    State.REWORK: rework.rework,
    State.REWORK_CHECK: rework.rework_check,
}.items()}

__all__ = ["HANDLERS", "Services", "StepFailed"]
