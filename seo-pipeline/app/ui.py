"""UI theo dõi pipeline (chỉ đọc): http://localhost:8000/ui

Chỉ mở từ máy local (dữ liệu có prompt, output model, text bài). Mở ra mạng ngoài thì đặt SEO_UI_TOKEN
và truy cập /ui?token=... (token được lưu ở sessionStorage của trình duyệt và gửi qua header X-UI-Token).
"""
from __future__ import annotations

import hmac
import json
import os
from pathlib import Path
from typing import Iterator
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from fastapi.responses import FileResponse, StreamingResponse

from pipeline.artifacts import LocalBlobStore
from pipeline.config import load_config
from pipeline.db import connect
from pipeline.notify import safe_listener
from pipeline.states import State

ROOT = Path(__file__).resolve().parent.parent
CONFIG = load_config()
BLOBS = LocalBlobStore(ROOT / CONFIG.get("blob_dir", "var/blobs"))
_REWORK = ("REWORK", "REWORK_CHECK")
PIPELINE = [s.value for s in State if s not in (State.NEEDS_HUMAN, State.STALE, State.CANCELLED) and s.value not in _REWORK]


def _pipeline(flow: str | None) -> list[str]:
    return [*_REWORK, "DONE"] if flow == "rework" else PIPELINE
MAX_TEXT = 400_000  # ký tự; prompt/output dài hơn thì cắt để trình duyệt không treo

router = APIRouter()


def ui_access(request: Request, x_ui_token: str | None = Header(None)) -> None:
    token = os.environ.get("SEO_UI_TOKEN", "")
    if token:
        x_ui_token = x_ui_token or request.query_params.get("token")  # EventSource không gửi được header
        if not x_ui_token or not hmac.compare_digest(x_ui_token, token):
            raise HTTPException(401, "cần SEO_UI_TOKEN")
        return
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "localhost", "testclient"):
        raise HTTPException(403, "UI chỉ mở từ máy local (hoặc đặt SEO_UI_TOKEN)")


def conn_dep() -> Iterator[psycopg.Connection]:
    with connect(autocommit=True) as conn:
        yield conn


def _blob(uri: str | None) -> str | None:
    if not uri:
        return None
    try:
        text = BLOBS.get(uri).decode("utf-8", errors="replace")
    except (OSError, ValueError):
        return None
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + f"\n\n… (cắt, tổng {len(text)} ký tự)"


@router.get("/ui", include_in_schema=False)
def page(request: Request) -> FileResponse:
    host = request.client.host if request.client else ""
    if not os.environ.get("SEO_UI_TOKEN") and host not in ("127.0.0.1", "::1", "localhost", "testclient"):
        raise HTTPException(403, "UI chỉ mở từ máy local (hoặc đặt SEO_UI_TOKEN)")
    return FileResponse(ROOT / "app" / "static" / "ui.html", headers={"Cache-Control": "no-store"})


@router.get("/ui/api/overview", dependencies=[Depends(ui_access)])
def overview(conn: psycopg.Connection = Depends(conn_dep)) -> dict:
    jobs = conn.execute(
        """SELECT j.id, j.topic, j.state, j.resume_state, j.mode, j.branch, j.cost_usd, j.cost_cap_usd, j.last_error, j.current_call, j.flow,
                  j.created_at, j.updated_at,
                  (SELECT count(*) FROM job_transitions t WHERE t.job_id = j.id AND t.actor = 'autopilot') AS autopilot_n
           FROM jobs j ORDER BY j.created_at DESC LIMIT 100""").fetchall()
    queue = conn.execute(
        """SELECT provider, status, count(*) AS n, min(created_at) AS oldest FROM external_tasks
           WHERE status IN ('pending', 'claimed') GROUP BY provider, status""").fetchall()
    recent = conn.execute(
        """SELECT provider, count(*) FILTER (WHERE status = 'done') AS done, count(*) FILTER (WHERE status = 'failed') AS failed,
                  percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM completed_at - created_at))
                      FILTER (WHERE status = 'done') AS median_s
           FROM external_tasks WHERE created_at > now() - interval '24 hours' GROUP BY provider""").fetchall()
    spend = conn.execute("SELECT coalesce(sum(cost_usd), 0) AS today FROM prompt_runs WHERE created_at > date_trunc('day', now())"
                         ).fetchone()
    return {"jobs": jobs, "queue": queue, "recent": recent, "spend_today": spend["today"], "pipeline": PIPELINE}


@router.get("/ui/api/stream", dependencies=[Depends(ui_access)])
def stream() -> StreamingResponse:
    """SSE: đẩy job_id mỗi khi job/task đổi (NOTIFY từ migration 005) để UI tải lại ngay, không chờ vòng 5s."""
    def gen():
        listener = safe_listener("job_event", "ext_task")
        if listener is None:
            yield "event: nolisten\ndata: {}\n\n"
            return
        try:
            yield "retry: 3000\n\n"
            while True:
                sent = set()
                for ch, payload in listener.events(timeout=15):
                    key = (ch, payload)
                    if key not in sent:  # gộp các notify trùng trong cùng đợt
                        sent.add(key)
                        yield f"data: {json.dumps({'channel': ch, 'id': payload})}\n\n"
                if not sent:
                    yield ": ping\n\n"  # giữ kết nối; client đóng tab thì lần ghi này làm generator dừng
        finally:
            listener.close()
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.get("/ui/api/jobs/{job_id}", dependencies=[Depends(ui_access)])
def job_detail(job_id: UUID, conn: psycopg.Connection = Depends(conn_dep)) -> dict:
    job = conn.execute("SELECT * FROM jobs WHERE id = %s", (job_id,)).fetchone()
    if job is None:
        raise HTTPException(404)
    transitions = conn.execute(
        "SELECT id, from_state, outcome, to_state, reason, actor, created_at FROM job_transitions WHERE job_id = %s ORDER BY id",
        (job_id,)).fetchall()
    runs = conn.execute(
        """SELECT id, step, prompt_id, prompt_version, provider, model, repair_attempt, status, validation_errors,
                  tokens_in, tokens_out, cost_usd, latency_ms, created_at, coalesce((params->>'round')::int, 0) AS round,
                  (request_redacted->>'prompt_uri') IS NOT NULL
                      OR (params->'raw' ? 'external_task_id') AS has_prompt
           FROM prompt_runs WHERE job_id = %s ORDER BY created_at""", (job_id,)).fetchall()
    tasks = conn.execute(
        """SELECT id, step, prompt_id, provider, status, failures, error, claimed_by, created_at, completed_at
           FROM external_tasks WHERE job_id = %s ORDER BY created_at""", (job_id,)).fetchall()
    artifacts = conn.execute(
        """SELECT DISTINCT ON (kind) kind, version, id, produced_by, created_at FROM artifacts WHERE job_id = %s
           ORDER BY kind, version DESC""", (job_id,)).fetchall()
    warn = conn.execute(
        "SELECT blob_uri FROM artifacts WHERE job_id = %s AND kind = 'Warnings' ORDER BY version DESC LIMIT 1",
        (job_id,)).fetchone()
    return {"job": job, "transitions": transitions, "runs": runs, "tasks": tasks, "artifacts": artifacts,
            "warnings": warn and _json_blob(warn["blob_uri"]), "pipeline": _pipeline(job.get("flow"))}


@router.get("/ui/api/jobs/{job_id}/steps", dependencies=[Depends(ui_access)])
def job_steps(job_id: UUID, conn: psycopg.Connection = Depends(conn_dep)) -> dict:
    """Tab "Các bước": mỗi bước làm gì, các lượt gọi model (prompt + kết quả xem qua /ui/api/runs/{id}), artifact sinh ra
    và chỉnh ở đâu (file prompt, khoá config, hàm code). Config và prompt nạp lại mỗi lần gọi để thấy bản đang nằm trên đĩa."""
    from pipeline.prompts import load_registry

    from .step_info import build_steps
    job = conn.execute("SELECT id, state, resume_state, flow FROM jobs WHERE id = %s", (job_id,)).fetchone()
    if job is None:
        raise HTTPException(404)
    transitions = conn.execute(
        "SELECT from_state, outcome, to_state, reason, actor, created_at FROM job_transitions WHERE job_id = %s ORDER BY id",
        (job_id,)).fetchall()
    runs = conn.execute(
        """SELECT id, step, prompt_id, prompt_version, provider, model, repair_attempt, status,
                  coalesce(jsonb_array_length(validation_errors), 0) AS n_errors,
                  tokens_in, tokens_out, cost_usd, latency_ms, created_at, coalesce((params->>'round')::int, 0) AS round
           FROM prompt_runs WHERE job_id = %s ORDER BY created_at""", (job_id,)).fetchall()
    artifacts = conn.execute(
        """SELECT DISTINCT ON (produced_by, kind) kind, version, id, produced_by, created_at FROM artifacts WHERE job_id = %s
           ORDER BY produced_by, kind, version DESC""", (job_id,)).fetchall()  # bản mới nhất của từng loại TẠI từng bước
    return {"steps": build_steps(job, transitions, runs, artifacts, _pipeline(job.get("flow")), load_registry(), load_config())}


def _json_blob(uri: str):
    text = _blob(uri)
    try:
        return json.loads(text) if text else None
    except ValueError:
        return text


@router.get("/ui/api/runs/{run_id}", dependencies=[Depends(ui_access)])
def run_detail(run_id: UUID, conn: psycopg.Connection = Depends(conn_dep)) -> dict:
    r = conn.execute("SELECT * FROM prompt_runs WHERE id = %s", (run_id,)).fetchone()
    if r is None:
        raise HTTPException(404)
    prompt = _blob((r["request_redacted"] or {}).get("prompt_uri"))
    if prompt is None and (task_id := (r["params"] or {}).get("raw", {}).get("external_task_id")):
        t = conn.execute("SELECT prompt_text FROM external_tasks WHERE id = %s", (task_id,)).fetchone()
        prompt = t and t["prompt_text"]
    return {"id": r["id"], "step": r["step"], "prompt_id": r["prompt_id"], "status": r["status"],
            "errors": r["validation_errors"], "prompt": prompt, "output": _blob(r["response_raw_uri"])}


@router.get("/ui/api/artifacts/{artifact_id}", dependencies=[Depends(ui_access)])
def artifact(artifact_id: UUID, conn: psycopg.Connection = Depends(conn_dep)) -> dict:
    a = conn.execute("SELECT kind, version, blob_uri FROM artifacts WHERE id = %s", (artifact_id,)).fetchone()
    if a is None:
        raise HTTPException(404)
    return {"kind": a["kind"], "version": a["version"], "payload": _json_blob(a["blob_uri"])}


# ---------- tạo job từ UI ----------

class JobForm(BaseModel):
    branch: str = Field(pattern="^(A|B)$")
    topic: str = Field("", max_length=300)  # nhánh A được bỏ trống: lấy keyword volume cao nhất / heading đầu bài
    brief: str = ""
    mode: str = Field("standard", pattern="^(lite|standard|heavy)$")
    ymyl: bool = False
    target_words: int = Field(1500, ge=300, le=8000)
    keywords_text: str = ""           # dán tay: mỗi dòng "keyword[,volume]"
    keywords_b64: str | None = None   # file (Ahrefs UTF-16/tab hoặc CSV) dạng base64
    article_md: str | None = None     # nhánh A: bài hiện tại của site
    article_url: str | None = None    # nhánh A: URL bài (giữ URL khi làm mới), chỉ để ghi nhận
    auto_source: int = Field(5, ge=0, le=8)  # nhánh B: số trang top SERP lấy làm tư liệu (0 = bài mới hoàn toàn)
    serp_query: str | None = None
    paa_text: str = ""
    author_id: str = "tac-gia-01"
    site_id: str | None = None        # site của bài (bảng sites): kho dữ liệu, trang nội bộ, tác giả mặc định đi theo
    flow: str = Field("full", pattern="^(rework|full)$")  # nhánh A: full = làm mới đầy đủ (mặc định); rework = chỉ viết lại câu chữ


class FetchForm(BaseModel):
    url: str = Field(min_length=8, max_length=2000)


@router.post("/ui/api/fetch-article", dependencies=[Depends(ui_access)])
def fetch_article_preview(form: FetchForm) -> dict:
    """Nút "Lấy bài từ link" trên form: tải bài để người dùng xem/sửa trước khi tạo job."""
    from pipeline.article_io import FetchError, fetch_article
    try:
        title, md = fetch_article(form.url)
    except FetchError as e:
        raise HTTPException(422, str(e)) from e
    return {"title": title, "article_md": md, "words": len(md.split())}


# ---------- site ----------

class SiteForm(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    url: str = Field(min_length=8, max_length=500)
    id: str | None = Field(None, max_length=61)  # bỏ trống = tên miền của url
    default_author_id: str | None = None


@router.get("/ui/api/sites", dependencies=[Depends(ui_access)])
def sites(conn: psycopg.Connection = Depends(conn_dep)) -> dict:
    from pipeline.sites import list_sites
    return {"sites": list_sites(conn)}


@router.post("/ui/api/sites", dependencies=[Depends(ui_access)], status_code=201)
def save_site(form: SiteForm, conn: psycopg.Connection = Depends(conn_dep)) -> dict:
    from pipeline.sites import SiteError, upsert_site
    try:
        return {"id": upsert_site(conn, form.name, form.url, form.id, form.default_author_id)}
    except SiteError as e:
        raise HTTPException(422, str(e)) from e


# ---------- tác giả ----------

class AuthorForm(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    url: str | None = Field(None, max_length=500)
    id: str | None = Field(None, max_length=61)  # bỏ trống = tạo từ tên (không dấu, gạch nối)
    kind: str = Field("person", pattern="^(person|organization)$")  # organization = ban biên tập / thương hiệu
    site_id: str | None = None  # tác giả thuộc site nào (bỏ trống = dùng chung)


@router.get("/ui/api/authors", dependencies=[Depends(ui_access)])
def authors(conn: psycopg.Connection = Depends(conn_dep)) -> dict:
    from pipeline.authors import list_authors
    return {"authors": list_authors(conn)}


@router.post("/ui/api/authors", dependencies=[Depends(ui_access)], status_code=201)
def save_author(form: AuthorForm, conn: psycopg.Connection = Depends(conn_dep)) -> dict:
    from pipeline.authors import AuthorError, upsert_author
    try:
        return {"id": upsert_author(conn, form.name, form.url, form.id, form.kind, form.site_id)}
    except AuthorError as e:
        raise HTTPException(422, str(e)) from e


@router.post("/ui/api/jobs", dependencies=[Depends(ui_access)], status_code=201)
def create_job(form: JobForm, conn: psycopg.Connection = Depends(conn_dep)) -> dict:
    import base64

    from pipeline.artifacts import ArtifactStore
    from pipeline.autosource import AutoSourceError, gather_sources
    from pipeline.inputs import JobInput, create_job_with_input
    from pipeline.keyword_io import decode, parse_keywords

    text = form.keywords_text
    if form.keywords_b64:
        try:
            text = decode(base64.b64decode(form.keywords_b64)) + "\n" + text
        except ValueError as e:
            raise HTTPException(422, f"file keyword không đọc được: {e}") from e
    keywords = parse_keywords(text)
    rework = form.branch == "A" and form.flow == "rework"
    if not keywords and form.branch == "B":  # nhánh A: keyword tự lấy từ bài nếu không nhập
        raise HTTPException(422, "cần ít nhất 1 keyword")
    article, fetched_title = None, None
    if form.branch == "A":
        from pipeline.article_io import FetchError, fetch_article, first_heading, normalize_article
        text_in = (form.article_md or "").strip()
        if not text_in and (form.article_url or "").strip():
            try:
                fetched_title, text_in = fetch_article(form.article_url)
            except FetchError as e:
                raise HTTPException(422, f"link bài cũ: {e}") from e
        if not text_in:
            raise HTTPException(422, "nhánh A cần nội dung bài hoặc link bài cũ")
        article = normalize_article(text_in)  # văn bản thường từ CMS: đoán heading, thêm ##
    topic = (form.topic.strip() or fetched_title or (keywords[0]["keyword"] if keywords else "")
             or (article and first_heading(article)) or (article or "")[:80].split("\n")[0])
    if len(topic) < 3:
        raise HTTPException(422, "cần chủ đề (nhánh B) hoặc ít nhất 1 keyword")
    brief = form.brief + (f"\nURL bài hiện tại: {form.article_url}" if form.article_url else "")
    config = load_config()
    # Tác giả chọn từ bảng authors: tên/URL cho JSON-LD + hồ sơ giọng văn dùng lại (nhánh B khỏi cần mẫu giọng).
    # Mã không có trong bảng (job cũ, "tac-gia-01") → dùng default_author của config như trước.
    from pipeline.authors import get_author, jsonld_type
    from pipeline.sites import get_site
    site = get_site(conn, form.site_id) if form.site_id else None
    if form.site_id and (site is None or not site["active"]):
        raise HTTPException(422, f"không có site '{form.site_id}'")
    author_id = form.author_id
    if site and site["default_author_id"] and get_author(conn, author_id) is None:
        author_id = site["default_author_id"]  # form không chọn tác giả có thật → tác giả mặc định của site
    row = get_author(conn, author_id)
    if row and not row["active"]:
        raise HTTPException(422, f"tác giả '{author_id}' đã bị ẩn")
    da = row or config.get("default_author") or {}
    inp = JobInput(topic=topic, brief=brief.strip(), branch=form.branch, mode=form.mode, ymyl=form.ymyl,
                   author={"name": da["name"], "url": da.get("url"), "type": jsonld_type(row)} if da.get("name") else None,
                   voice_profile=row["voice_profile"] if row else None,
                   internal_pages=site["internal_pages"] if site else [],
                   article_md=article, keywords=keywords,
                   paa_extra=[q.strip() for q in form.paa_text.splitlines() if q.strip()], target_words=form.target_words)
    notes, sources = [], []
    if form.branch == "B" and form.auto_source:
        try:
            sources = gather_sources(inp, config, form.auto_source, form.serp_query, log=notes.append)
        except AutoSourceError as e:
            raise HTTPException(422, f"auto_source: {e}") from e
    job_id = create_job_with_input(conn, ArtifactStore(BLOBS), inp, author_id=author_id, created_by="ui",
                                   config=config, source_texts=sources, flow="rework" if rework else "full",
                                   site_id=site["id"] if site else None)
    return {"id": job_id, "keywords": len(keywords), "sources": len(sources), "notes": notes}
