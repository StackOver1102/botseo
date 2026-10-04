"""Gemini qua proxy cục bộ gemini-web-to-api (mode: proxy) — thay cho extension trên Gemini web.

Proxy (../gemini-web-to-api, chỉ nghe ở 127.0.0.1) dùng cookie Gemini web và mở endpoint kiểu generateContent.
Khác API chính thức:
- Không ép được JSON schema, không có google_search/groundingMetadata, token luôn = 0 → schema được chèn vào prompt
  như nhánh external, output được parse lỏng (stop_reason "external"), chi phí = 0.
- Model Flash trên web từ chối ("Tôi không thể trợ giúp…", HTTP 200) khi output yêu cầu dài → tự gọi lại bằng
  `fallback_model` (Pro). Đo 2026-10-03: Flash từ chối 7/7 lần với JSON 85–140 câu trích; Pro trả đủ 140/140.
- Deep Research (P4-DR): gọi /interactions chạy nền (Deep Research thật của Gemini, 5–30 phút/lượt) rồi hỏi lại.
  Trạng thái lưu ở bảng external_tasks (claimed_by = "gemini-proxy") và job được hẹn chạy lại bằng AwaitingExternal,
  nên worker không bị giữ suốt thời gian nghiên cứu và lượt nghiên cứu sống qua việc restart worker.
"""
from __future__ import annotations

import json
import re
import time
from datetime import timedelta
from uuid import UUID

import httpx
import psycopg
from psycopg.types.json import Jsonb

from .base import AwaitingExternal, LLMRequest, LLMResult, ProviderError, TransientError
from .external import SCHEMA_INSTRUCTION, strip_fences

DEFAULT_BASE = "http://127.0.0.1:4981/gemini/v1beta"
DEEP_RESEARCH_MODEL = "gemini-deep-research"
CLAIMED_BY = "gemini-proxy"
# Câu từ chối đóng hộp của Gemini web (không phải lỗi HTTP): ngắn và chứa một trong các cụm này
_REFUSAL = re.compile(
    r"mô hình ngôn ngữ|không được lập trình|không được thiết kế|nằm ngoài khả năng|không thể (?:trợ )?giúp"
    r"|language model|not programmed|can(?:'|no)t help with that|unable to help", re.I)
_REFUSAL_MAX_CHARS = 400


def looks_like_refusal(text: str, expects_json: bool) -> bool:
    t = text.strip()
    if expects_json and "{" not in t:
        return True
    return len(t) <= _REFUSAL_MAX_CHARS and bool(_REFUSAL.search(t))


def render_user(req: LLMRequest) -> str:
    """Phần user gửi cho web model: prompt + schema (web không ép được schema) + gợi ý tìm web."""
    parts = [req.user]
    if req.schema is not None:
        parts.append(f"{SCHEMA_INSTRUCTION}\n<output_schema>\n{json.dumps(req.schema, ensure_ascii=False)}\n</output_schema>")
    if req.grounding:
        parts.append("Được phép tìm kiếm web; ghi URL nguồn đầy đủ cho mọi dữ kiện lấy từ web.")
    return "\n\n".join(parts)


class GeminiProxyProvider:
    name = "gemini"

    def __init__(self, cfg: dict, conn: psycopg.Connection | None = None, job_id: UUID | None = None,
                 step: str | None = None, http: httpx.Client | None = None):
        self.base = cfg.get("proxy_base", DEFAULT_BASE).rstrip("/")
        self.model_map: dict[str, str] = cfg.get("model_map") or {}
        self.fallback_model: str | None = cfg.get("fallback_model")
        self.poll_seconds = cfg.get("poll_seconds", 60)
        dr = cfg.get("deep_research") or {}
        self.dr_language = dr.get("language", "Vietnamese")
        self.dr_max_sources = dr.get("max_sources", 100)
        self.dr_lease = timedelta(minutes=dr.get("lease_minutes", 120))
        self.conn, self.job_id, self.step = conn, job_id, step
        self.http = http or httpx.Client(timeout=cfg.get("timeout_seconds", 600))

    # ---------- chat thường ----------

    def complete(self, req: LLMRequest, key: str) -> LLMResult:
        if req.model == DEEP_RESEARCH_MODEL:
            return self._deep_research(req, key)
        started = time.monotonic()
        model = self.model_map.get(req.model, req.model)
        text, served = self._generate(model, req)
        fallback = False
        expects_json = req.schema is not None
        if self.fallback_model and model != self.fallback_model and looks_like_refusal(text, expects_json):
            text, served = self._generate(self.fallback_model, req)
            fallback = True
        return LLMResult(
            text=strip_fences(text) if expects_json else text, stop_reason="external", model=served,
            latency_ms=int((time.monotonic() - started) * 1000),
            raw={"web_proxy": True, "requested_model": req.model, "fallback": fallback})

    def _generate(self, model: str, req: LLMRequest) -> tuple[str, str]:
        body: dict = {"contents": [{"role": "user", "parts": [{"text": render_user(req)}]}]}
        if req.system:
            body["systemInstruction"] = {"parts": [{"text": req.system}]}
        data = self._request("POST", f"/models/{model}:generateContent", json=body)
        cands = data.get("candidates") or []
        if not cands:
            raise ProviderError("gemini proxy: không có candidate")
        parts = (cands[0].get("content") or {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        return text, data.get("modelVersion") or model

    def _request(self, method: str, path: str, allow_404: bool = False, **kw) -> dict | None:
        try:
            r = self.http.request(method, f"{self.base}{path}", **kw)
        except httpx.HTTPError as e:
            raise TransientError(f"gemini proxy không phản hồi ({self.base}): {e}") from e
        if r.status_code == 404 and allow_404:
            return None
        if r.status_code == 429 or r.status_code >= 500:
            # Proxy trả 5xx cho lỗi phía Gemini web (hết phiên, giới hạn tốc độ, BardErrorInfo): thử lại được
            raise TransientError(f"gemini proxy {r.status_code}: {r.text[:300]}")
        if r.status_code >= 400:
            raise ProviderError(f"gemini proxy {r.status_code}: {r.text[:500]}")
        return r.json()

    # ---------- Deep Research ----------

    def _start_research(self, req: LLMRequest) -> str:
        data = self._request("POST", "/interactions", json={
            "input": req.user, "agent": "deep-research", "background": True,
            "language": self.dr_language, "max_sources": self.dr_max_sources})
        if not data or not data.get("id"):
            raise ProviderError(f"gemini proxy: không tạo được lượt Deep Research: {str(data)[:300]}")
        return data["id"]

    def _deep_research(self, req: LLMRequest, key: str) -> LLMResult:
        if self.conn is None:
            raise ProviderError("Deep Research qua proxy cần connection DB (trạng thái lưu ở external_tasks)")
        row = self.conn.execute("SELECT * FROM external_tasks WHERE idempotency_key = %s", (key,)).fetchone()
        if row is None:
            interaction = self._start_research(req)
            row = self.conn.execute(
                """INSERT INTO external_tasks (idempotency_key, job_id, step, prompt_id, provider, model_hint, grounding,
                       prompt_text, status, claimed_by, lease_until, result_meta)
                   VALUES (%s, %s, %s, %s, 'gemini', %s, true, %s, 'claimed', %s, now() + %s, %s)
                   ON CONFLICT (idempotency_key) DO UPDATE SET idempotency_key = EXCLUDED.idempotency_key
                   RETURNING *""",
                (key, self.job_id, self.step, req.prompt_id, req.model, req.user, CLAIMED_BY, self.dr_lease,
                 Jsonb({"interaction_id": interaction}))).fetchone()
            raise AwaitingExternal(str(row["id"]), self.poll_seconds)

        if row["status"] == "done":
            meta = row["result_meta"] or {}
            return LLMResult(text=row["result_text"] or "", stop_reason="external", model=req.model,
                             citations=[{"url": u} for u in meta.get("source_urls", [])],
                             raw={"web_proxy": True, "external_task_id": str(row["id"]),
                                  "interaction_id": meta.get("interaction_id")})

        interaction = (row["result_meta"] or {}).get("interaction_id")
        state = self._request("GET", f"/interactions/{interaction}", allow_404=True) if interaction else None
        status = (state or {}).get("status")
        if row["status"] in ("failed", "cancelled") or state is None or status == "failed":
            # failed/cancelled: bước được chạy lại (autopilot) → nghiên cứu lại. 404: proxy đã restart nên mất task
            # (bộ lưu của proxy nằm trong RAM). status failed: ghi lỗi, lần chạy lại sau mới tạo lượt mới.
            if status == "failed" and row["status"] == "claimed":
                error = str(state.get("error"))[:1500]
                self.conn.execute("UPDATE external_tasks SET status = 'failed', failures = failures + 1, error = %s "
                                  "WHERE id = %s", (error, row["id"]))
                raise ProviderError(f"Deep Research lỗi: {error}")
            interaction = self._start_research(req)
            self.conn.execute(
                "UPDATE external_tasks SET status = 'claimed', claimed_by = %s, lease_until = now() + %s, error = NULL, "
                "result_meta = %s WHERE id = %s", (CLAIMED_BY, self.dr_lease, Jsonb({"interaction_id": interaction}), row["id"]))
            raise AwaitingExternal(str(row["id"]), self.poll_seconds)

        if status != "completed":
            # Gia hạn lease để extension (nếu còn chạy) không nhận nhầm task này
            self.conn.execute("UPDATE external_tasks SET lease_until = now() + %s WHERE id = %s", (self.dr_lease, row["id"]))
            raise AwaitingExternal(str(row["id"]), self.poll_seconds)

        outputs = state.get("outputs") or []
        report = (outputs[-1].get("text") if outputs else "") or state.get("summary") or ""
        if not report.strip():
            raise ProviderError("Deep Research hoàn tất nhưng báo cáo rỗng")
        sources = [s for s in state.get("sources") or [] if s.get("url")]
        if sources:  # prompt P4-DR yêu cầu "liệt kê nguồn kèm URL": gắn danh sách nguồn đã trích vào cuối báo cáo
            report = report.rstrip() + "\n\n## Nguồn\n\n" + "\n".join(
                f"{i}. {s.get('title') or s['url']} — {s['url']}" for i, s in enumerate(sources, 1)) + "\n"
        urls = [s["url"] for s in sources]
        self.conn.execute(
            "UPDATE external_tasks SET status = 'done', result_text = %s, result_meta = %s, completed_at = now(), "
            "lease_until = NULL WHERE id = %s",
            (report, Jsonb({"interaction_id": interaction, "source_urls": urls, "duration_ms": state.get("duration_ms")}),
             row["id"]))
        return LLMResult(text=report, stop_reason="external", model=req.model, citations=[{"url": u} for u in urls],
                         latency_ms=int(state.get("duration_ms") or 0),
                         raw={"web_proxy": True, "external_task_id": str(row["id"]), "interaction_id": interaction})
