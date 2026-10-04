import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from pipeline import queue
from pipeline.artifacts import ArtifactStore, LocalBlobStore
from pipeline.executor import StepResult, Worker
from pipeline.llm.base import LLMResult, Truncated
from pipeline.llm.external import ExternalProvider, claim_next, fail_task, submit_result
from pipeline.llm.runner import InvalidOutput, PromptRunner
from pipeline.prompts import load_registry, load_schemas, load_shared
from pipeline.states import Outcome as O, State as S

REG, SCHEMAS, SHARED = load_registry(), load_schemas(), load_shared()
TRIAGE_VARS = {"JOB_BRIEF": "vay tiền", "SOURCE_EXCERPT_MARKED": "", "TONE_STYLE": "mới", "TONE_EXAMPLE": "hoà",
               "DATAMARK": "^"}
GOOD = json.dumps({"branch_suggest": "A", "ymyl": True, "mode_suggest": "standard", "evidence": ["vay"]})


class ScriptedProvider:
    name = "gemini"

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.requests = []

    def complete(self, req, key):
        self.requests.append(req)
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return LLMResult(text=out, stop_reason="STOP", model=req.model, tokens_in=10, tokens_out=5, cost_usd=0.01)


def make_runner(conn, config, blob_dir, provider):
    return PromptRunner(conn, config, REG, SCHEMAS, SHARED, LocalBlobStore(blob_dir), lambda name, job, step: provider)


def new_job(conn):
    return queue.create_job(conn, topic="t", author_id="a", created_by="x", cost_cap_usd=3)


def test_repair_then_cache(conn, config, blob_dir):
    job = new_job(conn)
    prov = ScriptedProvider(['{"branch_suggest": "C"}', GOOD])
    runner = make_runner(conn, config, blob_dir, prov)
    out = runner.run(job_id=job, step="TRIAGE_AI", prompt_id="B0-H", variables=TRIAGE_VARS, input_hashes=["h"])
    assert out.parsed["branch_suggest"] == "A" and not out.cached
    assert "<loi_kiem_tra>" in prov.requests[1].user and "branch_suggest" in prov.requests[1].user
    runs = conn.execute("SELECT status, repair_attempt FROM prompt_runs WHERE job_id = %s ORDER BY created_at", (job,)).fetchall()
    assert [(r["status"], r["repair_attempt"]) for r in runs] == [("invalid", 0), ("ok", 1)]
    assert float(conn.execute("SELECT cost_usd FROM jobs WHERE id = %s", (job,)).fetchone()["cost_usd"]) == pytest.approx(0.02)
    again = runner.run(job_id=job, step="TRIAGE_AI", prompt_id="B0-H", variables=TRIAGE_VARS, input_hashes=["h"])
    assert again.cached and again.parsed == out.parsed and len(prov.requests) == 2


def test_truncated_bumps_once_then_gives_up(conn, config, blob_dir):
    job = new_job(conn)
    prov = ScriptedProvider([Truncated("x"), GOOD])
    make_runner(conn, config, blob_dir, prov).run(job_id=job, step="s", prompt_id="B0-H", variables=TRIAGE_VARS, input_hashes=[])
    assert [r.max_output_tokens for r in prov.requests] == [1500, 2250]
    with pytest.raises(Truncated):
        make_runner(conn, config, blob_dir, ScriptedProvider([Truncated("x"), Truncated("x")])).run(
            job_id=job, step="s2", prompt_id="B0-H", variables=TRIAGE_VARS, input_hashes=[])


def test_invalid_after_repairs(conn, config, blob_dir):
    job = new_job(conn)
    with pytest.raises(InvalidOutput):
        make_runner(conn, config, blob_dir, ScriptedProvider(["x", "y", "z"])).run(
            job_id=job, step="s", prompt_id="B0-H", variables=TRIAGE_VARS, input_hashes=[])


def test_external_flow_through_worker(conn, config, blob_dir):
    """Job chờ extension → extension claim + gửi kết quả sai rồi đúng → job đi tiếp."""
    job = new_job(conn)
    ext_cfg = config["providers"]["gemini"]
    seen = {}

    def triage(ctx):
        runner = PromptRunner(ctx.conn, ctx.config, REG, SCHEMAS, SHARED, LocalBlobStore(blob_dir),
                              lambda name, j, step: ExternalProvider(name, ext_cfg, ctx.conn, j, step))
        seen["out"] = runner.run(job_id=ctx.job_id, step="TRIAGE_AI", prompt_id="B0-H", variables=TRIAGE_VARS,
                                 input_hashes=[])
        return StepResult(O.DONE)

    w = Worker(conn, config, ArtifactStore(LocalBlobStore(blob_dir)), {S.TRIAGE_AI: triage})
    assert w.run_once()
    row = conn.execute("SELECT * FROM jobs WHERE id = %s", (job,)).fetchone()
    assert row["state"] == "TRIAGE_AI" and row["attempts"] == 0 and row["last_error"].startswith("awaiting_external")
    assert not w.run_once()  # đang hẹn giờ, chưa tới lượt

    task = claim_next(conn, "gemini", "ext-1", timedelta(minutes=5))
    assert "<output_schema>" in task["prompt_text"] and claim_next(conn, "gemini", "ext-2", timedelta(minutes=5)) is None
    assert not submit_result(conn, task["id"], "ext-2", GOOD, None)  # client khác không được nộp
    assert submit_result(conn, task["id"], "ext-1", "```json\n{\"branch_suggest\": 1}\n```", None)

    assert w.run_once()  # kết quả sai → tạo task repair, lại chờ
    repair = claim_next(conn, "gemini", "ext-1", timedelta(minutes=5))
    assert "<loi_kiem_tra>" in repair["prompt_text"]
    assert fail_task(conn, repair["id"], "ext-1", "tab bị đóng")
    assert w.run_once()  # task lỗi → đưa lại pending
    repair = claim_next(conn, "gemini", "ext-1", timedelta(minutes=5))
    submit_result(conn, repair["id"], "ext-1", GOOD, {"model": "gemini-web"})

    assert w.run_once()
    assert conn.execute("SELECT state FROM jobs WHERE id = %s", (job,)).fetchone()["state"] == "TRIAGE_GATE"
    assert seen["out"].parsed["mode_suggest"] == "standard"


def test_extension_api(conn, db_url, monkeypatch):
    monkeypatch.setenv("SEO_DATABASE_URL", db_url)
    monkeypatch.setenv("SEO_EXTENSION_TOKEN", "secret")
    from app.main import app
    conn.execute("""INSERT INTO external_tasks (idempotency_key, prompt_id, provider, model_hint, prompt_text, expect_keys)
                    VALUES ('k1', 'P10', 'openai', 'gpt-6-sol', 'hỏi gì đó', '{items}')""")
    client = TestClient(app)
    h = {"X-Extension-Token": "secret", "X-Client-Id": "tab-1"}
    assert client.get("/external/tasks/next?provider=openai", headers={**h, "X-Extension-Token": "sai"}).status_code == 401
    assert client.get("/external/tasks/next?provider=gemini", headers=h).status_code == 204
    t = client.get("/external/tasks/next?provider=openai", headers=h).json()
    assert t["prompt_text"] == "hỏi gì đó" and t["expect_keys"] == ["items"]
    assert t["selectors"]["send"] and t["selectors"]["input"]  # selector từ config.yaml
    assert client.post(f"/external/tasks/{t['id']}/result", json={"text": "ok"},
                       headers={**h, "X-Client-Id": "tab-2"}).status_code == 409
    r = client.post(f"/external/tasks/{t['id']}/result", json={"text": "ok", "source_urls": ["https://x.vn"]}, headers=h)
    assert r.status_code == 204
    row = conn.execute("SELECT status, result_meta FROM external_tasks WHERE id = %s", (t["id"],)).fetchone()
    assert row["status"] == "done" and row["result_meta"]["source_urls"] == ["https://x.vn"]


def test_extension_api_marks_deep_research(conn, db_url, monkeypatch):
    monkeypatch.setenv("SEO_DATABASE_URL", db_url)
    monkeypatch.setenv("SEO_EXTENSION_TOKEN", "secret")
    from app.main import app
    conn.execute("""INSERT INTO external_tasks (idempotency_key, prompt_id, provider, model_hint, prompt_text)
                    VALUES ('dr1', 'P4-DR', 'gemini', 'gemini-deep-research', 'nghiên cứu')""")
    t = TestClient(app).get("/external/tasks/next?provider=gemini",
                            headers={"X-Extension-Token": "secret", "X-Client-Id": "tab-1"}).json()
    assert t["tool"] == "deep_research" and t["timeout_seconds"] == 2400
    assert "Deep research" in t["tool_texts"]["menu_path"][-1]
    lease = conn.execute("SELECT lease_until - now() AS left FROM external_tasks WHERE id = %s", (t["id"],)).fetchone()["left"]
    assert lease.total_seconds() > 2400  # giữ task lâu hơn thời gian chạy Deep Research


def test_long_poll_wakes_on_notify(conn, db_url, monkeypatch):
    """Task tạo ra khi extension đang long-poll được trả ngay nhờ NOTIFY (không chờ hết `wait`)."""
    import threading
    import time
    monkeypatch.setenv("SEO_DATABASE_URL", db_url)
    monkeypatch.setenv("SEO_EXTENSION_TOKEN", "secret")
    from app.main import app

    def insert_later():
        time.sleep(1.0)
        conn.execute("""INSERT INTO external_tasks (idempotency_key, prompt_id, provider, model_hint, prompt_text)
                        VALUES ('k-lp', 'G', 'openai', 'x', 'chấm')""")
    threading.Thread(target=insert_later).start()
    t0 = time.monotonic()
    r = TestClient(app).get("/external/tasks/next?provider=openai&wait=20",
                            headers={"X-Extension-Token": "secret", "X-Client-Id": "tab-1"})
    elapsed = time.monotonic() - t0
    assert r.status_code == 200 and r.json()["prompt_id"] == "G"
    assert elapsed < 2.5  # ~1s chờ insert, không phải 20s
    assert r.json()["chat_url"] == "https://chatgpt.com/"  # G thuộc fast_prompts, openai.fast = null → default


def test_listener_filters_payload(db_url):
    from pipeline.db import connect
    from pipeline.notify import Listener
    with Listener("ext_task", url=db_url) as lis, connect(db_url, autocommit=True) as c:
        c.execute("SELECT pg_notify('ext_task', 'gemini')")
        assert not lis.wait(0.5, lambda ch, p: p == "openai")
        c.execute("SELECT pg_notify('ext_task', 'openai')")
        assert lis.wait(2, lambda ch, p: p == "openai")
