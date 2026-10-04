"""Kiểu dữ liệu và lỗi chung cho mọi provider LLM (§6)."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Protocol

FAMILIES = ("anthropic", "openai", "gemini")


@dataclass
class LLMRequest:
    prompt_id: str
    provider: str
    model: str
    system: str
    user: str
    schema: dict | None = None  # None → output Markdown
    schema_name: str = "output"
    max_output_tokens: int = 8000
    effort: str | None = None  # anthropic/openai: low|medium|high…; gemini: thinking_level
    grounding: bool = False  # google_search / web_search
    cache_system: bool = False
    cache_ttl: str = "5m"


@dataclass
class LLMResult:
    text: str
    stop_reason: str
    model: str
    tokens_in: int = 0
    tokens_cached_read: int = 0
    tokens_cache_write: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    citations: list[dict] = field(default_factory=list)
    raw: dict | None = None


class LLMError(Exception):
    """billed: lượt gọi đã bị hãng tính tiền dù không dùng được (Truncated, Refused) — provider gắn LLMResult
    (text rỗng, đủ token + cost) để runner ghi prompt_runs và cộng vào chi phí job; None = không tốn tiền."""
    billed: "LLMResult | None" = None

    def with_billing(self, result: "LLMResult") -> "LLMError":
        self.billed = result
        return self


class TransientError(LLMError):
    """429/5xx/timeout/mạng — sau khi provider đã tự retry; executor sẽ thử lại cả bước."""


class Refused(LLMError):
    """Model từ chối — không repair, chuyển cho người."""


class Truncated(LLMError):
    """Chạm max_output_tokens — chạy lại với giới hạn ×1,5, không parse output bị cắt."""


class ProviderError(LLMError):
    """Lỗi không nên retry (400, sai cấu hình, thiếu key)."""


class AwaitingExternal(LLMError):
    """Đã giao task cho extension, chưa có kết quả — job được hẹn chạy lại, không tính là lỗi."""

    def __init__(self, task_id: str, poll_seconds: int):
        super().__init__(f"chờ external task {task_id}")
        self.task_id = task_id
        self.poll_seconds = poll_seconds


class Provider(Protocol):
    name: str

    def complete(self, req: LLMRequest, key: str) -> LLMResult: ...


def cost_usd(pricing: dict, model: str, tokens_in: int, cached_read: int, cache_write: int, tokens_out: int,
             cache_ttl: str = "5m") -> float:
    """tokens_in là phần input không cache (giá đầy đủ)."""
    p = pricing.get(model)
    if not p:
        return 0.0
    write_price = p.get(f"cache_write_{cache_ttl}", p.get("cache_write_5m", p["input"] * 1.25))
    return (tokens_in * p["input"] + cached_read * p.get("cache_read", p["input"] * 0.1)
            + cache_write * write_price + tokens_out * p["output"]) / 1_000_000


# Ràng buộc mà structured output của các hãng không hỗ trợ (hoặc hỗ trợ không đều).
# Bỏ khỏi schema gửi đi; code vẫn validate bằng schema đầy đủ sau khi nhận.
_UNSUPPORTED = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
                "minLength", "maxLength", "pattern", "maxItems", "uniqueItems", "$schema"}


def api_schema(schema: dict) -> dict:
    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                if k in _UNSUPPORTED:
                    continue
                if k == "minItems" and isinstance(v, int) and v > 1:
                    continue
                out[k] = walk(v) if k not in ("enum", "const", "required") else copy.deepcopy(v)
            return out
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node
    return walk(schema)
