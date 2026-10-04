"""GPT qua Responses API (HTTP thuần): text.format json_schema strict, reasoning.effort, web_search.

[CXM] Hình dạng request/response theo tài liệu Responses API; kiểm tra lại với tài khoản thật trước khi bật mode api.
"""
from __future__ import annotations

import os
import time

import httpx

from .base import LLMRequest, LLMResult, ProviderError, Refused, TransientError, Truncated, api_schema, cost_usd


class OpenAIProvider:
    name = "openai"

    def __init__(self, cfg: dict, pricing: dict, http: httpx.Client | None = None):
        self.pricing = pricing
        self.base = cfg.get("api_base", "https://api.openai.com/v1").rstrip("/")
        self.key = os.environ.get(cfg.get("api_key_env", "OPENAI_API_KEY"), "")
        self.http = http or httpx.Client(timeout=cfg.get("timeout_seconds", 600))
        self.retries = cfg.get("max_retries", 2)

    def _body(self, req: LLMRequest) -> dict:
        body: dict = {
            "model": req.model,
            "instructions": req.system,
            "input": [{"role": "user", "content": req.user}],
            "max_output_tokens": req.max_output_tokens,
            "reasoning": {"effort": req.effort or "medium"},
        }
        if req.schema is not None:
            body["text"] = {"format": {"type": "json_schema", "name": req.schema_name,
                                       "schema": api_schema(req.schema), "strict": True}}
        if req.grounding:
            body["tools"] = [{"type": "web_search"}]
        return body

    def complete(self, req: LLMRequest, key: str) -> LLMResult:
        if not self.key:
            raise ProviderError("thiếu OPENAI_API_KEY")
        started = time.monotonic()
        data = self._post(self._body(req))

        u = data.get("usage") or {}
        cached = (u.get("input_tokens_details") or {}).get("cached_tokens", 0)
        t_in = u.get("input_tokens", 0) - cached
        t_out = u.get("output_tokens", 0)

        def billed(stop: str) -> LLMResult:  # lượt gọi hỏng vẫn bị tính tiền → runner cộng vào chi phí job
            return LLMResult(text="", stop_reason=stop, model=data.get("model", req.model), tokens_in=t_in,
                             tokens_cached_read=cached, tokens_out=t_out,
                             cost_usd=cost_usd(self.pricing, req.model, t_in, cached, 0, t_out),
                             latency_ms=int((time.monotonic() - started) * 1000), raw={"id": data.get("id")})

        if data.get("status") == "incomplete":
            reason = (data.get("incomplete_details") or {}).get("reason", "")
            if reason == "max_output_tokens":
                raise Truncated(f"max_output_tokens={req.max_output_tokens}").with_billing(billed("max_output_tokens"))
            raise ProviderError(f"incomplete: {reason}")

        text, citations = "", []
        for item in data.get("output", []):
            if item.get("type") != "message":
                continue
            for c in item.get("content", []):
                if c.get("type") == "refusal":
                    raise Refused(c.get("refusal", "refusal")).with_billing(billed("refusal"))
                if c.get("type") == "output_text":
                    text += c.get("text", "")
                    citations += [a for a in c.get("annotations", []) if a.get("type") == "url_citation"]

        return LLMResult(
            text=text, stop_reason=data.get("status", ""), model=data.get("model", req.model),
            tokens_in=t_in, tokens_cached_read=cached, tokens_out=t_out,
            cost_usd=cost_usd(self.pricing, req.model, t_in, cached, 0, t_out),
            latency_ms=int((time.monotonic() - started) * 1000), citations=citations,
            raw={"id": data.get("id")},
        )

    def _post(self, body: dict) -> dict:
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                r = self.http.post(f"{self.base}/responses", json=body,
                                   headers={"Authorization": f"Bearer {self.key}"})
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
