"""Lấy SERP Google (§6.2). JobInput có SERP nhập tay thì luôn dùng bản đó; không có thì gọi provider trong config.

Serper (serper.dev): top 10 organic + People Also Ask. Không trả AI Overview → aio_present = None ("không rõ").
[CXM] Kiểm tra độ phủ SERP tiếng Việt với vài keyword thật trước khi dựa vào.
"""
from __future__ import annotations

import os
from typing import Protocol

import httpx

from .inputs import SerpIn
from .llm.base import ProviderError, TransientError


class SerpProvider(Protocol):
    def fetch(self, keyword: str) -> SerpIn: ...


class SerperProvider:
    URL = "https://google.serper.dev/search"

    def __init__(self, cfg: dict, http: httpx.Client | None = None):
        self.key = os.environ.get(cfg.get("api_key_env", "SERPER_API_KEY"), "")
        self.gl = cfg.get("gl", "vn")
        self.hl = cfg.get("hl", "vi")
        self.num = cfg.get("num", 10)
        self.location = cfg.get("location")
        self.http = http or httpx.Client(timeout=cfg.get("timeout_seconds", 30))

    def fetch(self, keyword: str, page: int = 1) -> SerpIn:
        if not self.key:
            raise ProviderError("thiếu SERPER_API_KEY")
        body = {"q": keyword, "gl": self.gl, "hl": self.hl, "num": self.num}
        if page > 1:  # trang sau: dùng cho auto_source khi top 10 toàn video/MXH/trang bị chặn
            body["page"] = page
        if self.location:
            body["location"] = self.location
        try:
            r = self.http.post(self.URL, json=body, headers={"X-API-KEY": self.key})
        except httpx.HTTPError as e:
            raise TransientError(f"serper: {e}") from e
        if r.status_code == 429 or r.status_code >= 500:
            raise TransientError(f"serper HTTP {r.status_code}")
        if r.status_code >= 400:
            raise ProviderError(f"serper HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        offset = (page - 1) * self.num
        organic = [{"rank": offset + o.get("position", i + 1), "url": o["link"], "title": o.get("title", ""),
                    "snippet": o.get("snippet", "")}
                   for i, o in enumerate(data.get("organic", [])) if o.get("link")][: self.num]
        paa = [q["question"] for q in data.get("peopleAlsoAsk", []) if q.get("question")]
        return SerpIn(organic=organic, paa=paa, aio_present=None, aio_cited_urls=[], provider="serper")


def make_serp_provider(config: dict) -> SerpProvider | None:
    scfg = config.get("adapters", {}).get("serp", {})
    if scfg.get("provider") == "serper":
        return SerperProvider(scfg.get("serper", {}))
    return None  # manual: bắt buộc nhập tay
