"""Gemini qua generateContent (HTTP thuần): responseJsonSchema, thinkingLevel, google_search.

Dùng generateContent thay vì Interactions API vì Batch và explicit caching chỉ có ở đây (§6.1).
[CXM] Structured output + google_search trên Gemini 3 là Preview; kiểm tra lại trước khi bật mode api.
Nếu hãng từ chối kết hợp này, chạy grounded lấy văn bản rồi cho P4-D/P10 trích JSON (fallback 2 bước).
"""
from __future__ import annotations

import os
import time

import httpx

from .base import LLMRequest, LLMResult, ProviderError, Refused, TransientError, Truncated, api_schema, cost_usd

_BLOCKED = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"}


class GeminiProvider:
    name = "gemini"

    def __init__(self, cfg: dict, pricing: dict, http: httpx.Client | None = None):
        self.pricing = pricing
        self.base = cfg.get("api_base", "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
        self.key = os.environ.get(cfg.get("api_key_env", "GEMINI_API_KEY"), "")
        self.http = http or httpx.Client(timeout=cfg.get("timeout_seconds", 600))
        self.retries = cfg.get("max_retries", 2)

    def _body(self, req: LLMRequest) -> dict:
        gen: dict = {"maxOutputTokens": req.max_output_tokens,
                     "thinkingConfig": {"thinkingLevel": req.effort or "high"}}
        if req.schema is not None:
            gen["responseMimeType"] = "application/json"
            gen["responseJsonSchema"] = api_schema(req.schema)
        body: dict = {
            "systemInstruction": {"parts": [{"text": req.system}]},
            "contents": [{"role": "user", "parts": [{"text": req.user}]}],
            "generationConfig": gen,
        }
        if req.grounding:
            body["tools"] = [{"google_search": {}}]
        return body

    def complete(self, req: LLMRequest, key: str) -> LLMResult:
        if not self.key:
            raise ProviderError("thiếu GEMINI_API_KEY")
        started = time.monotonic()
        data = self._post(req.model, self._body(req))

        if (data.get("promptFeedback") or {}).get("blockReason"):
            raise Refused(data["promptFeedback"]["blockReason"])
        cands = data.get("candidates") or []
        if not cands:
            raise ProviderError("không có candidate")
        cand = cands[0]
        finish = cand.get("finishReason", "")
        u = data.get("usageMetadata") or {}
        cached = u.get("cachedContentTokenCount", 0)
        t_in = u.get("promptTokenCount", 0) - cached
        t_out = u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0)
        if finish in _BLOCKED or finish == "MAX_TOKENS":
            # Lượt gọi hỏng vẫn bị tính tiền → runner cộng vào chi phí job
            billed = LLMResult(text="", stop_reason=finish, model=data.get("modelVersion", req.model), tokens_in=t_in,
                               tokens_cached_read=cached, tokens_out=t_out,
                               cost_usd=cost_usd(self.pricing, req.model, t_in, cached, 0, t_out),
                               latency_ms=int((time.monotonic() - started) * 1000),
                               raw={"response_id": data.get("responseId")})
            if finish in _BLOCKED:
                raise Refused(finish).with_billing(billed)
            raise Truncated(f"maxOutputTokens={req.max_output_tokens}").with_billing(billed)
        text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []) if not p.get("thought"))

        grounding = cand.get("groundingMetadata") or {}
        citations = [{"url": c["web"]["uri"], "title": c["web"].get("title")}
                     for c in grounding.get("groundingChunks", []) if "web" in c]
        return LLMResult(
            text=text, stop_reason=finish, model=data.get("modelVersion", req.model),
            tokens_in=t_in, tokens_cached_read=cached, tokens_out=t_out,
            cost_usd=cost_usd(self.pricing, req.model, t_in, cached, 0, t_out),
            latency_ms=int((time.monotonic() - started) * 1000), citations=citations,
            raw={"response_id": data.get("responseId"), "search_queries": grounding.get("webSearchQueries")},
        )

    def _post(self, model: str, body: dict) -> dict:
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                r = self.http.post(f"{self.base}/models/{model}:generateContent", json=body,
                                   headers={"x-goog-api-key": self.key})
            except httpx.HTTPError as e:
                last = e
            else:
                if r.status_code == 429 or r.status_code >= 500:
                    last = TransientError(f"{r.status_code}: {r.text[:300]}")
                elif r.status_code >= 400:
                    raise ProviderError(f"{r.status_code}: {r.text[:500]}")
                else:
                    return r.json()
            time.sleep(min(2 * 4 ** attempt, 30))
        raise TransientError(str(last))
