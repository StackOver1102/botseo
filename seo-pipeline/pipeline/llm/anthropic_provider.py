"""Claude qua Messages API: structured output bằng output_config.format, effort, prompt caching.

Opus 5.5: thinking luôn bật (không gửi tham số thinking), effort mặc định là medium nên luôn đặt rõ.
Không dùng temperature/top_p/prefill (400 trên các model này).
"""
from __future__ import annotations

import time

import anthropic

from .base import LLMRequest, LLMResult, ProviderError, Refused, TransientError, Truncated, api_schema, cost_usd


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, cfg: dict, pricing: dict, client: anthropic.Anthropic | None = None):
        self.pricing = pricing
        self.client = client or anthropic.Anthropic(
            max_retries=cfg.get("max_retries", 2), timeout=cfg.get("timeout_seconds", 600))

    def _params(self, req: LLMRequest) -> dict:
        system_block = {"type": "text", "text": req.system}
        if req.cache_system:
            system_block["cache_control"] = {"type": "ephemeral", **({"ttl": "1h"} if req.cache_ttl == "1h" else {})}
        # Haiku 4.5 không có tham số effort (và không bật thinking) — chỉ gửi khi model hỗ trợ
        output_config: dict = {} if "haiku" in req.model and not req.effort else {"effort": req.effort or "medium"}
        if req.schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": api_schema(req.schema)}
        return {
            "model": req.model,
            "max_tokens": req.max_output_tokens,
            "system": [system_block],
            "messages": [{"role": "user", "content": req.user}],
            "output_config": output_config,
        }

    def complete(self, req: LLMRequest, key: str) -> LLMResult:
        started = time.monotonic()
        try:
            # Luôn stream: max_tokens lớn (P6 24K) dễ chạm timeout HTTP nếu gọi thường
            with self.client.messages.stream(**self._params(req)) as stream:
                msg = stream.get_final_message()
        except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.APITimeoutError) as e:
            raise TransientError(str(e)) from e
        except anthropic.APIStatusError as e:
            if e.status_code >= 500:
                raise TransientError(str(e)) from e
            raise ProviderError(f"{e.status_code}: {e.message}") from e

        u = msg.usage
        cached = u.cache_read_input_tokens or 0
        written = u.cache_creation_input_tokens or 0
        if msg.stop_reason in ("refusal", "max_tokens"):
            # Lượt gọi vẫn bị tính tiền: gắn usage vào lỗi để runner cộng vào chi phí job (trần chi phí mới chặn được)
            billed = LLMResult(
                text="", stop_reason=msg.stop_reason, model=msg.model, tokens_in=u.input_tokens,
                tokens_cached_read=cached, tokens_cache_write=written, tokens_out=u.output_tokens,
                cost_usd=cost_usd(self.pricing, req.model, u.input_tokens, cached, written, u.output_tokens, req.cache_ttl),
                latency_ms=int((time.monotonic() - started) * 1000), raw={"id": msg.id})
            if msg.stop_reason == "refusal":
                details = getattr(msg, "stop_details", None)
                raise Refused(getattr(details, "category", None) or "refusal").with_billing(billed)
            raise Truncated(f"max_tokens={req.max_output_tokens}").with_billing(billed)

        text = next((b.text for b in msg.content if b.type == "text"), "")
        return LLMResult(
            text=text,
            stop_reason=msg.stop_reason or "",
            model=msg.model,
            tokens_in=u.input_tokens,
            tokens_cached_read=cached,
            tokens_cache_write=written,
            tokens_out=u.output_tokens,
            cost_usd=cost_usd(self.pricing, req.model, u.input_tokens, cached, written, u.output_tokens, req.cache_ttl),
            latency_ms=int((time.monotonic() - started) * 1000),
            raw={"id": msg.id, "request_id": getattr(msg, "_request_id", None)},
        )

    def count_tokens(self, req: LLMRequest) -> int:
        """Đo ngân sách token bằng endpoint của hãng (tokenizer mới, không dùng tiktoken)."""
        p = self._params(req)
        return self.client.messages.count_tokens(
            model=p["model"], system=p["system"], messages=p["messages"]).input_tokens
