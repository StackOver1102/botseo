import json
from types import SimpleNamespace

import httpx
import pytest

from pipeline.config import load_config
from pipeline.llm.anthropic_provider import AnthropicProvider
from pipeline.llm.base import LLMRequest, Refused, Truncated, api_schema
from pipeline.llm.external import render_prompt_text, strip_fences
from pipeline.llm.gemini_provider import GeminiProvider
from pipeline.llm.openai_provider import OpenAIProvider
from pipeline.llm.runner import choose_model
from pipeline.llm.validators import validate_output
from pipeline.prompts import TemplateError, load_registry, load_schemas, load_shared, render, render_spec

REG, SCHEMAS, SHARED = load_registry(), load_schemas(), load_shared()
CFG = load_config()
PRICING = CFG["pricing"]


def test_registry_complete_and_extends():
    assert len(REG) == 26  # 22 prompt của spec + P10-S + RW, RWA (rework) + P6-A (nhánh A)
    for spec in REG.values():
        assert spec.output == "markdown" or spec.output in SCHEMAS, spec.id
    assert "Google Search" in REG["P4-G"].user_template and "<serp_top10>" in REG["P4-G"].user_template
    assert "<section_hien_tai>" in REG["P6-H"].user_template and "<outline>" in REG["P6-H"].user_template
    assert REG["P6-H"].system_template == REG["P6-S"].system_template


def test_render_includes_hard_rules_and_checks_vars():
    variables = {"JOB_BRIEF": "b", "SOURCE_EXCERPT_MARKED": "s", "TONE_STYLE": "mới", "TONE_EXAMPLE": "hoà",
                 "DATAMARK": "^"}
    system, user = render_spec(REG["B0-H"], variables, SHARED)
    assert "Quy tắc làm việc" in system and "{{" not in system + user
    with pytest.raises(TemplateError, match="JOB_BRIEF"):
        render(REG["B0-H"].user_template, {}, SHARED)


def test_api_schema_strips_unsupported_but_keeps_structure():
    s = api_schema(SCHEMAS["VoiceProfile"])
    dims = s["properties"]["dimensions"]
    assert "minItems" not in dims and "maxItems" not in dims and dims["items"]["additionalProperties"] is False
    assert "$schema" not in s


def test_choose_model_avoids_family():
    assert choose_model(CFG, "gate").provider == "openai"
    alt = choose_model(CFG, "gate", {"openai"})
    assert alt.provider == "gemini"  # họ chạy qua extension web (miễn phí) được ưu tiên hơn Claude API
    assert choose_model(CFG, "gate", {"openai", "gemini"}).provider == "anthropic"


def test_validate_output_schema_enum_casefix_and_custom():
    kw = {"rows": [{"keyword_nfc": "a", "display": "a", "key_khongdau": "a", "intent": "Thong_Tin", "cluster": "",
                    "role": "PRIMARY", "section": "", "volume": 5, "note": ""}], "entities": [], "questions_for_human": []}
    rep = validate_output(json.dumps(kw), SCHEMAS["KeywordMap"], REG["P1"].validators,
                          {"input_rows": [{"keyword_nfc": "a", "volume": 5}]})
    assert rep.ok and rep.parsed["rows"][0]["role"] == "primary"
    rep = validate_output(json.dumps(kw), SCHEMAS["KeywordMap"], REG["P1"].validators,
                          {"input_rows": [{"keyword_nfc": "a", "volume": 6}, {"keyword_nfc": "b", "volume": None}]})
    assert any("thiếu dòng" in e for e in rep.errors) and any("volume" in e for e in rep.errors)
    assert validate_output("{bad", SCHEMAS["KeywordMap"], [], {}).errors[0].startswith("$ | JSON")


def test_gate_postprocessor_confidence_floor():
    review = {"gate": "KW", "checks": [{"check_id": "1", "evidence": "KHÔNG TÌM THẤY", "verdict": "YES"}],
              "decision": "approve", "confidence": 0.5, "revise_instructions": "", "escalate_reason": ""}
    rep = validate_output(json.dumps(review), SCHEMAS["GateReview"], REG["G"].validators,
                          {"source_text": "", "expected_check_ids": ["1"], "min_confidence": 0.8})
    assert rep.ok and rep.parsed["decision"] == "escalate"
    review["checks"][0]["verdict"] = "NO"
    review["confidence"] = 0.9
    rep = validate_output(json.dumps(review), SCHEMAS["GateReview"], REG["G"].validators,
                          {"source_text": "", "expected_check_ids": ["1"]})
    assert any("không được approve" in e for e in rep.errors)


# ---------- provider (không gọi mạng) ----------

REQ = LLMRequest(prompt_id="P1", provider="anthropic", model="claude-sonnet-5", system="sys", user="hi",
                 schema={"type": "object", "properties": {"a": {"type": "string", "maxLength": 3}},
                         "required": ["a"], "additionalProperties": False}, effort="medium", cache_system=True)


class FakeStream:
    def __init__(self, msg, sink, kwargs):
        self.msg = msg
        sink.append(kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.msg


def fake_client(msg, sink):
    return SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: FakeStream(msg, sink, kw)))


def anthropic_msg(stop="end_turn", text='{"a":"x"}'):
    usage = SimpleNamespace(input_tokens=1000, output_tokens=500, cache_read_input_tokens=2000, cache_creation_input_tokens=0)
    return SimpleNamespace(id="m1", model="claude-sonnet-5", stop_reason=stop, stop_details=None,
                           content=[SimpleNamespace(type="text", text=text)], usage=usage)


def test_anthropic_params_and_cost():
    sent = []
    r = AnthropicProvider({}, PRICING, fake_client(anthropic_msg(), sent)).complete(REQ, "k")
    p = sent[0]
    assert "thinking" not in p and "temperature" not in p
    assert p["output_config"]["effort"] == "medium"
    assert "maxLength" not in json.dumps(p["output_config"]["format"]["schema"])
    assert p["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert r.text == '{"a":"x"}' and r.cost_usd == pytest.approx((1000 * 2 + 2000 * 0.2 + 500 * 10) / 1e6)


def test_anthropic_refusal_and_truncation():
    with pytest.raises(Refused):
        AnthropicProvider({}, PRICING, fake_client(anthropic_msg("refusal"), [])).complete(REQ, "k")
    with pytest.raises(Truncated):
        AnthropicProvider({}, PRICING, fake_client(anthropic_msg("max_tokens"), [])).complete(REQ, "k")


def mock_http(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_openai_request_and_parse(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "id": "r1", "model": "gpt-6-luna", "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": '{"a":"x"}', "annotations": []}]}],
            "usage": {"input_tokens": 1000, "output_tokens": 100, "input_tokens_details": {"cached_tokens": 400}}})

    req = LLMRequest(**{**REQ.__dict__, "provider": "openai", "model": "gpt-6-luna", "grounding": True})
    r = OpenAIProvider(CFG["providers"]["openai"], PRICING, mock_http(handler)).complete(req, "k")
    b = seen["body"]
    assert b["text"]["format"]["strict"] is True and b["text"]["format"]["name"] == "output"
    assert b["reasoning"] == {"effort": "medium"} and b["tools"] == [{"type": "web_search"}]
    assert r.text == '{"a":"x"}' and r.tokens_in == 600 and r.tokens_cached_read == 400


def test_openai_refusal(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    body = {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]}
    with pytest.raises(Refused):
        OpenAIProvider({}, PRICING, mock_http(lambda r: httpx.Response(200, json=body))).complete(REQ, "k")


def test_gemini_request_and_grounding(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    seen = {}

    def handler(request):
        seen["url"], seen["body"] = str(request.url), json.loads(request.content)
        return httpx.Response(200, json={
            "candidates": [{"finishReason": "STOP",
                            "content": {"parts": [{"text": "nghĩ", "thought": True}, {"text": '{"a":"x"}'}]},
                            "groundingMetadata": {"groundingChunks": [{"web": {"uri": "https://a.vn", "title": "A"}}]}}],
            "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 20, "thoughtsTokenCount": 30}})

    req = LLMRequest(**{**REQ.__dict__, "provider": "gemini", "model": "gemini-3.1-pro-preview", "effort": "high",
                        "grounding": True})
    r = GeminiProvider({}, PRICING, mock_http(handler)).complete(req, "k")
    g = seen["body"]["generationConfig"]
    assert seen["url"].endswith("/models/gemini-3.1-pro-preview:generateContent")
    assert g["responseMimeType"] == "application/json" and g["thinkingConfig"] == {"thinkingLevel": "high"}
    assert seen["body"]["tools"] == [{"google_search": {}}]
    assert r.text == '{"a":"x"}' and r.citations == [{"url": "https://a.vn", "title": "A"}] and r.tokens_out == 50


def test_external_prompt_text_and_fences():
    text = render_prompt_text(REQ)
    assert text.startswith("<system>\nsys") and "<output_schema>" in text
    assert strip_fences('```json\n{"a":1}\n```') == '{"a":1}'


def test_web_output_with_unescaped_quotes_is_repaired_by_schema():
    """ChatGPT/Gemini web trích JSON làm evidence mà không escape dấu ngoặc kép (lỗi gặp khi chạy thật)."""
    bad = ('{"gate":"KW","checks":[{"check_id":"KW1","evidence":""role": "primary"","verdict":"YES"},'
           '{"check_id":"KW2","evidence":"vay "online"","verdict":"NO"}],"decision":"revise","confidence":0.9,'
           '"revise_instructions":"bỏ câu hỏi "thừa"","escalate_reason":""}')
    strict = validate_output(bad, SCHEMAS["GateReview"], [], {})
    assert not strict.ok and "gần:" in strict.errors[0]
    rep = validate_output(bad, SCHEMAS["GateReview"], [], {}, lenient_json=True)
    assert rep.ok and [c["verdict"] for c in rep.parsed["checks"]] == ["YES", "NO"]
    assert rep.parsed["checks"][1]["evidence"] == 'vay "online"'
    assert rep.parsed["revise_instructions"] == 'bỏ câu hỏi "thừa"'


def test_evidence_match_ignores_quotes():
    rep = validate_output('{"items":[{"check_id":"a","evidence":"role: primary","verdict":"YES","severity":"thap",'
                          '"fix_hint":"","location":"S01"}],"pass_id":"P8a","blocking_fail":false}',
                          SCHEMAS["Verdict"], ["evidence_substring_or_notfound"], {"source_text": '{"role": "primary"}'})
    assert rep.ok


def test_p10s_quote_must_be_in_fetched_source():
    src = {"C010": {"https://a.gov.vn/x": "Thông tư 29/2026/TT-NHNN quy định thứ tự thu nợ gốc trước, nợ lãi sau."}}
    def fc(url, quote, verdict="DUNG"):
        return json.dumps({"items": [{"claim_id": "C010", "atomic_claim": "x", "verdict": verdict, "current_value": "",
                                      "sources": [{"url": url, "date": "", "quote": quote, "tier": 1}] if url else [],
                                      "conflict_note": "", "suggested_action": ""}]})
    v = lambda t: validate_output(t, SCHEMAS["FactCheck"], REG["P10-S"].validators, {"provided_sources": src, "datamark": "¤"})
    assert v(fc("https://a.gov.vn/x", "thu nợ¤gốc trước, nợ lãi sau")).ok          # datamark trong quote được bỏ
    # URL chỉ khác dấu/dấu "/" cuối vẫn khớp nguồn đã cho, url được chuẩn hoá về source_url
    rep = v(fc("https://a.gov.vn/x/", "thu nợ gốc trước"))
    assert rep.ok and rep.parsed["items"][0]["sources"][0]["url"] == "https://a.gov.vn/x"
    # quote không có trong nguồn / URL lạ: code bỏ nguồn và hạ về KHONG_XAC_MINH_DUOC thay vì repair qua web
    for bad in (fc("https://a.gov.vn/x", "lãi tối đa 20%/năm"), fc("https://khac.vn/y", "thu nợ gốc trước")):
        rep = v(bad)
        assert rep.ok and rep.parsed["items"][0]["verdict"] == "KHONG_XAC_MINH_DUOC" and not rep.parsed["items"][0]["sources"]
    assert v(fc(None, "")).parsed["items"][0]["verdict"] == "KHONG_XAC_MINH_DUOC"
    assert v(fc(None, "", "KHONG_XAC_MINH_DUOC")).ok


def test_p4g_coverage_outside_serp_is_dropped():
    from pipeline.llm.validators import _prune_coverage
    out = {"coverage": [{"url": "https://www.a.vn/1-trận-bóng/"}, {"url": "https://bia.vn/x"}]}
    out, notes = _prune_coverage(out, {"serp_urls": ["https://a.vn/1-tran-bong"]})
    assert [c["url"] for c in out["coverage"]] == ["https://www.a.vn/1-trận-bóng/"] and notes


def test_placeholders_rejected_in_article_and_edits():
    draft = {"body_markdown": "<!-- S01 -->\n## A\nLãi suất [CẦN NGƯỜI: bổ sung mức trần].", "keyword_placement": [],
             "can_nguoi": [], "change_log": []}
    rep = validate_output(json.dumps(draft, ensure_ascii=False), SCHEMAS["ArticleDraft"], REG["P6-S"].validators, {})
    assert any("không placeholder" in e for e in rep.errors)
    assert "no_placeholders" in REG["P6-H"].validators  # P6-H kế thừa validator của P6-S
    edits = {"edits": [{"check_id": "P9.1", "error_type": 0, "find": "x", "replace": "[TODO: điền]", "reason": ""}]}
    rep = validate_output(json.dumps(edits), SCHEMAS["Edits"], ["no_placeholders"], {})
    assert rep.errors


def test_evidence_multi_quote_joined_by_pipe():
    """ChatGPT gộp nhiều câu trích bằng ' | ' (gặp khi chạy thật): từng đoạn là chuỗi con thì hợp lệ."""
    from pipeline.llm.validators import _evidence_ok
    src = "Câu một của bài viết. Câu hai nằm ở đoạn khác."
    assert _evidence_ok(src, "Câu một của bài viết. | Câu hai nằm ở đoạn khác.")
    assert _evidence_ok(src, "Câu một của bài … nằm ở đoạn khác")
    assert not _evidence_ok(src, "Câu một của bài viết. | Câu bịa ra không có")


def test_coverage_url_matching_is_lenient():
    """Gặp khi chạy thật: Gemini thêm dấu vào slug không dấu, SERP có ?srsltid=…, mã hoá %XX."""
    from pipeline.llm.validators import url_key
    assert url_key("https://bf365.vn/news/1-tran-bong-ro") == url_key("https://bf365.vn/news/1-trận-bóng-rổ/")
    assert url_key("https://www.decathlon.vn/blog/x/?srsltid=AU7") == url_key("http://decathlon.vn/blog/x")
    assert url_key("https://a.vn/1-tr%E1%BA%ADn") == url_key("https://a.vn/1-trận")


def test_failed_api_calls_carry_their_cost(monkeypatch):
    """Lượt gọi bị cắt / bị từ chối vẫn bị hãng tính tiền: lỗi phải mang theo usage để runner cộng vào chi phí job."""
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    pricing = {"gpt-6-luna": {"input": 1.0, "output": 10.0}, "gemini-3.8-flash": {"input": 1.0, "output": 10.0}}

    def openai(body):
        req = LLMRequest(**{**REQ.__dict__, "provider": "openai", "model": "gpt-6-luna"})
        return OpenAIProvider({}, pricing, mock_http(lambda r: httpx.Response(200, json=body))).complete(req, "k")

    usage = {"input_tokens": 1000, "output_tokens": 2000}
    with pytest.raises(Truncated) as e:
        openai({"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}, "usage": usage})
    assert e.value.billed.cost_usd == pytest.approx(0.021) and e.value.billed.tokens_out == 2000
    with pytest.raises(Refused) as e:
        openai({"status": "completed", "usage": usage,
                "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]})
    assert e.value.billed.cost_usd == pytest.approx(0.021)

    def gemini(finish):
        req = LLMRequest(**{**REQ.__dict__, "provider": "gemini", "model": "gemini-3.8-flash"})
        body = {"candidates": [{"finishReason": finish, "content": {"parts": []}}],
                "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 1500, "thoughtsTokenCount": 500}}
        return GeminiProvider({}, pricing, mock_http(lambda r: httpx.Response(200, json=body))).complete(req, "k")

    with pytest.raises(Truncated) as e:
        gemini("MAX_TOKENS")
    assert e.value.billed.cost_usd == pytest.approx(0.021) and e.value.billed.tokens_out == 2000
    with pytest.raises(Refused) as e:
        gemini("SAFETY")
    assert e.value.billed.cost_usd == pytest.approx(0.021)
