import pytest

from pipeline.states import (
    TERMINAL, TRANSITIONS, InvalidTransition, JobState, Outcome as O, State as S, next_state,
)

RM = {"kw": 1, "fact": 1, "outline": 2}


def step(job, outcome):
    return next_state(job, outcome, RM)


def test_every_non_terminal_state_has_exit():
    sources = {s for s, _ in TRANSITIONS}
    for st in S:
        if st in TERMINAL or st in (S.NEEDS_HUMAN, S.STALE):
            continue
        assert st in sources, st


def test_all_states_reachable_and_done_reachable():
    graph = {}
    for (src, _), dst in TRANSITIONS.items():
        graph.setdefault(src, set()).add(dst)
    seen, stack = set(), [S.TRIAGE_AI, S.REWORK]  # hai điểm bắt đầu: flow full và flow rework
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(graph.get(cur, ()))
    missing = set(S) - seen - {S.NEEDS_HUMAN, S.STALE, S.CANCELLED}
    assert not missing and S.DONE in seen


def test_happy_path():
    job = JobState(S.TRIAGE_AI)
    path = [O.DONE, O.APPROVE, O.DONE, O.DONE, O.APPROVE, O.DONE, O.APPROVE, O.DONE, O.DONE, O.DONE,
            O.DONE, O.APPROVE, O.APPROVE, O.DONE, O.DONE, O.APPROVE, O.APPROVE, O.DONE, O.APPROVE,
            O.APPROVE, O.DONE, O.APPROVE, O.DONE, O.APPROVE, O.DONE, O.DONE]
    for o in path:
        job = step(job, o)
    assert job.state == S.DONE


def test_revise_counts_then_escalates():
    job = JobState(S.KW_GATE)
    job = step(job, O.REVISE)
    assert job.state == S.KW_MAP_AI and job.revise_count == {"kw": 1}
    job = step(step(job, O.DONE), O.REVISE)
    assert job.state == S.NEEDS_HUMAN and job.resume_state == S.KW_GATE
    assert job.needs_human_reason == "REVISE_EXHAUSTED"


def test_outline_counter_shared_by_preflight_and_knowledge_select():
    job = step(JobState(S.KNOWLEDGE_SELECT), O.REVISE)
    job = JobState(S.PREFLIGHT, revise_count=job.revise_count)
    job = step(job, O.REVISE)
    assert job.revise_count == {"outline": 2}
    job = step(JobState(S.OUTLINE_GATE, revise_count=job.revise_count), O.REVISE)
    assert job.state == S.NEEDS_HUMAN


@pytest.mark.parametrize("gate", [S.QA_GATE, S.FINAL_GATE, S.FACT_SIGNOFF, S.TITLE_GATE])
def test_revise_without_budget_escalates(gate):
    assert step(JobState(gate), O.REVISE).state == S.NEEDS_HUMAN


def test_rubric_fix_loop_not_counted_here():
    job = step(JobState(S.RUBRIC), O.REVISE)
    assert job.state == S.FIX and job.revise_count == {}


def test_human_resume_approve_and_rerun():
    job = step(JobState(S.FINAL_GATE), O.ESCALATE)
    assert job.state == S.NEEDS_HUMAN
    assert step(job, O.HUMAN_APPROVE).state == S.PUBLISH
    assert step(job, O.HUMAN_RERUN).state == S.FINAL_GATE
    job = step(JobState(S.KNOWLEDGE_SELECT), O.ERROR)
    assert step(job, O.HUMAN_APPROVE).state == S.NEW_FACT_CHECK


def test_stale_and_cancel():
    job = step(step(JobState(S.WRITE), O.FAIL), O.TIMEOUT)
    assert job.state == S.STALE
    assert step(job, O.HUMAN_RERUN).state == S.WRITE
    assert step(job, O.CANCEL).state == S.CANCELLED
    with pytest.raises(InvalidTransition):
        step(JobState(S.DONE), O.DONE)


def test_publish_defer_stays():
    assert step(JobState(S.PUBLISH), O.DEFER).state == S.PUBLISH


def test_fact_signoff_apply_path():
    job = step(JobState(S.FACT_SIGNOFF), O.APPLY)
    assert job.state == S.FACT_APPLY
    assert step(step(job, O.DONE), O.FAIL).state == S.NEEDS_HUMAN
