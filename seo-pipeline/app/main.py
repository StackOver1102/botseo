"""HTTP API: /external cho extension trình duyệt (nhánh GPT/Gemini qua web), /jobs cho editor/admin.

Chạy: SEO_EXTENSION_TOKEN=... uvicorn app.main:app --port 8000
Extension gửi header `X-Extension-Token` và `X-Client-Id` (định danh máy/tab, để một task chỉ một client giữ).
Editor gửi `X-Admin-Token` (SEO_ADMIN_TOKEN) và `X-User-Id`.
"""
from __future__ import annotations

import hmac
import os
import time
from datetime import timedelta
from typing import Any, Iterator
from uuid import UUID

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from pipeline.artifacts import ArtifactStore, LocalBlobStore
from pipeline.config import load_config
from pipeline.db import connect
from pipeline.executor import human_resolve
from pipeline.inputs import JobInput, create_job_with_input
from pipeline.llm import external
from pipeline.notify import safe_listener

app = FastAPI(title="SEO pipeline")
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^(chrome-extension|moz-extension)://.*$",
    allow_methods=["GET", "POST"],
    allow_headers=["X-Extension-Token", "X-Client-Id", "Content-Type"],
)
CONFIG = load_config()

from app.ui import router as ui_router  # noqa: E402

app.include_router(ui_router)


# Worker chạy nền trong cùng tiến trình: job tạo từ UI tự chạy, không cần mở thêm terminal.
# Tắt bằng SEO_EMBEDDED_WORKER=0 (vd. khi chạy worker riêng: python -m pipeline.worker).
@app.on_event("startup")
def _start_embedded_worker() -> None:
    if os.environ.get("SEO_EMBEDDED_WORKER", "1") == "0":
        return
    import threading
    from pipeline.worker import run_forever
    threading.Thread(target=run_forever, name="embedded-worker", daemon=True).start()


def get_conn() -> Iterator[psycopg.Connection]:
    with connect(autocommit=True) as conn:
        yield conn


def extension_client(x_extension_token: str = Header(...), x_client_id: str = Header(..., max_length=200)) -> str:
    expected = os.environ.get("SEO_EXTENSION_TOKEN", "")
    if not expected or not hmac.compare_digest(x_extension_token, expected):
        raise HTTPException(401, "token sai")
    return x_client_id


class TaskOut(BaseModel):
    id: UUID
    provider: str
    model_hint: str
    grounding: bool
    prompt_id: str
    prompt_text: str
    expect_keys: list[str] = Field(default_factory=list)  # key gốc của JSON kết quả; rỗng = Markdown
    selectors: dict[str, list[str]] = Field(default_factory=dict)  # từ config, thử trước selector cài trong extension
    tool: str | None = None  # "deep_research": extension bật chế độ Deep Research trước khi gửi
    tool_texts: dict[str, Any] = Field(default_factory=dict)  # chữ hiển thị của nút cần bấm; menu_path: list các bước
    timeout_seconds: int = 720  # thời gian extension chờ câu trả lời
    chat_url: str | None = None  # URL mở chat mới (vd. chọn model nhanh cho gate); None = trang chủ của hãng


class ResultIn(BaseModel):
    text: str = Field(min_length=1)
    model: str | None = None
    source_urls: list[str] = Field(default_factory=list)


class FailIn(BaseModel):
    error: str = Field(min_length=1, max_length=2000)


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/external/tasks/next", response_model=TaskOut | None)
def next_task(provider: str = Query(pattern="^(openai|gemini)$"), wait: int = Query(0, ge=0, le=25),
              client: str = Depends(extension_client), conn: psycopg.Connection = Depends(get_conn)):
    """Nhận task tiếp theo cho hãng. `wait` > 0: long-poll, giữ request tới `wait` giây và trả ngay khi có task.
    204 khi không có việc."""
    lease = timedelta(seconds=CONFIG["providers"][provider].get("task_lease_seconds", 900))
    deadline = time.monotonic() + wait
    # LISTEN trước khi claim để không lỡ task tạo ra giữa hai thao tác; DB chưa có trigger (migration 005) thì quét 0,5s
    listener = safe_listener("ext_task") if wait else None
    try:
        row = external.claim_next(conn, provider, client, lease)
        while row is None and (left := deadline - time.monotonic()) > 0:
            if listener:
                # Thức dậy khi có task của hãng này; tối đa 5s/lần để nhận cả task hết lease (không có notify)
                listener.wait(min(left, 5.0), lambda ch, payload: payload == provider)
            else:
                time.sleep(0.5)
            row = external.claim_next(conn, provider, client, lease)
    finally:
        if listener:
            listener.close()
    if row is None:
        return Response(status_code=204)
    # Đọc lại config mỗi lần: sửa selector trong config.yaml là có hiệu lực ngay, không cần restart hay reload extension
    ext = load_config().get("extension", {})
    selectors = ext.get("selectors", {}).get(provider, {})
    extra: dict = {"selectors": selectors}
    urls = ext.get("chat_urls", {}).get(provider, {})
    kind = "fast" if row["prompt_id"] in ext.get("fast_prompts", []) else "default"
    extra["chat_url"] = urls.get(kind) or urls.get("default")
    dr = ext.get("deep_research", {})
    if dr.get("enabled") and provider == "gemini" and row["prompt_id"] in dr.get("prompt_ids", []):
        timeout = int(dr.get("timeout_minutes", 40) * 60)
        extra.update(tool="deep_research", tool_texts=dr.get("texts", {}), timeout_seconds=timeout)
        # Deep Research chạy lâu hơn lease mặc định: giữ task đủ lâu để client khác không nhận lại giữa chừng
        conn.execute("UPDATE external_tasks SET lease_until = now() + %s WHERE id = %s",
                     (timedelta(seconds=timeout + 300), row["id"]))
    fields = {k: row[k] for k in ("id", "provider", "model_hint", "grounding", "prompt_id", "prompt_text", "expect_keys")}
    return TaskOut(**fields, **extra)


@app.post("/external/tasks/{task_id}/result", status_code=204)
def submit(task_id: UUID, body: ResultIn, client: str = Depends(extension_client),
           conn: psycopg.Connection = Depends(get_conn)):
    meta = {"model": body.model, "source_urls": body.source_urls}
    if not external.submit_result(conn, task_id, client, body.text, meta):
        raise HTTPException(409, "task không còn do client này giữ (hết lease hoặc đã xong)")


class GeminiCookiesIn(BaseModel):
    cookie_header: str = Field("", max_length=30000)   # header Cookie của gemini.google.com; rỗng khi extension không đọc được
    error: str | None = Field(None, max_length=500)    # lý do extension không đọc được cookie (để ghi log chẩn đoán)
    auth_user: str | None = Field(None, pattern=r"^\d{0,2}$")     # số trong /u/<số>/; "" = tài khoản mặc định; None = giữ


@app.post("/external/gemini-cookies")
def gemini_cookies(body: GeminiCookiesIn, client: str = Depends(extension_client)) -> dict:
    """Extension gửi cookie Gemini định kỳ; chỉ khi proxy cục bộ báo hết hạn thì mới ghi vào .env của proxy và khởi động
    lại nó (pipeline.gemini_cookies). Kết quả không chứa giá trị cookie."""
    import logging

    from pipeline.gemini_cookies import CookieSyncError, cookie_names, sync
    logger = logging.getLogger("uvicorn.error")
    if body.error or not body.cookie_header:
        logger.warning("gemini cookie sync: extension không đọc được cookie: %s", body.error or "cookie rỗng")
        return {"updated": False, "healthy": None, "reason": f"extension không đọc được cookie: {body.error or 'rỗng'}"}
    try:
        result = sync(load_config(), body.cookie_header, body.auth_user)
    except CookieSyncError as e:
        logger.warning("gemini cookie sync lỗi: %s", e)
        raise HTTPException(500, str(e)) from e
    # Chỉ ghi số lượng cookie và kết quả, không ghi giá trị
    logger.info("gemini cookie sync: nhận %d cookie (auth_user=%s) → %s", len(cookie_names(body.cookie_header)),
                body.auth_user, result["reason"])
    return result


@app.post("/external/tasks/{task_id}/fail", status_code=204)
def fail(task_id: UUID, body: FailIn, client: str = Depends(extension_client),
         conn: psycopg.Connection = Depends(get_conn)):
    if not external.fail_task(conn, task_id, client, body.error):
        raise HTTPException(409, "task không còn do client này giữ")


# ---------- quản lý job (editor/admin; UI §7 sẽ dùng các endpoint này) ----------

def admin_user(x_admin_token: str = Header(...), x_user_id: str = Header(..., max_length=200)) -> str:
    expected = os.environ.get("SEO_ADMIN_TOKEN", "")
    if not expected or not hmac.compare_digest(x_admin_token, expected):
        raise HTTPException(401, "token sai")
    return x_user_id


class JobCreate(BaseModel):
    input: JobInput
    author_id: str
    source_text: str | None = None  # nhánh B: text bài nguồn, bị xoá sau FACT_GATE


class Resolve(BaseModel):
    action: str = Field(pattern="^(approve|rerun|cancel)$")
    reason: str = Field(min_length=3)


@app.post("/jobs", status_code=201)
def create_job(body: JobCreate, user: str = Depends(admin_user), conn: psycopg.Connection = Depends(get_conn)) -> dict:
    artifacts = ArtifactStore(LocalBlobStore(CONFIG.get("blob_dir")))
    job_id = create_job_with_input(conn, artifacts, body.input, author_id=body.author_id, created_by=user,
                                   config=CONFIG, source_text=body.source_text)
    return {"id": job_id}


@app.get("/jobs/{job_id}")
def get_job(job_id: UUID, user: str = Depends(admin_user), conn: psycopg.Connection = Depends(get_conn)) -> dict:
    job = conn.execute(
        """SELECT id, topic, branch, mode, ymyl, state, resume_state, needs_human_reason, revise_count, cost_usd,
                  cost_cap_usd, last_error, created_at, updated_at FROM jobs WHERE id = %s""", (job_id,)).fetchone()
    if job is None:
        raise HTTPException(404)
    job["artifacts"] = conn.execute(
        """SELECT DISTINCT ON (kind) kind, version, id, created_at FROM artifacts WHERE job_id = %s
           ORDER BY kind, version DESC""", (job_id,)).fetchall()
    job["transitions"] = conn.execute(
        "SELECT from_state, outcome, to_state, reason, actor, created_at FROM job_transitions WHERE job_id = %s ORDER BY id",
        (job_id,)).fetchall()
    job["external_tasks"] = conn.execute(
        "SELECT id, prompt_id, provider, status, failures, created_at FROM external_tasks WHERE job_id = %s ORDER BY created_at",
        (job_id,)).fetchall()
    return job


@app.post("/jobs/{job_id}/resolve")
def resolve(job_id: UUID, body: Resolve, user: str = Depends(admin_user), conn: psycopg.Connection = Depends(get_conn)) -> dict:
    """Xử lý job ở NEEDS_HUMAN: approve (đi tiếp), rerun (chạy lại bước dừng), cancel."""
    try:
        after = human_resolve(conn, job_id, user, body.action, body.reason, CONFIG)
    except Exception as e:
        raise HTTPException(409, str(e)) from e
    return {"state": after.state.value}
