"""Dữ liệu đầu vào của một job và hàm tạo job.

Text bài nguồn nhánh B không nằm trong JobInput (artifact bất biến) mà lưu ở bảng source_texts để xoá được (ADR-6).
"""
from __future__ import annotations

import hashlib
from typing import Literal
from uuid import UUID

import psycopg
from pydantic import BaseModel, Field

from . import queue
from .artifacts import ArtifactStore


class KeywordIn(BaseModel):
    keyword: str
    volume: int | None = None


class SerpOrganic(BaseModel):
    rank: int
    url: str
    title: str = ""
    snippet: str = ""


class SerpIn(BaseModel):
    """Định dạng upload tay (§6.2) hoặc từ SerpProvider."""
    organic: list[SerpOrganic]
    paa: list[str] = Field(default_factory=list)
    aio_present: bool | None = None
    aio_cited_urls: list[str] = Field(default_factory=list)
    provider: str = "manual"


class PageIn(BaseModel):
    url: str
    title: str


class AuthorIn(BaseModel):
    name: str
    url: str | None = None
    type: Literal["Person", "Organization"] = "Person"  # @type của author trong JSON-LD (ban biên tập = Organization)


class JobInput(BaseModel):
    topic: str
    brief: str = ""
    branch: Literal["A", "B"] | None = None  # để trống → theo kết quả triage
    mode: Literal["lite", "standard", "heavy"] | None = None
    ymyl: bool | None = None
    article_md: str | None = None  # nhánh A: bài hiện tại của site
    voice_samples_md: str | None = None  # nhánh B: 2–3 bài của chính tác giả
    voice_profile: dict | None = None  # Lite: hồ sơ giọng có sẵn
    keywords: list[KeywordIn]
    serp: SerpIn | None = None
    paa_extra: list[str] = Field(default_factory=list)  # PAA nhập tay, gộp với SERP lấy tự động (Serper không trả PAA tiếng Việt)
    internal_pages: list[PageIn] = Field(default_factory=list)
    existing_titles: list[str] = Field(default_factory=list)
    author: AuthorIn | None = None
    target_words: int = 2000
    source_urls: list[str] = Field(default_factory=list)  # nhánh B: URL các bài nguồn (auto_source), để truy vết
    urgent: bool = False


def create_job_with_input(conn: psycopg.Connection, artifacts: ArtifactStore, inp: JobInput, *, author_id: str,
                          created_by: str, config: dict, source_text: str | None = None,
                          source_texts: list[str] | None = None, flow: str = "full", site_id: str | None = None) -> UUID:
    """source_texts: nhiều bài nguồn nhánh B (mỗi bài một dòng → fingerprint/containment theo từng nguồn)."""
    caps = config["cost"]["cap_usd_per_job"]
    cap = caps.get("rework", 0.5) if flow == "rework" else caps.get(inp.mode or "heavy")  # chưa biết mode → trần cao nhất
    with conn.transaction():
        job_id = queue.create_job(conn, topic=inp.topic, author_id=author_id, created_by=created_by, cost_cap_usd=cap,
                                  urgent=inp.urgent, branch=inp.branch, mode=inp.mode, ymyl=inp.ymyl, flow=flow,
                                  site_id=site_id)
        artifacts.save(conn, job_id, "JobInput", inp.model_dump(mode="json"), f"human:{created_by}")
        for text in ([source_text] if source_text else []) + (source_texts or []):
            conn.execute("INSERT INTO source_texts (job_id, sha256, text) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                         (job_id, hashlib.sha256(text.encode("utf-8")).hexdigest(), text))
    return job_id
