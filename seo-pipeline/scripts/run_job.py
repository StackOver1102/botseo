"""Tạo job từ một thư mục đầu vào và chạy worker, in tiến độ từng bước.

    .venv/bin/python scripts/run_job.py examples/vay-tien-online --auto-gates
    .venv/bin/python scripts/run_job.py --resume <job_id> --auto-gates
    .venv/bin/python scripts/run_job.py examples/vay-tien-online --dry-run     # chỉ kiểm tra file, không tạo job
                                                                        # (auto_source: vẫn lấy SERP + tải nguồn để xem trước)

--auto-gates : mọi AI gate chạy chế độ auto cho lần chạy này (không sửa config.yaml).
Cấu trúc thư mục: xem examples/vay-tien-online/job.yaml.
Job dừng khi vào NEEDS_HUMAN (in lý do) hoặc DONE; kết quả được xuất ra out/<job_id>/.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from knowledge.repository import EntryInput, KnowledgeValidationError, import_entry  # noqa: E402
from pipeline.artifacts import ArtifactStore, LocalBlobStore  # noqa: E402
from pipeline.config import load_config  # noqa: E402
from pipeline.db import connect, migrate  # noqa: E402
from pipeline.autosource import AutoSourceError, gather_sources  # noqa: E402
from pipeline.executor import Worker, cancel_job, human_resolve, rewind_job  # noqa: E402
from pipeline.states import State  # noqa: E402
from pipeline.inputs import JobInput, create_job_with_input  # noqa: E402
from pipeline.notify import safe_listener  # noqa: E402
from pipeline.steps import HANDLERS, Services  # noqa: E402

GATES = ("triage", "kw", "fact", "outline", "qa", "fact_signoff", "final")
STOP = {"NEEDS_HUMAN", "STALE", "DONE", "CANCELLED"}


def read(folder: Path, name: str) -> str | None:
    p = folder / name
    return p.read_text(encoding="utf-8") if p.exists() else None


def load_folder(folder: Path) -> tuple[dict, JobInput, str | None, list[dict]]:
    meta = yaml.safe_load(read(folder, "job.yaml") or "") or {}
    keywords = [{"keyword": r["keyword"], "volume": int(r["volume"]) if r.get("volume") else None}
                for r in csv.DictReader((read(folder, "keywords.csv") or "keyword,volume\n").splitlines())]
    serp = None
    paa = [line.strip() for line in (read(folder, "paa.txt") or "").splitlines() if line.strip()]
    if serp_csv := read(folder, "serp.csv"):
        organic = [{"rank": int(r["rank"]), "url": r["url"], "title": r.get("title", ""), "snippet": r.get("snippet", "")}
                   for r in csv.DictReader(serp_csv.splitlines())]
        serp = {"organic": organic, "paa": paa, "aio_present": meta.get("aio_present"),
                "aio_cited_urls": meta.get("aio_cited_urls") or []}
    article = read(folder, "article.md")
    if not article and meta.get("article_url"):  # nhánh A: link bài cũ thay cho article.md
        from pipeline.article_io import FetchError, fetch_article
        try:
            title, article = fetch_article(meta["article_url"])
        except FetchError as e:
            sys.exit(f"article_url: {e}")
        meta.setdefault("topic", title)
    if article:
        from pipeline.article_io import first_heading, normalize_article
        article = normalize_article(article)  # bài dán từ CMS: văn bản thường, không có dấu #
    topic = meta.get("topic") or (keywords[0]["keyword"] if keywords else None) or (article and first_heading(article))
    if not topic:
        sys.exit("job.yaml cần topic (hoặc keywords.csv / article.md có heading)")
    inp = JobInput(
        topic=topic, brief=meta.get("brief", ""), branch=meta.get("branch"), mode=meta.get("mode"),
        ymyl=meta.get("ymyl"), article_md=article, voice_samples_md=read(folder, "voice_samples.md"),
        keywords=keywords, serp=serp, internal_pages=meta.get("internal_pages") or [],
        existing_titles=meta.get("existing_titles") or [], author=meta.get("author"), paa_extra=[] if serp else paa,
        target_words=meta.get("target_words", 2000),
        voice_profile=json.loads(read(folder, "voice_profile.json")) if read(folder, "voice_profile.json") else None,
    )
    knowledge = yaml.safe_load(read(folder, "knowledge.yaml") or "") or []
    return meta, inp, read(folder, "source.md"), knowledge


def import_knowledge(conn, author_id: str, entries: list[dict]) -> None:
    for e in entries:
        exists = conn.execute("SELECT id FROM author_knowledge WHERE author_id = %s AND title = %s AND status = 'active'",
                              (author_id, e["title"])).fetchone()
        if exists:
            print(f"  kho: bỏ qua '{e['title']}' (đã có {exists['id']})")
            continue
        try:
            r = import_entry(conn, EntryInput(
                author_id=author_id, kind=e["kind"], title=e["title"], content=e["content"],
                topic_tags=e["topic_tags"], valid_until=e["valid_until"], facts=e.get("facts") or [],
                evidence_ref=e.get("evidence_ref")))
            print(f"  kho: nạp {r.entry_id} '{e['title']}'" + (f" (redact {r.pii_redacted} PII)" if r.pii_redacted else ""))
        except KnowledgeValidationError as err:
            sys.exit(f"  kho: LỖI '{e['title']}': {err}")


def auto_sources(meta: dict, inp: JobInput, config: dict) -> list[str]:
    """job.yaml `auto_source: N`: lấy SERP theo topic (dùng luôn làm SERP của job) rồi tải N trang top làm bài nguồn."""
    if not meta.get("auto_source"):
        return []
    try:
        return gather_sources(inp, config, int(meta["auto_source"]), meta.get("serp_query"), log=lambda m: print("  " + m))
    except AutoSourceError as e:
        sys.exit(f"auto_source: {e}")


def export_steps(conn, artifacts: ArtifactStore, job_id, out: Path) -> None:
    """out/<job>/steps/NN_<STATE>/<prompt>_<vòng>_a<lượt>.{prompt.md,output.txt,errors.txt} + timeline.md để debug."""
    steps = out / "steps"
    order: dict[str, int] = {}
    runs = conn.execute("SELECT * FROM prompt_runs WHERE job_id = %s ORDER BY created_at", (job_id,)).fetchall()
    for i, r in enumerate(runs):
        step_dir = steps / f"{order.setdefault(r['step'], len(order) + 1):02d}_{r['step']}"
        step_dir.mkdir(parents=True, exist_ok=True)
        rnd = (r["params"] or {}).get("round", 0)
        stem = f"{i + 1:03d}_{r['prompt_id']}_r{rnd}_a{r['repair_attempt']}_{r['status']}"
        uri = (r["request_redacted"] or {}).get("prompt_uri")
        if uri:
            (step_dir / f"{stem}.prompt.md").write_bytes(artifacts.blobs.get(uri))
        else:  # run cũ chưa lưu prompt: lấy từ task extension nếu có
            task = (r["params"] or {}).get("raw", {}).get("external_task_id")
            t = task and conn.execute("SELECT prompt_text FROM external_tasks WHERE id = %s", (task,)).fetchone()
            if t:
                (step_dir / f"{stem}.prompt.md").write_text(t["prompt_text"], encoding="utf-8")
        if r["response_raw_uri"]:
            (step_dir / f"{stem}.output.txt").write_bytes(artifacts.blobs.get(r["response_raw_uri"]))
        if r["validation_errors"]:
            (step_dir / f"{stem}.errors.txt").write_text("\n".join(r["validation_errors"]), encoding="utf-8")
    lines = ["| Giờ | Từ | → | Kết quả | Ai | Lý do |", "|---|---|---|---|---|---|"]
    for t in conn.execute("SELECT * FROM job_transitions WHERE job_id = %s ORDER BY id", (job_id,)):
        why = (t["reason"] or "").replace("|", "/").replace("\n", " ")[:400]
        lines.append(f"| {t['created_at']:%H:%M:%S} | {t['from_state']} | {t['to_state']} | {t['outcome']} | {t['actor'].split(':')[0]} | {why} |")
    (out / "timeline.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def export(conn, artifacts: ArtifactStore, job_id) -> Path:
    out = ROOT / "out" / str(job_id)
    out.mkdir(parents=True, exist_ok=True)
    export_steps(conn, artifacts, job_id, out)
    kinds = [r["kind"] for r in conn.execute("SELECT DISTINCT kind FROM artifacts WHERE job_id = %s", (job_id,))]
    for kind in kinds:
        art = artifacts.latest(conn, job_id, kind)
        name = kind.replace(":", "_")
        (out / f"{name}.json").write_text(json.dumps(art.payload, ensure_ascii=False, indent=2), encoding="utf-8")
        if kind == "ArticleDraft":
            (out / "article.md").write_text(art.payload["body_markdown"], encoding="utf-8")
        if kind == "FinalPackage":  # bài cuối để đăng: title + meta + nội dung đã bỏ marker section
            p = art.payload
            head = f"# {p['title']}\n\n> Meta: {p['meta']}\n> Slug: {p['slug']}\n\n" if p.get("title") else ""  # rework: không title/meta
            (out / "final.md").write_text(head + p["article_markdown"], encoding="utf-8")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", nargs="?", type=Path)
    ap.add_argument("--resume", help="job_id đã có (đang NEEDS_HUMAN thì chạy lại bước đã dừng)")
    ap.add_argument("--approve", metavar="LÝ_DO", help="cùng --resume: người duyệt thay gate đang dừng rồi chạy tiếp")
    ap.add_argument("--revise", metavar="HƯỚNG_DẪN",
                    help="cùng --resume: gửi gate đang dừng (KW/FACT/OUTLINE) về bước tạo artifact kèm hướng dẫn sửa")
    ap.add_argument("--auto-gates", action="store_true")
    ap.add_argument("--from", dest="from_state", metavar="STATE",
                    help="cùng --resume: tua job về STATE (vd. NEW_FACT_CHECK, WRITE) rồi chạy tiếp; bước trước lấy từ cache")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--cancel", metavar="JOB_ID", help="huỷ job (dừng script đang chạy job đó trước)")
    ap.add_argument("--timeout-minutes", type=float, default=60)
    args = ap.parse_args()
    if args.cancel:
        load_config()
        with connect(autocommit=True) as c:
            cancel_job(c, args.cancel, "run_job", "run_job --cancel")
        print(f"Đã huỷ job {args.cancel}")
        if not args.folder:
            return
    if not args.folder and not args.resume:
        ap.error("cần thư mục đầu vào hoặc --resume")

    overrides = {"gates": {"mode": {g: "auto" for g in GATES}}} if args.auto_gates else None
    config = load_config(overrides=overrides)
    conn = connect(autocommit=True)
    migrate(conn)
    artifacts = ArtifactStore(LocalBlobStore(ROOT / config.get("blob_dir", "var/blobs")))

    if args.resume:
        job_id = args.resume
        job = conn.execute("SELECT state, resume_state FROM jobs WHERE id = %s", (job_id,)).fetchone()
        if args.from_state:
            rewind_job(conn, job_id, State(args.from_state), "run_job", f"run_job --from {args.from_state}")
            print(f"Tua job về {args.from_state}")
        elif job and job["state"] in ("NEEDS_HUMAN", "STALE"):
            if args.approve:
                human_resolve(conn, job_id, "run_job", "approve", args.approve, config)
                print(f"Đã duyệt thay {job['resume_state']}: {args.approve}")
            elif args.revise:
                after = human_resolve(conn, job_id, "run_job", "revise", args.revise, config, artifacts)
                print(f"Gửi {job['resume_state']} về {after.state.value} kèm hướng dẫn: {args.revise}")
            else:
                human_resolve(conn, job_id, "run_job", "rerun", "run_job --resume: chạy lại bước đã dừng", config)
                print(f"Chạy lại bước {job['resume_state']}")
    else:
        meta, inp, source, knowledge = load_folder(args.folder)
        sources = auto_sources(meta, inp, config)
        print(f"Đầu vào: '{inp.topic}', {len(inp.keywords)} keyword, SERP {f'{len(inp.serp.organic)} URL ({inp.serp.provider})' if inp.serp else 'lấy qua Serper'}, {len(set((inp.serp.paa if inp.serp else []) + inp.paa_extra))} PAA, "
              f"bài gốc {'có' if inp.article_md else 'không'}, bài nguồn {len(sources) if sources else ('có' if source else 'không')}, "
              f"{len(knowledge)} mục kho")
        if args.dry_run:
            return
        import_knowledge(conn, meta["author_id"], knowledge)
        # Tác giả có trong bảng authors: lấy tên/URL và hồ sơ giọng văn từ đó khi job.yaml không tự khai
        from pipeline.authors import get_author, jsonld_type
        from pipeline.inputs import AuthorIn
        if row := get_author(conn, meta["author_id"]):
            inp = inp.model_copy(update={
                "author": inp.author or AuthorIn(name=row["name"], url=row["url"], type=jsonld_type(row)),
                "voice_profile": inp.voice_profile or row["voice_profile"]})
        # Mặc định full (nhánh A = làm mới đầy đủ); `flow: rework` trong job.yaml để chỉ viết lại câu chữ
        flow = meta.get("flow") or "full"
        site_id = meta.get("site_id")
        if site_id:  # job.yaml `site_id:` — kho dữ liệu và trang nội bộ của site đi theo job
            from pipeline.sites import get_site
            site = get_site(conn, site_id)
            if site is None:
                sys.exit(f"job.yaml: không có site '{site_id}' (xem: python scripts/site_tool.py list)")
            if not inp.internal_pages:
                inp = inp.model_copy(update={"internal_pages": JobInput.model_validate(
                    {"topic": "x", "keywords": [], "internal_pages": site["internal_pages"]}).internal_pages})
        job_id = create_job_with_input(conn, artifacts, inp, author_id=meta["author_id"], created_by="run_job",
                                       config=config, source_text=source, source_texts=sources, flow=flow, site_id=site_id)
        print(f"Tạo job {job_id}")

    worker = Worker(conn, config, artifacts, HANDLERS, services=Services.default(conn, config))
    listener = safe_listener("job_event")  # task xong → job chạy tiếp ngay (không chờ hết 5s)
    seen, deadline, waiting_printed = 0, time.time() + args.timeout_minutes * 60, None
    while time.time() < deadline:
        # Bỏ qua backoff retry của job này để chạy thử nhanh (không đụng job đang chờ extension)
        conn.execute("UPDATE jobs SET run_after = now() WHERE id = %s AND lease_until IS NULL AND run_after > now() "
                     "AND last_error NOT LIKE 'awaiting_external%%'", (job_id,))
        worker.run_once()
        for t in conn.execute("SELECT * FROM job_transitions WHERE job_id = %s ORDER BY id OFFSET %s", (job_id, seen)).fetchall():
            seen += 1
            note = f"  ({t['reason'][:160]})" if t["reason"] else ""
            print(f"  {t['created_at']:%H:%M:%S} {t['from_state']:<18} → {t['to_state']:<18} [{t['outcome']}]{note}")
        job = conn.execute("SELECT * FROM jobs WHERE id = %s", (job_id,)).fetchone()
        if job["state"] in STOP:
            break
        if (job["last_error"] or "").startswith("awaiting_external"):
            if waiting_printed != job["last_error"]:
                print(f"  … {job['state']}: chờ extension xử lý task {job['last_error'].split(':', 1)[1]}")
                waiting_printed = job["last_error"]
            if listener:
                listener.wait(5, lambda ch, payload: payload == str(job_id))
            else:
                time.sleep(5)
    else:
        print("Hết thời gian chờ; chạy lại với --resume để tiếp tục.")

    job = conn.execute("SELECT * FROM jobs WHERE id = %s", (job_id,)).fetchone()
    print(f"\nTrạng thái: {job['state']}" + (f" (dừng tại {job['resume_state']})" if job["resume_state"] else ""))
    if job["needs_human_reason"]:
        print(f"Lý do: {job['needs_human_reason']}")
    print(f"Chi phí: ${float(job['cost_usd']):.4f} / cap ${float(job['cost_cap_usd']):.2f}")
    print(f"Kết quả: {export(conn, artifacts, job_id)}")


if __name__ == "__main__":
    main()
