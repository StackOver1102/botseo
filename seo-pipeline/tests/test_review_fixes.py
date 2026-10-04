"""Các lỗi tìm thấy khi rà soát code (2026-10): autopilot duyệt thay bước tạo artifact, chi phí lượt gọi hỏng,
vòng chạy lại vô hạn, regex tiền tệ, URL nội bộ, đường dẫn blob khi chuyển máy."""
import pytest

from pipeline import queue
from pipeline.artifacts import LocalBlobStore
from pipeline.executor import StepResult
from pipeline.llm.base import LLMResult, Refused, Truncated
from pipeline.states import Outcome as O, State as S
from pipeline.steps import _guard
from pipeline.steps.common import StepFailed
from pipeline.urlcheck import UrlVerifier, is_public_url
from vnseo.facts import scan_numbers

from .test_autopilot import autopilot_log, new_job, row, run
from .test_runner_external import GOOD, TRIAGE_VARS, ScriptedProvider, make_runner


# ---------- autopilot ----------

def test_failed_producing_step_is_not_approved(conn, auto_config, blob_dir):
    """WRITE không tạo được bài (StepFailed): autopilot chạy lại, KHÔNG đẩy job sang META_EDIT với bài không tồn tại."""
    job = new_job(conn, state=S.WRITE)

    def write(ctx):
        raise StepFailed("bài viết sai cấu trúc")

    run(conn, auto_config, blob_dir, {S.WRITE: _guard(write)}, times=3)
    assert row(conn, job)["state"] == "WRITE"
    assert set(autopilot_log(conn, job)) == {("AUTO_RERUN", "WRITE")}


def test_escalate_from_producing_step_is_not_approved(conn, auto_config, blob_dir):
    job = new_job(conn, state=S.SERP_INPUT)
    run(conn, auto_config, blob_dir, {S.SERP_INPUT: lambda ctx: StepResult(O.ESCALATE, "SERP_MISSING: thiếu key")})
    assert row(conn, job)["state"] == "SERP_INPUT" and autopilot_log(conn, job) == [("AUTO_RERUN", "SERP_INPUT")]


def test_rerun_stops_after_rerun_max(conn, auto_config, blob_dir):
    auto_config["automation"]["rerun_max"] = 2
    job = new_job(conn)

    def broken(ctx):
        raise KeyError("mode")  # lỗi lập trình: chạy lại bao nhiêu lần cũng vậy

    run(conn, auto_config, blob_dir, {S.TRIAGE_AI: broken}, times=5)
    r = row(conn, job)
    assert r["state"] == "NEEDS_HUMAN" and r["resume_state"] == "TRIAGE_AI"
    assert autopilot_log(conn, job) == [("AUTO_RERUN", "TRIAGE_AI")] * 2
    assert "autopilot dừng" in r["last_error"]


def test_ymyl_final_gate_is_left_for_a_human(conn, auto_config, blob_dir):
    job = new_job(conn, state=S.FINAL_GATE)
    run(conn, auto_config, blob_dir, {S.FINAL_GATE: lambda ctx: StepResult(O.ESCALATE, "YMYL: bài cần người duyệt cuối")})
    assert row(conn, job)["state"] == "NEEDS_HUMAN" and autopilot_log(conn, job) == []


def test_publish_requires_final_package(conn, auto_config, blob_dir):
    from pipeline.steps import HANDLERS
    job = new_job(conn, state=S.PUBLISH)
    run(conn, auto_config, blob_dir, {S.PUBLISH: HANDLERS[S.PUBLISH]})
    assert row(conn, job)["state"] == "PUBLISH"  # chạy lại, không sang GSC_28D/DONE
    assert conn.execute("SELECT count(*) AS n FROM publications").fetchone()["n"] == 0


def test_monthly_cap_pauses_jobs(conn, auto_config, blob_dir):
    auto_config["cost"]["monthly_cap_usd"] = 1.0
    spent = new_job(conn, state=S.DONE)
    conn.execute("INSERT INTO prompt_runs (job_id, step, prompt_id, prompt_version, provider, model, params, "
                 "idempotency_key, cost_usd, status) VALUES (%s, 's', 'P', '1', 'anthropic', 'm', '{}', 'k', 1.5, 'ok')",
                 (spent,))
    job = new_job(conn)
    called = []
    run(conn, auto_config, blob_dir, {S.TRIAGE_AI: lambda ctx: called.append(1) or StepResult(O.DONE)})
    r = row(conn, job)
    assert not called and r["state"] == "NEEDS_HUMAN" and r["needs_human_reason"].startswith("PAUSED_BUDGET")
    assert autopilot_log(conn, job) == []


# ---------- runner: lượt gọi hỏng vẫn tính tiền ----------

def _billed(cost):
    return LLMResult(text="", stop_reason="max_tokens", model="m", tokens_in=100, tokens_out=2000, cost_usd=cost)


def test_truncated_and_refused_calls_are_billed(conn, config, blob_dir):
    job = queue.create_job(conn, topic="t", author_id="a", created_by="x", cost_cap_usd=3)
    prov = ScriptedProvider([Truncated("x").with_billing(_billed(0.3)), Truncated("x").with_billing(_billed(0.45))])
    with pytest.raises(Truncated):
        make_runner(conn, config, blob_dir, prov).run(job_id=job, step="s", prompt_id="B0-H", variables=TRIAGE_VARS,
                                                      input_hashes=[])
    with pytest.raises(Refused):
        make_runner(conn, config, blob_dir, ScriptedProvider([Refused("no").with_billing(_billed(0.05))])).run(
            job_id=job, step="s2", prompt_id="B0-H", variables=TRIAGE_VARS, input_hashes=[])
    assert float(conn.execute("SELECT cost_usd FROM jobs WHERE id = %s", (job,)).fetchone()["cost_usd"]) == pytest.approx(0.8)
    statuses = [r["status"] for r in conn.execute("SELECT status FROM prompt_runs WHERE job_id = %s ORDER BY created_at", (job,))]
    assert statuses == ["truncated", "truncated", "refused"]


def test_cached_ok_that_no_longer_validates_is_replaced(conn, config, blob_dir):
    """Output từng đạt nay sai (validator siết lại): lượt gọi mới đạt không được đụng unique index prompt_runs_idem_ok."""
    job = queue.create_job(conn, topic="t", author_id="a", created_by="x", cost_cap_usd=3)
    kw = dict(job_id=job, step="s", prompt_id="B0-H", variables=TRIAGE_VARS, input_hashes=[])
    make_runner(conn, config, blob_dir, ScriptedProvider([GOOD])).run(**kw)
    old = conn.execute("SELECT id FROM prompt_runs WHERE job_id = %s", (job,)).fetchone()["id"]
    bad_uri = LocalBlobStore(blob_dir).put(b'{"branch_suggest": "C"}', "0" * 64)
    conn.execute("UPDATE prompt_runs SET response_raw_uri = %s WHERE id = %s", (bad_uri, old))  # giả lập output nay không đạt
    out = make_runner(conn, config, blob_dir, ScriptedProvider([GOOD])).run(**kw)
    assert out.parsed["branch_suggest"] == "A" and not out.cached
    rows = {r["status"] for r in conn.execute("SELECT status FROM prompt_runs WHERE job_id = %s", (job,))}
    assert rows == {"ok", "invalid"}


# ---------- regex số liệu ----------

@pytest.mark.parametrize("text", ["Có 5 triệu chứng thường gặp", "Anh ấy có 2 đồng hồ", "Nhóm có 3 đồng nghiệp",
                                  "So sánh 2 tỉ lệ này", "Thắng 2 tỷ số liên tiếp"])
def test_compound_words_are_not_currency(text):
    assert [h.kind for h in scan_numbers(text)] == ["NUM"]


@pytest.mark.parametrize("text,kind,key", [
    ("giá 50K một tháng", "CUR", "num:50000"),
    ("vay 5 Tỷ đồng", "CUR", "num:5000000000"),
    ("lãi 2.5% mỗi năm", "PCT", "num:2.5"),
    ("lãi 2,5% mỗi năm", "PCT", "num:2.5"),
    ("khoảng 3.5 triệu", "CUR", "num:3500000"),
    ("tăng 1,000,000 đồng", "CUR", "num:1000000"),
    ("tăng 1.000.000 đồng", "CUR", "num:1000000"),
    ("giá 5 triệu đồng", "CUR", "num:5000000"),
    ("còn 2 đồng", "CUR", "num:2"),
])
def test_number_formats(text, kind, key):
    hits = scan_numbers(text)
    assert (hits[0].kind, hits[0].key) == (kind, key)


# ---------- URL nội bộ ----------

@pytest.mark.parametrize("url", ["http://127.0.0.1:55432/", "http://localhost/admin", "http://169.254.169.254/latest/meta-data/",
                                 "http://192.168.1.1/", "http://10.0.0.5/x", "http://[::1]/", "http://[::1", "http:///x"])
def test_private_urls_are_blocked(url):
    assert not is_public_url(url)


def test_verifier_does_not_fetch_private_or_malformed_urls(monkeypatch):
    monkeypatch.delenv("SEO_ALLOW_PRIVATE_URLS", raising=False)
    v = UrlVerifier()
    assert v.fetch_text("http://127.0.0.1:55432/") == (None, "")
    assert v.fetch_text("http://[::1") == (None, "")
    assert not v.verify("http://[::1", "x").ok
    assert v.article_markdown("http://169.254.169.254/latest/") == (None, "")


# ---------- blob khi chuyển máy ----------

def test_blob_uri_from_another_machine_resolves_by_sha(tmp_path):
    store = LocalBlobStore(tmp_path / "blobs")
    sha = "ab" + "0" * 62
    store.put(b"noi dung", sha)
    assert store.get(f"file:///home/may-cu/seo-pipeline/var/blobs/ab/{sha}") == b"noi dung"
    with pytest.raises(OSError):
        store.get("file:///home/may-cu/seo-pipeline/var/blobs/cd/" + "cd" + "1" * 62)
