"""Autopilot: hệ thống không người — job không nằm lại ở NEEDS_HUMAN."""
from pipeline import queue
from pipeline.artifacts import ArtifactStore, LocalBlobStore
from pipeline.executor import StepResult, Worker
from pipeline.llm.runner import InvalidOutput, PromptRunner
from pipeline.states import Outcome as O, State as S

from .test_runner_external import GOOD, REG, SCHEMAS, SHARED, TRIAGE_VARS, ScriptedProvider


def new_job(conn, cap=3.0, state=None):
    job = queue.create_job(conn, topic="t", author_id="a", created_by="x", cost_cap_usd=cap)
    if state:
        conn.execute("UPDATE jobs SET state = %s WHERE id = %s", (state.value, job))
    return job


def run(conn, auto_config, blob_dir, handlers, times=1):
    store = ArtifactStore(LocalBlobStore(blob_dir))
    w = Worker(conn, auto_config, store, handlers, worker_id="w1")
    for _ in range(times):
        conn.execute("UPDATE jobs SET run_after = now()")  # bỏ backoff để test nhanh
        w.run_once()
    return store


def row(conn, job):
    return conn.execute("SELECT * FROM jobs WHERE id = %s", (job,)).fetchone()


def autopilot_log(conn, job):
    return [(r["outcome"], r["to_state"]) for r in conn.execute(
        "SELECT * FROM job_transitions WHERE job_id = %s AND actor = 'autopilot' ORDER BY id", (job,))]


def test_error_reruns_with_growing_backoff_until_success(conn, auto_config, blob_dir):
    job = new_job(conn)
    calls = []

    def flaky(ctx):
        calls.append(1)
        if len(calls) < 3:
            raise InvalidOutput("B0-H: output sai sau 2 lần repair")
        return StepResult(O.DONE)

    store = ArtifactStore(LocalBlobStore(blob_dir))
    w = Worker(conn, auto_config, store, {S.TRIAGE_AI: flaky}, worker_id="w1")
    w.run_once()
    r = row(conn, job)
    assert r["state"] == "TRIAGE_AI"  # không nằm ở NEEDS_HUMAN
    delay1 = conn.execute("SELECT run_after - now() AS d FROM jobs WHERE id = %s", (job,)).fetchone()["d"].total_seconds()
    conn.execute("UPDATE jobs SET run_after = now()")
    w.run_once()
    delay2 = conn.execute("SELECT run_after - now() AS d FROM jobs WHERE id = %s", (job,)).fetchone()["d"].total_seconds()
    assert 50 < delay1 < 61 and 290 < delay2 < 301
    conn.execute("UPDATE jobs SET run_after = now()")
    w.run_once()
    assert row(conn, job)["state"] == "TRIAGE_GATE"
    assert autopilot_log(conn, job) == [("AUTO_RERUN", "TRIAGE_AI"), ("AUTO_RERUN", "TRIAGE_AI")]


def test_gate_escalate_revises_then_approves_with_warning(conn, auto_config, blob_dir):
    job = new_job(conn, state=S.KW_GATE)
    handlers = {S.KW_GATE: lambda ctx: StepResult(O.ESCALATE, "primary sai ý định"),
                S.KW_MAP_AI: lambda ctx: StepResult(O.DONE)}
    store = run(conn, auto_config, blob_dir, handlers, times=5)  # gate, map, gate, map, gate
    assert autopilot_log(conn, job) == [("AUTO_REVISE", "KW_MAP_AI"), ("AUTO_REVISE", "KW_MAP_AI"),
                                        ("AUTO_APPROVE", "FACT_VOICE_FANOUT")]
    fb = store.latest(conn, job, "Feedback:kw").payload
    assert fb == {"round": 2, "text": "primary sai ý định"}
    assert store.latest(conn, job, "Warnings").payload["items"][0]["ai_action"] == "autopilot_approve"


def test_uniqueness_fail_rewinds_to_write(conn, auto_config, blob_dir):
    job = new_job(conn, state=S.UNIQUENESS)
    store = run(conn, auto_config, blob_dir, {S.UNIQUENESS: lambda ctx: StepResult(O.FAIL, "containment 0.180 ≥ 0.1")})
    r = row(conn, job)
    assert r["state"] == "WRITE" and r["revise_count"] == {"write": 1}
    assert "Viết lại" in store.latest(conn, job, "Feedback:write").payload["text"]


def test_budget_pause_is_the_only_stop(conn, auto_config, blob_dir):
    job = new_job(conn, cap=0.0)
    run(conn, auto_config, blob_dir, {S.TRIAGE_AI: lambda ctx: StepResult(O.DONE)})
    assert row(conn, job)["state"] == "NEEDS_HUMAN" and autopilot_log(conn, job) == []


def test_step_can_rewind_to_root_cause(conn, auto_config, blob_dir):
    job = new_job(conn, state=S.RUBRIC)
    run(conn, auto_config, blob_dir, {S.RUBRIC: lambda ctx: StepResult(O.REVISE, "thiếu fact",
                                                                         rewind_to=S.FACT_VOICE_FANOUT, bump=("fact",))})
    r = row(conn, job)
    assert r["state"] == "FACT_VOICE_FANOUT" and r["revise_count"] == {"fact": 1}
    t = conn.execute("SELECT outcome FROM job_transitions WHERE job_id = %s", (job,)).fetchone()
    assert t["outcome"] == "AUTO_REWIND"


def test_rerun_after_invalid_asks_model_again(conn, auto_config, blob_dir):
    """Vòng mới dùng key mới: task external cũ (output sai) không bị đọc lại."""
    job = new_job(conn)
    keys = []

    class Prov(ScriptedProvider):
        def complete(self, req, key):
            keys.append(key)
            return super().complete(req, key)

    prov = Prov(["x", "y", "z", GOOD])
    runner = PromptRunner(conn, auto_config, REG, SCHEMAS, SHARED, LocalBlobStore(blob_dir), lambda n, j, s: prov)
    kw = dict(job_id=job, step="TRIAGE_AI", prompt_id="B0-H", variables=TRIAGE_VARS, input_hashes=["h"])
    try:
        runner.run(**kw)
    except InvalidOutput:
        pass
    out = runner.run(**kw)
    assert out.parsed["branch_suggest"] == "A"
    assert ":r1:" in keys[3] and not any(":r1:" in k for k in keys[:3])
    uri = conn.execute("SELECT request_redacted->>'prompt_uri' AS u FROM prompt_runs WHERE job_id = %s AND status = 'ok'",
                       (job,)).fetchone()["u"]
    assert "=== USER ===" in LocalBlobStore(blob_dir).get(uri).decode()  # prompt lưu để debug
