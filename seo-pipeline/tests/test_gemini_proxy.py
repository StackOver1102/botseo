"""Gemini qua proxy cục bộ gemini-web-to-api (providers.gemini.mode: proxy)."""
import json

import httpx
import pytest

from pipeline import queue
from pipeline.llm.base import AwaitingExternal, LLMRequest, ProviderError, TransientError
from pipeline.llm.gemini_proxy_provider import DEEP_RESEARCH_MODEL, GeminiProxyProvider, looks_like_refusal
from pipeline.llm.runner import choose_model, make_provider_factory

CFG = {"proxy_base": "http://proxy.test/gemini/v1beta", "model_map": {"gemini-3.1-pro-preview": "gemini-3.1-pro"},
       "fallback_model": "gemini-3.1-pro", "poll_seconds": 7}
SCHEMA = {"type": "object", "required": ["claims"], "properties": {"claims": {"type": "array"}}}


def answer(text, model="gemini-3.8-flash"):
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "nghĩ", "thought": True}, {"text": text}]},
                                                     "finishReason": "STOP"}],
                                     "usageMetadata": {"promptTokenCount": 0}, "modelVersion": model})


def provider(handler, conn=None, job_id=None):
    return GeminiProxyProvider(CFG, conn, job_id, "GAP_IG_AI", http=httpx.Client(transport=httpx.MockTransport(handler)))


def req(model="gemini-3.8-flash", schema=SCHEMA, **kw):
    return LLMRequest(prompt_id="P2a", provider="gemini", model=model, system="Bạn trích claim.", user="<bai_viet>…</bai_viet>",
                      schema=schema, **kw)


def test_chat_sends_schema_in_prompt_and_strips_fences():
    seen = []

    def handler(request):
        seen.append((request.url.path, json.loads(request.content)))
        return answer('```json\n{"claims": []}\n```')

    res = provider(handler).complete(req(grounding=True), "k")
    path, body = seen[0]
    assert path == "/gemini/v1beta/models/gemini-3.8-flash:generateContent"
    assert body["systemInstruction"]["parts"][0]["text"] == "Bạn trích claim."
    user = body["contents"][0]["parts"][0]["text"]
    assert "<output_schema>" in user and '"claims"' in user and "tìm kiếm web" in user
    assert "generationConfig" not in body and "tools" not in body  # proxy không ép schema, không có google_search
    assert res.text == '{"claims": []}' and res.stop_reason == "external" and res.cost_usd == 0
    assert res.raw == {"web_proxy": True, "requested_model": "gemini-3.8-flash", "fallback": False}


def test_model_map_translates_pipeline_name():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return answer('{"claims": []}', "gemini-3.1-pro")

    res = provider(handler).complete(req("gemini-3.1-pro-preview"), "k")
    assert paths == ["/gemini/v1beta/models/gemini-3.1-pro:generateContent"] and res.model == "gemini-3.1-pro"


@pytest.mark.parametrize("refusal", ["Tôi không thể trợ giúp về điều đó, vì tôi chỉ là một mô hình ngôn ngữ.",
                                     "Tôi không được lập trình để làm điều đó."])
def test_flash_refusal_falls_back_to_pro(refusal):
    paths = []

    def handler(request):
        paths.append(request.url.path.rsplit("/", 1)[1])
        return answer(refusal) if "flash" in request.url.path else answer('{"claims": [1]}', "gemini-3.1-pro")

    res = provider(handler).complete(req(), "k")
    assert paths == ["gemini-3.8-flash:generateContent", "gemini-3.1-pro:generateContent"]
    assert res.text == '{"claims": [1]}' and res.raw["fallback"] and res.model == "gemini-3.1-pro"


def test_refusal_detection():
    assert looks_like_refusal("Là một mô hình ngôn ngữ, tôi không được thiết kế để trợ giúp về điều đó.", False)
    assert looks_like_refusal("Đây là câu trả lời không có JSON", True)
    assert not looks_like_refusal('{"a": 1}', True)
    # Bài Markdown dài có nhắc "mô hình ngôn ngữ" không phải lời từ chối
    assert not looks_like_refusal("# Bài viết\n" + "Nội dung nói về mô hình ngôn ngữ lớn. " * 30, False)
    assert not looks_like_refusal("- Đổi tiêu đề\n- Cập nhật số liệu", False)


def test_markdown_refusal_falls_back_but_pro_refusal_is_returned_once():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return answer("Tôi không thể giúp bạn việc đó.")

    res = provider(handler).complete(req("gemini-3.1-pro-preview", schema=None), "k")
    assert len(calls) == 1 and res.raw["fallback"] is False  # đã là Pro: không gọi lại, validator/repair xử lý tiếp


def test_proxy_errors_map_to_pipeline_errors():
    with pytest.raises(TransientError):
        provider(lambda r: httpx.Response(500, text="BardErrorInfo")).complete(req(), "k")
    with pytest.raises(ProviderError):
        provider(lambda r: httpx.Response(400, text="model lạ")).complete(req(), "k")

    def down(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(TransientError, match="không phản hồi"):
        provider(down).complete(req(), "k")


def test_factory_and_model_choice_use_proxy(conn, config):
    assert config["providers"]["gemini"]["mode"] == "proxy"
    prov = make_provider_factory(conn, config)("gemini", None, "s")
    assert isinstance(prov, GeminiProxyProvider) and prov.fallback_model == "gemini-3.1-pro"
    # Đổi họ (ADR-5): proxy cũng là họ "miễn phí" nên được ưu tiên như extension
    config["providers"]["openai"]["mode"] = "api"
    assert choose_model(config, "writer", avoid_families={"anthropic"}).provider == "gemini"


# ---------- Deep Research ----------

def dr_req():
    return LLMRequest(prompt_id="P4-DR", provider="gemini", model=DEEP_RESEARCH_MODEL, system="",
                      user='Nghiên cứu chủ đề "bóng đá"', schema=None)


def test_deep_research_lifecycle(conn):
    job = queue.create_job(conn, topic="t", author_id="a", created_by="x", cost_cap_usd=3)
    state = {"status": "in_progress", "posts": []}

    def handler(request):
        if request.method == "POST":
            state["posts"].append(json.loads(request.content))
            return httpx.Response(202, json={"id": f"task-{len(state['posts'])}", "status": "in_progress"})
        if state["status"] == "gone":
            return httpx.Response(404, json={"error": "not found"})
        body = {"id": "task-1", "status": state["status"]}
        if state["status"] == "completed":
            body.update(outputs=[{"text": "# Báo cáo\nNội dung [cite: 1]"}], duration_ms=900000,
                        sources=[{"url": "https://www.theifab.com/laws", "title": "IFAB"}, {"title": "không url"}])
        if state["status"] == "failed":
            body["error"] = "hết lượt Deep Research"
        return httpx.Response(200, json=body)

    prov = provider(handler, conn, job)
    with pytest.raises(AwaitingExternal) as wait:
        prov.complete(dr_req(), "key-1")
    assert wait.value.poll_seconds == 7
    assert state["posts"] == [{"input": 'Nghiên cứu chủ đề "bóng đá"', "agent": "deep-research", "background": True,
                               "language": "Vietnamese", "max_sources": 100}]
    row = conn.execute("SELECT * FROM external_tasks WHERE idempotency_key = 'key-1'").fetchone()
    assert (row["status"], row["claimed_by"], row["job_id"], row["result_meta"]) == ("claimed", "gemini-proxy", job,
                                                                                    {"interaction_id": "task-1"})
    assert str(row["id"]) == wait.value.task_id

    with pytest.raises(AwaitingExternal):  # vẫn đang chạy: không tạo lượt mới
        prov.complete(dr_req(), "key-1")
    assert len(state["posts"]) == 1

    state["status"] = "completed"
    res = prov.complete(dr_req(), "key-1")
    assert res.text.startswith("# Báo cáo") and res.text.rstrip().endswith("1. IFAB — https://www.theifab.com/laws")
    assert res.citations == [{"url": "https://www.theifab.com/laws"}] and res.stop_reason == "external"
    assert conn.execute("SELECT status FROM external_tasks WHERE idempotency_key = 'key-1'").fetchone()["status"] == "done"

    state["status"] = "gone"  # đã lưu kết quả: không hỏi proxy nữa
    again = prov.complete(dr_req(), "key-1")
    assert again.text == res.text and len(state["posts"]) == 1


def test_deep_research_restarts_when_proxy_lost_the_task(conn):
    job = queue.create_job(conn, topic="t", author_id="a", created_by="x", cost_cap_usd=3)
    posts = []

    def handler(request):
        if request.method == "POST":
            posts.append(1)
            return httpx.Response(202, json={"id": f"task-{len(posts)}"})
        return httpx.Response(404, json={"error": "not found"})  # proxy restart: bộ lưu trong RAM mất task

    prov = provider(handler, conn, job)
    for _ in range(2):
        with pytest.raises(AwaitingExternal):
            prov.complete(dr_req(), "key-2")
    assert len(posts) == 2
    meta = conn.execute("SELECT result_meta FROM external_tasks WHERE idempotency_key = 'key-2'").fetchone()["result_meta"]
    assert meta == {"interaction_id": "task-2"}


def test_deep_research_failure_then_rerun_starts_new_research(conn):
    job = queue.create_job(conn, topic="t", author_id="a", created_by="x", cost_cap_usd=3)
    posts = []

    def handler(request):
        if request.method == "POST":
            posts.append(1)
            return httpx.Response(202, json={"id": f"task-{len(posts)}"})
        return httpx.Response(200, json={"id": "x", "status": "failed", "error": "Gemini did not return a deep research plan"})

    prov = provider(handler, conn, job)
    with pytest.raises(AwaitingExternal):
        prov.complete(dr_req(), "key-3")
    with pytest.raises(ProviderError, match="did not return a deep research plan"):
        prov.complete(dr_req(), "key-3")
    row = conn.execute("SELECT status, failures FROM external_tasks WHERE idempotency_key = 'key-3'").fetchone()
    assert (row["status"], row["failures"]) == ("failed", 1) and len(posts) == 1
    with pytest.raises(AwaitingExternal):  # bước được chạy lại (autopilot): nghiên cứu lại
        prov.complete(dr_req(), "key-3")
    assert len(posts) == 2
