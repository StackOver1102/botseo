"""Nhánh external: giao prompt cho extension chạy trên ChatGPT / Gemini web rồi lấy kết quả về.

Luồng: runner gọi complete() → chưa có task thì tạo (pending) và ném AwaitingExternal → executor hẹn job
chạy lại sau poll_seconds → extension claim task, dán prompt, gửi result → lần chạy sau complete() trả kết quả.
Web UI không ép được schema, effort hay max tokens: code render <output_schema> vào prompt và validate + repair như API.
"""
from __future__ import annotations

import json
from datetime import timedelta
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from .base import AwaitingExternal, LLMRequest, LLMResult, ProviderError

SCHEMA_INSTRUCTION = (
    "Trả về DUY NHẤT một đối tượng JSON hợp lệ theo schema dưới đây, không kèm lời dẫn; "
    "mọi key trong required đều phải có, không thêm key khác. "
    "Trong giá trị chuỗi không dùng dấu ngoặc kép \" (nếu bắt buộc thì viết \\\"); "
    "khi trích từ dữ liệu JSON, chỉ trích phần chữ của một giá trị, không trích tên trường hay dấu câu JSON."
)


def render_prompt_text(req: LLMRequest) -> str:
    parts = [f"<system>\n{req.system}\n</system>", req.user]
    if req.schema is not None:
        parts.append(f"{SCHEMA_INSTRUCTION}\n<output_schema>\n{json.dumps(req.schema, ensure_ascii=False)}\n</output_schema>")
    if req.grounding:
        parts.append("Được phép tìm kiếm web; ghi URL nguồn đầy đủ cho mọi dữ kiện lấy từ web.")
    return "\n\n".join(parts)


def strip_fences(text: str) -> str:
    """Web UI hay bọc JSON trong ```json … ```; bỏ đi trước khi parse."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


class ExternalProvider:
    def __init__(self, name: str, cfg: dict, conn: psycopg.Connection, job_id: UUID | None = None, step: str | None = None):
        self.name = name
        self.conn = conn
        self.job_id = job_id
        self.step = step
        self.poll_seconds = cfg.get("poll_seconds", 60)
        self.max_failures = cfg.get("max_task_failures", 3)

    def complete(self, req: LLMRequest, key: str) -> LLMResult:
        row = self.conn.execute("SELECT * FROM external_tasks WHERE idempotency_key = %s", (key,)).fetchone()
        if row is None:
            row = self.conn.execute(
                """INSERT INTO external_tasks (idempotency_key, job_id, step, prompt_id, provider, model_hint, grounding,
                       prompt_text, expect_keys)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (idempotency_key) DO UPDATE SET idempotency_key = EXCLUDED.idempotency_key
                   RETURNING *""",
                (key, self.job_id, self.step, req.prompt_id, self.name, req.model, req.grounding, render_prompt_text(req),
                 list((req.schema or {}).get("required", []))),
            ).fetchone()
        if row["status"] == "done":
            meta = row["result_meta"] or {}
            return LLMResult(
                text=strip_fences(row["result_text"] or ""), stop_reason="external", model=meta.get("model", req.model),
                citations=[{"url": u} for u in meta.get("source_urls", [])], raw={"external_task_id": str(row["id"])},
            )
        if row["status"] == "failed":
            if row["failures"] >= self.max_failures:
                raise ProviderError(f"external task {row['id']} lỗi {row['failures']} lần: {row['error']}")
            self.conn.execute("UPDATE external_tasks SET status = 'pending', claimed_by = NULL, lease_until = NULL WHERE id = %s",
                              (row["id"],))
        if row["status"] == "cancelled":
            # Task bị huỷ khi tua job (--from) nhưng job vẫn đang chạy và cần đúng prompt này: mở lại thay vì báo lỗi
            job = self.conn.execute("SELECT state FROM jobs WHERE id = %s", (self.job_id,)).fetchone() if self.job_id else None
            if job is None or job["state"] == "CANCELLED":
                raise ProviderError(f"external task {row['id']} đã bị huỷ")
            self.conn.execute("UPDATE external_tasks SET status = 'pending', claimed_by = NULL, lease_until = NULL, "
                              "failures = 0, error = NULL WHERE id = %s", (row["id"],))
        raise AwaitingExternal(str(row["id"]), self.poll_seconds)


# ---------- phía API cho extension ----------

def claim_next(conn: psycopg.Connection, provider: str, client_id: str, lease: timedelta) -> dict[str, Any] | None:
    with conn.transaction():
        return conn.execute(
            """UPDATE external_tasks SET status = 'claimed', claimed_by = %s, lease_until = now() + %s
               WHERE id = (
                   SELECT id FROM external_tasks
                   WHERE provider = %s AND (status = 'pending' OR (status = 'claimed' AND lease_until < now()))
                   ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED)
               RETURNING id, provider, model_hint, grounding, prompt_text, prompt_id, expect_keys, created_at""",
            (client_id, lease, provider),
        ).fetchone()


def submit_result(conn: psycopg.Connection, task_id: UUID, client_id: str, text: str, meta: dict | None) -> bool:
    """Ghi kết quả và đánh thức job đang chờ. False = task không còn thuộc client này."""
    with conn.transaction():
        row = conn.execute(
            """UPDATE external_tasks SET status = 'done', result_text = %s, result_meta = %s, completed_at = now()
               WHERE id = %s AND status = 'claimed' AND claimed_by = %s RETURNING job_id""",
            (text, Jsonb(meta or {}), task_id, client_id),
        ).fetchone()
        if row is None:
            return False
        if row["job_id"]:
            conn.execute("UPDATE jobs SET run_after = now() WHERE id = %s AND lease_until IS NULL", (row["job_id"],))
        return True


def fail_task(conn: psycopg.Connection, task_id: UUID, client_id: str, error: str) -> bool:
    with conn.transaction():
        row = conn.execute(
            """UPDATE external_tasks SET status = 'failed', failures = failures + 1, error = %s, claimed_by = NULL, lease_until = NULL
               WHERE id = %s AND status = 'claimed' AND claimed_by = %s RETURNING job_id""",
            (error, task_id, client_id),
        ).fetchone()
        if row and row["job_id"]:
            conn.execute("UPDATE jobs SET run_after = now() WHERE id = %s AND lease_until IS NULL", (row["job_id"],))
        return row is not None
