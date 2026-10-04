from datetime import timedelta

import pytest

from pipeline import queue
from pipeline.artifacts import ArtifactStore, LocalBlobStore
from pipeline.db import migrate
from pipeline.executor import (
    GateDecision, RetryableError, StepRefused, StepResult, Worker, human_resolve, sweep_waiting,
)
from pipeline.idempotency import idempotency_key
from pipeline.states import Outcome as O, State as S


def new_job(conn, **kw):
    return queue.create_job(conn, topic="vay tiền online", author_id="a1", created_by="tester",
                            cost_cap_usd=kw.pop("cap", 3.0), **kw)


def state_of(conn, job_id):
    return conn.execute("SELECT * FROM jobs WHERE id = %s", (job_id,)).fetchone()


def make_worker(conn, config, blob_dir, handlers, wid="w1"):
    return Worker(conn, config, ArtifactStore(LocalBlobStore(blob_dir)), handlers, worker_id=wid)


def test_migrate_is_idempotent(conn):
    assert migrate(conn) == []


def test_artifact_versions_and_dedup(conn, blob_dir):
    job = new_job(conn)
    store = ArtifactStore(LocalBlobStore(blob_dir))
    a1 = store.save(conn, job, "KeywordMap", {"rows": [1]}, "KW_MAP_AI")
    a1_again = store.save(conn, job, "KeywordMap", {"rows": [1]}, "KW_MAP_AI")
    a2 = store.save(conn, job, "KeywordMap", {"rows": [1, 2]}, "human:ed1")
    assert (a1.version, a1_again.id, a2.version) == (1, a1.id, 2)
    assert store.latest(conn, job, "KeywordMap").payload == {"rows": [1, 2]}
    assert store.get(conn, a1.id).payload == {"rows": [1]}


def test_worker_runs_steps_and_logs(conn, config, blob_dir):
    job = new_job(conn)
    calls = []

    def triage(ctx):
        ctx.artifacts.save(ctx.conn, ctx.job_id, "TriageSuggestion", {"branch_suggest": "A"}, "TRIAGE_AI")
        calls.append("triage")
        return StepResult(O.DONE)

    def gate(ctx):
        return StepResult(O.APPROVE, decisions=[GateDecision("TRIAGE", "ai", "auto", "approve", 0.9)])

    w = make_worker(conn, config, blob_dir, {S.TRIAGE_AI: triage, S.TRIAGE_GATE: gate})
    assert w.run_until_idle() == 3  # bước 3: KW_NORMALIZE chưa có handler → NEEDS_HUMAN
    row = state_of(conn, job)
    assert row["state"] == "NEEDS_HUMAN" and row["resume_state"] == "KW_NORMALIZE"
    assert row["needs_human_reason"].startswith("no_handler")
    log = conn.execute("SELECT from_state, to_state FROM job_transitions WHERE job_id = %s ORDER BY id", (job,)).fetchall()
    assert [(r["from_state"], r["to_state"]) for r in log] == [
        ("TRIAGE_AI", "TRIAGE_GATE"), ("TRIAGE_GATE", "KW_NORMALIZE"), ("KW_NORMALIZE", "NEEDS_HUMAN")]
    assert conn.execute("SELECT count(*) AS n FROM gate_decisions WHERE job_id = %s", (job,)).fetchone()["n"] == 1


def test_retry_then_escalate(conn, config, blob_dir):
    job = new_job(conn)

    def flaky(ctx):
        raise RetryableError("429")

    w = make_worker(conn, config, blob_dir, {S.TRIAGE_AI: flaky})
    for _ in range(3):
        assert w.run_once()
        conn.execute("UPDATE jobs SET run_after = now() WHERE id = %s", (job,))  # bỏ qua backoff trong test
    row = state_of(conn, job)
    assert row["state"] == "NEEDS_HUMAN" and "retry_exhausted" in row["needs_human_reason"]


def test_refusal_and_budget_go_to_human(conn, config, blob_dir):
    j1 = new_job(conn)
    j2 = new_job(conn, cap=0)

    def refuse(ctx):
        raise StepRefused("policy")

    make_worker(conn, config, blob_dir, {S.TRIAGE_AI: refuse}).run_until_idle()
    assert state_of(conn, j1)["needs_human_reason"].startswith("REFUSED")
    assert state_of(conn, j2)["needs_human_reason"] == "PAUSED_BUDGET"


def test_lease_blocks_second_worker_and_expires(conn, config, blob_dir):
    job = new_job(conn)
    row = queue.claim(conn, "w1", timedelta(minutes=5))
    assert row["id"] == job
    assert queue.claim(conn, "w2", timedelta(minutes=5)) is None
    conn.execute("UPDATE jobs SET lease_until = now() - interval '1 second' WHERE id = %s", (job,))
    assert queue.claim(conn, "w2", timedelta(minutes=5))["id"] == job
    # w1 đã mất lease nên không ghi được transition
    before = queue.job_state(row)
    from pipeline.states import next_state
    after = next_state(before, O.DONE, config["gates"]["revise_max"])
    assert not queue.write_transition(conn, job, before, after, "DONE", "w1", worker_id="w1")


def test_human_resolve_and_cancel(conn, config, blob_dir):
    job = new_job(conn)
    make_worker(conn, config, blob_dir, {S.TRIAGE_AI: lambda ctx: StepResult(O.ESCALATE, "mơ hồ")}).run_until_idle()
    with pytest.raises(ValueError):
        human_resolve(conn, job, "ed1", "approve", " ", config)
    after = human_resolve(conn, job, "ed1", "rerun", "đã bổ sung brief", config)
    assert after.state == S.TRIAGE_AI
    d = conn.execute("SELECT actor, action, user_id FROM gate_decisions WHERE job_id = %s", (job,)).fetchone()
    assert d == {"actor": "human", "action": "revise", "user_id": "ed1"}
    make_worker(conn, config, blob_dir, {S.TRIAGE_AI: lambda ctx: StepResult(O.FAIL)}).run_until_idle()
    assert human_resolve(conn, job, "ed1", "cancel", "trùng chủ đề", config).state == S.CANCELLED


def test_sweeper_marks_stale(conn, config, blob_dir):
    job = new_job(conn)
    make_worker(conn, config, blob_dir, {S.TRIAGE_AI: lambda ctx: StepResult(O.FAIL)}).run_until_idle()
    conn.execute("UPDATE jobs SET updated_at = now() - interval '3 days' WHERE id = %s", (job,))
    assert job in sweep_waiting(conn, config)["remind"]
    conn.execute("UPDATE jobs SET updated_at = now() - interval '15 days' WHERE id = %s", (job,))
    assert job in sweep_waiting(conn, config)["stale"]
    assert state_of(conn, job)["state"] == "STALE"


def test_idempotency_key_changes():
    base = dict(step_id="P1", input_artifact_hashes=["a", "b"], prompt_version="4.0.0", provider="anthropic",
                model="claude-sonnet-5", params={"effort": "medium"}, config_thresholds={"x": 1})
    k = idempotency_key(**base)
    assert k == idempotency_key(**{**base, "input_artifact_hashes": ["b", "a"]})
    assert k != idempotency_key(**{**base, "model": "claude-opus-5-5"})
    assert k != idempotency_key(**{**base, "config_thresholds": {"x": 2}})


def test_human_revise_sends_gate_back_with_feedback(conn, config, blob_dir):
    job = new_job(conn)
    store = ArtifactStore(LocalBlobStore(blob_dir))
    conn.execute("UPDATE jobs SET state = 'NEEDS_HUMAN', resume_state = 'OUTLINE_GATE', revise_count = '{\"outline\": 2}' "
                 "WHERE id = %s", (job,))
    after = human_resolve(conn, job, "ed1", "revise", "bỏ section S02", config, store)
    assert after.state == S.GAP_IG_AI and after.revise_count == {"outline": 3}  # người được vượt revise_max
    fb = store.latest(conn, job, "Feedback:outline").payload
    assert fb["round"] == 3 and "bỏ section S02" in fb["text"]


def test_lease_extended_while_handler_runs(conn, config, blob_dir):
    """Lease ngắn nhưng được gia hạn trong lúc handler chạy lâu (gọi model vài phút) — không bị worker khác giành."""
    import time
    job = new_job(conn)
    config = {**config, "worker": {**config["worker"], "lease_seconds": 2}}
    seen = []

    def slow(ctx):
        time.sleep(3.5)  # > lease 2s: không có gia hạn thì lease đã hết
        seen.append(conn.execute("SELECT lease_until > now() AS ok FROM jobs WHERE id = %s", (ctx.job_id,)).fetchone()["ok"])
        return StepResult(O.DONE)

    make_worker(conn, config, blob_dir, {S.TRIAGE_AI: slow}).run_once()
    assert seen == [True] and state_of(conn, job)["state"] == "TRIAGE_GATE"
