"""Step handler cho mốc M0: TRIAGE_AI → UNIQUENESS (B0–B8).

Quy ước:
- Handler không đổi state; trả StepResult. Output LLM lưu thành artifact (idempotent theo nội dung).
- Việc "sau khi gate approve" (duyệt fact, xoá text nguồn) chạy ở đầu bước kế tiếp, vì ở chế độ shadow/human
  người approve qua hàng chờ và job nhảy thẳng sang bước sau, không quay lại gate.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from urllib.parse import urlsplit

from gates import rules
from knowledge.repository import build_evidence, retrieve
from vnseo.edits import Edit, apply_edits
from vnseo.facts import claim_value_keys, scan_numbers
from vnseo.style import blacklist_scan, sentence_stats
from vnseo.uniqueness import containment_hashes, pack_hashes, shingle_hashes, unpack_hashes
from vnseo.vn_text.markdown import SECTION_MARKER
from vnseo.vn_text.normalize import normalize, strip_diacritics

from .. import autosource
from ..authors import get_author, record_learned_voice, style_only
from ..executor import GateDecision, StepContext, StepResult
from ..llm.base import ProviderError
from ..llm.runner import InvalidOutput
from ..states import Outcome as O
from ..urlcheck import relevant_excerpt
from . import branch_a
from .common import (
    StepFailed, add_warning, base_vars, datamark, datamark_char, feedback_for, finish_gate, job_input, job_row, latest,
    producer_family, run_gate, runner, save, save_feedback, sha, source_text, to_json, undatamark,
)

MODE_RANK = {"lite": 0, "standard": 1, "heavy": 2}
_HEADING = re.compile(r"^#{1,6}\s+(.*)$", re.M)
_BULLET = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s")


def _article_outline(article_md: str | None) -> str | None:
    """Dàn ý bài hiện tại (nhánh A). Bài dán từ web thường không có tiêu đề và heading không có dấu #:
    khi đó coi dòng ngắn đứng riêng, không phải bullet, không kết thúc bằng dấu câu là heading."""
    if not article_md or not article_md.strip():
        return None
    marked = _HEADING.findall(article_md)
    if marked:
        return "\n".join(marked)
    heads = []
    for line in (ln.strip() for ln in article_md.splitlines()):
        if (line and len(line) <= 150 and len(line.split()) <= 25 and not _BULLET.match(line)
                and line[-1] not in ".:;!?…,\"”'’)"):
            heads.append(line)
    return "\n".join(heads) or None


def _excerpt(text: str | None, limit: int = 4000) -> str:
    return (text or "")[:limit]


def _run(sctx: StepContext, prompt_id: str, variables: dict, hashes: list[str], **kw):
    """kw chuyển thẳng cho PromptRunner.run (ctx = dữ liệu cho validator, feedback, model_key…)."""
    return runner(sctx).run(job_id=sctx.job_id, step=sctx.state.state.value, prompt_id=prompt_id,
                            variables={**base_vars(sctx), **variables}, input_hashes=hashes, **kw)


def _approved(claims: list[dict]) -> list[dict]:
    return [c for c in claims if c["status"] == "DA_DUYET" and c["action"] != "BO"]


def _next_claim_index(claims: list[dict]) -> int:
    return max((int(c["claim_id"][1:]) for c in claims), default=0) + 1


# ---------- B0: phân loại ----------

def _triage_vars(ctx: StepContext) -> tuple[dict, list[str]]:
    inp, art = job_input(ctx)
    src = source_text(ctx)
    # Text thường, không JSON: model trích bằng chứng nguyên văn, JSON trong brief làm lọt dấu " vào output
    brief = "\n".join([
        f"Chủ đề: {inp.topic}",
        f"Mô tả: {inp.brief or '(không có)'}",
        f"Có bài hiện tại của site: {'có' if inp.article_md else 'không'}",
        f"Có bài của bên thứ ba làm tư liệu: {'có' if src else 'không'}",
        f"Số keyword: {len(inp.keywords)}",
    ])
    excerpt = _excerpt(inp.article_md or src, 3000)
    return ({"JOB_BRIEF": brief, "SOURCE_EXCERPT_MARKED": datamark(excerpt, datamark_char(ctx))}, [art.sha256])


def _triage_given(ctx: StepContext) -> bool:
    """job.yaml đã ghi đủ branch, mode, ymyl: không cần hỏi model (bỏ 2 lượt web)."""
    inp, _ = job_input(ctx)
    return bool(inp.branch and inp.mode and inp.ymyl is not None)


def triage_ai(ctx: StepContext) -> StepResult:
    if _triage_given(ctx):
        inp, _ = job_input(ctx)
        save(ctx, "TriageSuggestion", {"branch_suggest": inp.branch, "ymyl": inp.ymyl, "mode_suggest": inp.mode,
                                       "evidence": ["job.yaml"]})
        return StepResult(O.DONE)
    variables, hashes = _triage_vars(ctx)
    out = _run(ctx, "B0-H", variables, hashes)
    save(ctx, "TriageSuggestion", out.parsed, run_id=out.run_id)
    return StepResult(O.DONE)


def triage_gate(ctx: StepContext) -> StepResult:
    """Không dùng G: chạy lại B0-H bằng model họ khác, so hai kết quả (§4.5)."""
    first_art = latest(ctx, "TriageSuggestion")
    first = first_art.payload
    if _triage_given(ctx):
        second = first
    else:
        variables, hashes = _triage_vars(ctx)
        second = _run(ctx, "B0-H", variables, hashes, model_key="triage_second",
                      avoid_families=producer_family(ctx, first_art)).parsed
    inp, _ = job_input(ctx)

    failures = []
    branch = inp.branch or (first["branch_suggest"] if first["branch_suggest"] == second["branch_suggest"] else None)
    ymyl = inp.ymyl if inp.ymyl is not None else (first["ymyl"] if first["ymyl"] == second["ymyl"] else None)
    if branch is None:
        failures.append(f"hai model lệch nhánh: {first['branch_suggest']} / {second['branch_suggest']}")
    if ymyl is None:
        failures.append(f"hai model lệch YMYL: {first['ymyl']} / {second['ymyl']}")
    mode = inp.mode or max(first["mode_suggest"], second["mode_suggest"], key=MODE_RANK.get)
    if ymyl and MODE_RANK[mode] < MODE_RANK["standard"]:
        mode = "standard"  # YMYL tối thiểu Standard (§2)
    if branch == "A" and not inp.article_md:
        failures.append("nhánh A nhưng không có article_md")
    # Nhánh B không có bài nguồn = bài mới hoàn toàn (dữ kiện lấy từ nghiên cứu SERP + fact-check)

    save(ctx, "TriageDecision", {"branch": branch, "mode": mode, "ymyl": ymyl, "first": first, "second": second,
                                 "failures": failures})
    # Hai model lệch nhau: gate vẫn escalate, nhưng job luôn được ghi giá trị dùng được — nếu gate được duyệt thay
    # (autopilot/người) mà cột còn NULL thì các bước sau đổ vỡ (config["modes"][None]) và lặp mãi.
    # Chọn phía an toàn: YMYL = true (kéo mode lên ≥ standard), nhánh theo việc có bài gốc của site hay không.
    if ymyl is None:
        ymyl = True
        if MODE_RANK[mode] < MODE_RANK["standard"]:
            mode = "standard"
    if branch is None:
        branch = "A" if inp.article_md else "B"
    cap = ctx.config["cost"]["cap_usd_per_job"][mode]
    ctx.conn.execute("UPDATE jobs SET branch = %s, mode = %s, ymyl = %s, cost_cap_usd = %s WHERE id = %s",
                     (branch, mode, ymyl, cap, ctx.job_id))
    action = "escalate" if failures else "approve"
    d = GateDecision("TRIAGE", "ai", ctx.config["gates"]["mode"]["triage"], action, reason="; ".join(failures) or None)
    return finish_gate(ctx, "triage", action, "; ".join(failures), [d])


# ---------- B1: keyword ----------

def kw_normalize(ctx: StepContext) -> StepResult:
    inp, art = job_input(ctx)
    rows, seen = [], set()
    keywords = list(inp.keywords)
    if not keywords and inp.article_md:
        # Nhánh A không có file keyword: tự lấy keyword chính/phụ có sẵn trong bài (RWA, Haiku) — không bắt người nhập
        from .rework import brief
        b = brief(ctx, inp, art)
        keywords = [type("K", (), {"keyword": k, "volume": None})() for k in [b["primary_keyword"], *b["keywords"]] if k]
    for k in keywords:
        display = normalize(k.keyword, ctx.config["locale"]["tone_style"])
        key = display.lower()
        if not key or key in seen:
            continue
        seen.add(key)
        rows.append({"keyword_nfc": key, "display": display, "key_khongdau": strip_diacritics(display), "volume": k.volume})
    if not rows:
        raise StepFailed("danh sách keyword rỗng")
    rows.sort(key=lambda r: r["volume"] or 0, reverse=True)
    save(ctx, "KeywordInput", {"rows": rows})
    return StepResult(O.DONE)


def kw_map_ai(ctx: StepContext) -> StepResult:
    inp, _ = job_input(ctx)
    kin = latest(ctx, "KeywordInput")
    rows = kin.payload["rows"]
    size = ctx.config["thresholds"]["kw_map_rows_per_batch"]
    outline = _article_outline(inp.article_md)
    feedback = feedback_for(ctx, "kw")
    merged = {"rows": [], "entities": [], "questions_for_human": []}
    run_id = None
    for i in range(0, len(rows), size):
        batch = rows[i:i + size]
        out = _run(ctx, "P1", {"TOPIC": inp.topic, "ARTICLE_OUTLINE_OR_NULL": outline, "KEYWORD_ROWS_JSON": to_json(batch),
                               "ROW_COUNT": len(batch)}, [kin.sha256, str(i)], feedback=feedback,
                   ctx={"input_rows": batch, "secondary_batch": i > 0})
        merged["rows"] += out.parsed["rows"]
        merged["entities"] += [e for e in out.parsed["entities"] if e not in merged["entities"]]
        merged["questions_for_human"] += out.parsed["questions_for_human"]
        run_id = out.run_id
        ctx.heartbeat()
    merged["questions_for_human"] = merged["questions_for_human"][:5]
    save(ctx, "KeywordMap", merged, run_id=run_id)
    return StepResult(O.DONE)


def kw_gate(ctx: StepContext) -> StepResult:
    km = latest(ctx, "KeywordMap")
    rule = rules.kw_rules(latest(ctx, "KeywordInput").payload["rows"], km.payload)
    inp, _ = job_input(ctx)
    return run_gate(ctx, "kw", "KeywordMap", km.payload, {"topic": inp.topic, "brief": inp.brief}, rule,
                    producer_family(ctx, km))


# ---------- B2 + B3: fact và giọng văn ----------

def _is_new_article(ctx: StepContext) -> bool:
    """Nhánh B mà chưa từng có bài nguồn (không text, không fingerprint): viết bài mới hoàn toàn."""
    if job_row(ctx)["branch"] != "B" or source_text(ctx):
        return False
    return ctx.conn.execute("SELECT 1 FROM source_fingerprints WHERE job_id = %s LIMIT 1", (ctx.job_id,)).fetchone() is None


def _refetch_sources(ctx: StepContext, inp) -> str | None:
    """Text nguồn đã xoá sau FACT_GATE (ADR-6) mà job quay lại bước trích fact (autopilot): tải lại từ source_urls."""
    fetch = getattr(ctx.services, "url_main_text", None)
    if not inp.source_urls or fetch is None:
        return None
    max_chars = ctx.config["adapters"].get("auto_source", {}).get("max_chars_per_source", 12000)
    from ..autosource import Source, _cut
    with ctx.conn.transaction():
        for i, url in enumerate(inp.source_urls, 1):
            status, text = fetch(url)
            if status == 200 and text:
                body = Source(0, url, "", _cut(text, max_chars)).as_source_text(i)
                ctx.conn.execute("INSERT INTO source_texts (job_id, sha256, text) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                                 (ctx.job_id, hashlib.sha256(body.encode()).hexdigest(), body))
    return source_text(ctx)


def _fact_source(ctx: StepContext) -> tuple[str, str]:
    job = job_row(ctx)
    inp, _ = job_input(ctx)
    if job["branch"] == "B":
        text = source_text(ctx) or _refetch_sources(ctx, inp)
        if not text:
            if _is_new_article(ctx):
                return "", "bai_nguon"
            raise StepFailed("text bài nguồn đã bị xoá hoặc chưa nhập")
        return text, "bai_nguon"
    return inp.article_md or "", "bai_goc"


def fact_voice_fanout(ctx: StepContext) -> StepResult:
    job = job_row(ctx)
    inp, inp_art = job_input(ctx)
    text, origin = _fact_source(ctx)
    mark = datamark_char(ctx)
    marked = datamark(text, mark) if origin == "bai_nguon" else text
    text_hash = hashlib.sha256(text.encode()).hexdigest()

    claims, run_id = [], None
    if text.strip():  # bài mới hoàn toàn: không có bài để trích claim
        out = _run(ctx, "P2a", {"ARTICLE_MD": marked, "ORIGIN": origin}, [text_hash], feedback=feedback_for(ctx, "fact"),
                   ctx={"source_text": marked, "first_claim_index": 1, "datamark": mark if origin == "bai_nguon" else None})
        claims = out.parsed["claims"]
        run_id = out.run_id
        ctx.heartbeat()

    if job["mode"] != "lite" and text.strip():
        known = set()
        for c in claims:
            known |= claim_value_keys(c["verbatim"])
        # Quét trên text gốc: ký tự datamark (vd. "ˆ" là \w) dính vào số làm regex không nhận ra số nào
        unmatched = [h.raw for h in scan_numbers(text) if h.key not in known]
        nxt = _next_claim_index(claims)
        out2 = _run(ctx, "P2b", {"ARTICLE_MD": marked, "FACT_INVENTORY_JSON": to_json({"claims": claims}),
                                 "UNMATCHED_NUMBERS_JSON": to_json(unmatched), "NEXT_CLAIM_ID": f"C{nxt:03d}"},
                    [text_hash, run_id.hex],
                    ctx={"source_text": marked, "first_claim_index": nxt, "existing_verbatims": [c["verbatim"] for c in claims],
                         "datamark": mark if origin == "bai_nguon" else None})
        claims += out2.parsed["claims"]
        ctx.heartbeat()

    if origin == "bai_nguon":
        claims = [{**c, "verbatim": undatamark(c["verbatim"], mark)} for c in claims]
    save(ctx, "FactInventory", {"claims": claims}, run_id=run_id)

    author = get_author(ctx.conn, job["author_id"])
    learned = _learn_voice(ctx, job, inp_art, author, text if origin == "bai_nguon" else None)
    if learned:  # hồ sơ của tác giả vừa được học thêm từ nguồn SERP của job này
        save(ctx, "VoiceProfile", learned[0], run_id=learned[1])
    elif author and author["voice_profile"]:  # hồ sơ hiện có của tác giả (kể cả vừa dựng sau khi tạo job)
        save(ctx, "VoiceProfile", author["voice_profile"])
    elif inp.voice_profile:  # hồ sơ khai sẵn trong job: dùng lại, khỏi chạy P3
        save(ctx, "VoiceProfile", inp.voice_profile)
    elif job["mode"] != "lite":
        samples = inp.article_md if job["branch"] == "A" else inp.voice_samples_md
        if samples:
            st = sentence_stats(samples).__dict__
            vp = _run(ctx, "P3", {"VOICE_SAMPLES_MD": samples, "SENTENCE_STATS_JSON": to_json(st)},
                      [inp_art.sha256, hashlib.sha256(samples.encode()).hexdigest()], ctx={"source_text": samples})
            save(ctx, "VoiceProfile", vp.parsed, run_id=vp.run_id)
        else:
            # Không có giọng văn không làm hỏng bài (fact đã trích xong): viết theo giọng trung tính, bỏ chấm P8b
            add_warning(ctx, "voice", "skip", "không có hồ sơ giọng văn: tác giả chưa có hồ sơ, job không có bài mẫu "
                                              "và không có nguồn SERP để học — bài viết theo giọng trung tính")
    return StepResult(O.DONE)


def _learn_voice(ctx: StepContext, job: dict, inp_art, author: dict | None, source: str | None) -> tuple[dict, object] | None:
    """Tự học giọng văn dần từ các trang top SERP của job (config voice_learning). Mỗi job học một lần: chạy P3 trên
    nguồn của job, kèm hồ sơ đang có để model điều chỉnh thay vì làm lại từ đầu; lưu vào tác giả bản đã bỏ câu trích
    (authors.style_only). Dừng khi đủ max_jobs, hoặc khi tác giả có hồ sơ dựng từ bài của chính họ."""
    vl = ctx.config.get("voice_learning") or {}
    if not (vl.get("enabled") and author and source and source.strip()) or author["voice_origin"] == "author":
        return None
    done = author["voice_learned_from"] or []
    if str(ctx.job_id) in done or len(done) >= vl.get("max_jobs", 5):
        return None
    samples = source[: vl.get("max_sample_chars", 30000)]
    if len(samples.split()) < vl.get("min_sample_words", 600):
        return None
    feedback = None
    if author["voice_profile"]:
        current = [{"dim_id": d["dim_id"], "name": d["name"], "description": d["description"]}
                   for d in author["voice_profile"].get("dimensions", [])]
        feedback = (f"Hồ sơ giọng văn hiện tại, học từ {len(done)} lượt trước (các bài khác cùng lĩnh vực):\n{to_json(current)}\n"
                    "Giữ những nhận xét vẫn đúng với mẫu lần này, điều chỉnh chỗ mẫu lần này cho thấy khác. "
                    "Mọi câu trích vẫn phải lấy nguyên văn từ mẫu lần này.")
    try:
        vp = _run(ctx, "P3", {"VOICE_SAMPLES_MD": samples, "SENTENCE_STATS_JSON": to_json(sentence_stats(samples).__dict__)},
                  [inp_art.sha256, hashlib.sha256(samples.encode()).hexdigest()],
                  ctx={"source_text": samples, "voice_learning": True}, feedback=feedback)
    except InvalidOutput as e:
        # Học giọng văn là phần thêm: hỏng thì bài vẫn phải chạy tiếp (dùng hồ sơ đang có hoặc giọng trung tính),
        # không để cả job kẹt ở FACT_VOICE_FANOUT và tốn tiền chạy lại P3
        add_warning(ctx, "voice", "learn_failed", f"không học được giọng văn từ nguồn SERP của job này: {e}")
        return None
    n = record_learned_voice(ctx.conn, author["id"], vp.parsed, ctx.job_id, f"tự học từ nguồn SERP của {len(done) + 1} job")
    add_warning(ctx, "voice", "learned", f"giọng văn của tác giả '{author['name']}' được học từ các trang top SERP "
                                         f"(lượt {n}/{vl.get('max_jobs', 5)}); dựng hồ sơ từ bài của chính tác giả bằng "
                                         "scripts/author_tool.py voice để thay thế")
    return style_only(vp.parsed), vp.run_id


def fact_gate(ctx: StepContext) -> StepResult:
    art = latest(ctx, "FactInventory")
    inv = rules.normalize_fact_actions(art.payload)
    if inv != art.payload:
        art = save(ctx, "FactInventory", inv)
    if not inv["claims"]:  # bài mới hoàn toàn: không có claim để AI duyệt
        return StepResult(O.APPROVE)
    text, _ = _fact_source(ctx)
    job = job_row(ctx)
    models = ctx.config["models"]
    # FactInventory do P2a (+ P2b nếu không phải Lite) tạo → gate phải khác họ với cả hai
    families = {models["fact_extract"]["provider"]} | ({models["fact_recall"]["provider"]} if job["mode"] != "lite" else set())
    return run_gate(ctx, "fact", "FactInventory", inv, {"article_excerpt": _excerpt(text, 6000), "ymyl": job["ymyl"]},
                    rules.fact_rules(inv), families)


def ensure_fact_approved(ctx: StepContext) -> None:
    """Chạy sau FACT_GATE (auto hoặc người approve): duyệt fact; nhánh B lưu fingerprint rồi xoá text nguồn."""
    art = latest(ctx, "FactInventory")
    if any(c["status"] == "CHUA_DUYET" for c in art.payload["claims"]):
        save(ctx, "FactInventory", rules.approve_fact_inventory(rules.normalize_fact_actions(art.payload)))
    if job_row(ctx)["branch"] != "B":
        return
    n = ctx.config["thresholds"]["shingle_n"]
    with ctx.conn.transaction():
        for row in ctx.conn.execute("SELECT sha256, text FROM source_texts WHERE job_id = %s", (ctx.job_id,)).fetchall():
            ctx.conn.execute(
                """INSERT INTO source_fingerprints (job_id, source_sha256, shingle_n, shingle_hashes)
                   VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                (ctx.job_id, row["sha256"], n, pack_hashes(shingle_hashes(row["text"], n))))
        ctx.conn.execute("DELETE FROM source_texts WHERE job_id = %s", (ctx.job_id,))


# ---------- B4: SERP, gap, IG ----------

def serp_input(ctx: StepContext) -> StepResult:
    ensure_fact_approved(ctx)
    inp, _ = job_input(ctx)
    serp = inp.serp
    if serp is None or not serp.organic:
        provider = ctx.services.serp_provider
        if provider is None:
            return StepResult(O.ESCALATE, "SERP_MISSING: cần upload SERP (top 10, PAA, AIO) vào JobInput rồi chạy lại")
        km = latest(ctx, "KeywordMap").payload
        primary = next(r["display"] for r in km["rows"] if r["role"] == "primary")
        try:
            serp = provider.fetch(primary)  # TransientError → executor retry
        except ProviderError as e:
            return StepResult(O.ESCALATE, f"SERP_MISSING: {e}; nhập SERP tay hoặc kiểm tra key")
        if not serp.organic:
            return StepResult(O.ESCALATE, f"SERP_MISSING: provider không trả kết quả cho '{primary}'")
    paa = list(dict.fromkeys([*serp.paa, *inp.paa_extra]))
    save(ctx, "SerpSnapshot", {**serp.model_dump(mode="json"), "paa": paa,
                               "fetched_at": dt.datetime.now(dt.UTC).isoformat()})
    return StepResult(O.DONE)


def _knowledge_index(ctx: StepContext) -> list[dict]:
    rows = ctx.conn.execute(
        """SELECT id, title, kind, topic_tags FROM author_knowledge
           WHERE (author_id = %s OR site_id = %s) AND status = 'active' AND valid_until >= current_date ORDER BY id""",
        (ctx.job["author_id"], ctx.job.get("site_id"))).fetchall()
    return [{"entry_id": r["id"], "title": r["title"], "kind": r["kind"], "topic_tags": r["topic_tags"]} for r in rows]


def gap_ig_ai(ctx: StepContext) -> StepResult:
    job = job_row(ctx)
    inp, _ = job_input(ctx)
    serp = latest(ctx, "SerpSnapshot")
    km = latest(ctx, "KeywordMap")
    facts = latest(ctx, "FactInventory")
    mark = datamark_char(ctx)
    competitor = "\n".join(f"[{o['rank']}] {o['title']}: {o['snippet']}" for o in serp.payload["organic"])

    hashes = [serp.sha256, km.sha256, facts.sha256]
    if job["mode"] == "heavy":
        # Báo cáo chỉ phụ thuộc chủ đề: chạy một lần mỗi job, vòng revise outline dùng lại (Deep Research 10–30 phút/lượt)
        existing = latest(ctx, "DeepResearch", required=False)
        if existing:
            report = existing.payload["markdown"]
        else:
            dr = _run(ctx, "P4-DR", {"TOPIC": inp.topic, "YEAR": dt.date.today().year}, [inp.topic])
            save(ctx, "DeepResearch", {"markdown": dr.text}, run_id=dr.run_id)
            report = dr.text
        competitor += "\n\n" + report
        ctx.heartbeat()

    research = (_gap_research(ctx, inp, serp.payload, [r["display"] for r in km.payload["rows"]])
                if job["mode"] != "lite" else [])
    if research:
        competitor += "\n\n" + "\n\n".join(f"[Nguồn nghiên cứu | {r['url']}]\n{r['excerpt']}" for r in research)
        hashes = hashes + [hashlib.sha256(to_json([r["url"] for r in research]).encode()).hexdigest()]

    index = _knowledge_index(ctx)
    approved = _approved(facts.payload["claims"])
    orig = branch_a.original_sections(inp.article_md) if job["branch"] == "A" else []
    prompt_id = "P4-D" if job["mode"] == "lite" else "P4-G"
    out = _run(ctx, prompt_id, {
        "SERP_JSON": to_json({k: serp.payload[k] for k in ("organic", "paa", "aio_present", "aio_cited_urls")}),
        "COMPETITOR_SUMMARIES_MARKED": datamark(competitor, mark), "KEYWORD_MAP_JSON": to_json(km.payload),
        "APPROVED_FACTS_JSON": to_json(approved), "KNOWLEDGE_INDEX_JSON": to_json(index),
        "REVISE_INSTRUCTIONS_OR_NULL": feedback_for(ctx, "outline"),
        "ORIGINAL_OUTLINE_OR_NULL": to_json(branch_a.outline_brief(orig)) if orig else None,
    }, hashes + [hashlib.sha256(to_json(index).encode()).hexdigest()],
        ctx={"approved_fact_ids": [c["claim_id"] for c in approved], "serp_urls": [o["url"] for o in serp.payload["organic"]]})
    plan = out.parsed
    if orig:  # nhánh A: không để outline bỏ sót section nào của bài gốc
        plan["outline"], notes = branch_a.cover_original(plan["outline"], orig)
        if notes:
            add_warning(ctx, "outline", "autofix", "section gốc bị outline bỏ sót, đã gán lại: " + "; ".join(notes))
    save(ctx, "GapIgPlan", plan, run_id=out.run_id)
    return StepResult(O.DONE)


_QUESTION_KW = re.compile(r"là gì|bao nhiêu|như thế nào|thế nào|tại sao|vì sao|ở đâu|khi nào|bao lâu|có nên|\?|^cách ", re.I)


def _gap_research(ctx: StepContext, inp, serp: dict, keywords: list[str] = ()) -> list[dict]:
    """Tài liệu nghiên cứu cho P4 do code tìm và tải (config gap_research). Qua proxy/extension, Gemini không tự tra web
    nên P4 không đề xuất được dữ kiện mới có nguồn → 0 IG có dữ liệu thật. Ở đây code tìm theo các câu hỏi người dùng
    hay hỏi (PAA) và chủ đề, bỏ trang bị chặn và các trang job đã lấy làm tư liệu (cần nguồn MỚI), tải đoạn liên quan.
    P4 chỉ được lấy new_claims từ các khối này; NEW_FACT_CHECK kiểm lại từng claim với đúng trang đó.
    Kết quả lưu ở artifact GapResearch: vòng revise outline và bước chạy lại dùng lại, không tìm lại."""
    cfg = ctx.config.get("gap_research") or {}
    search, fetch = ctx.services.serp_provider, ctx.services.url_fetcher
    if not cfg.get("enabled") or search is None or fetch is None:
        return []
    # Serper không trả PAA tiếng Việt: keyword dạng câu hỏi trong bộ keyword của job cũng là câu hỏi thật của người tìm
    questions = [k for k in keywords if _QUESTION_KW.search(k)]
    queries = list(dict.fromkeys([*serp.get("paa", []), *inp.paa_extra, *questions, inp.topic]))[: cfg.get("max_searches", 4)]
    key = hashlib.sha256(to_json(queries).encode()).hexdigest()
    cached = latest(ctx, "GapResearch", required=False)
    if cached and cached.payload.get("key") == key:
        return cached.payload["items"]

    def host(url: str) -> str:
        try:
            return urlsplit(url).netloc.lower().removeprefix("www.")
        except ValueError:
            return ""

    own = {host(u) for u in inp.source_urls} - {""}
    block_cfg = ctx.config.get("adapters", {}).get("auto_source", {})
    width = max(300, cfg.get("excerpt_chars", 3000) // 2)
    items, seen = [], set()
    for q in queries:
        picked = 0
        for o in search.fetch(q).organic:
            if picked >= cfg.get("results_per_query", 2):
                break
            if o.url in seen or autosource._blocked(o.url, block_cfg) or host(o.url) in own:
                continue
            status, page = fetch(o.url)
            excerpt = relevant_excerpt(page, q, width=width, windows=1) if status == 200 and page else ""
            if len(excerpt) < 200:
                continue
            seen.add(o.url)
            items.append({"query": q, "url": o.url, "title": o.title, "excerpt": excerpt})
            picked += 1
        ctx.heartbeat()
    save(ctx, "GapResearch", {"key": key, "queries": queries, "items": items})
    return items


# ---------- B5: chọn dữ liệu từ kho ----------

def knowledge_select(ctx: StepContext) -> StepResult:
    job = job_row(ctx)
    plan_art = latest(ctx, "GapIgPlan")
    plan = plan_art.payload
    kcfg = ctx.config["knowledge"]
    cands = retrieve(ctx.conn, ctx.job_id, job["author_id"], plan["ig"], kcfg["score_weights"],
                     k=kcfg["candidates_per_ig"], max_reuse=kcfg["max_reuse_per_entry"], site_id=job.get("site_id"))
    by_ig = {ig: [c.to_prompt() for c in lst] for ig, lst in cands.items()}
    contents = {c.entry_id: c.content for lst in cands.values() for c in lst}
    max_entries = ctx.config["modes"][job["mode"]]["max_entries"]

    out = _run(ctx, "P5", {"GAP_IG_JSON": to_json(plan), "KNOWLEDGE_CANDIDATES_JSON": to_json(by_ig),
                           "MAX_ENTRIES": max_entries}, [plan_art.sha256, hashlib.sha256(to_json(by_ig).encode()).hexdigest()],
               ctx={"candidate_contents": contents, "max_entries": max_entries})
    selection = out.parsed
    save(ctx, "KnowledgeSelection", selection, run_id=out.run_id)

    kb_igs = {ig["ig_id"] for ig in plan["ig"] if ig["owner"] == "KHO_TAC_GIA"}
    dropped = sorted(kb_igs & set(selection["unfilled_ig_ids"]))
    remaining = [ig for ig in plan["ig"] if ig["ig_id"] not in dropped]
    if len(remaining) < ctx.config["thresholds"]["ig_min"] and ctx.config["gates"].get("outline_autofix", True):
        add_warning(ctx, "outline", "autofix", f"kho tác giả không có dữ liệu cho {dropped}; bỏ các IG này, đi tiếp với "
                                               f"{len(remaining)} IG")
    elif len(remaining) < ctx.config["thresholds"]["ig_min"]:
        save_feedback(ctx, "outline", "; ".join(f"IG {i} không có dữ liệu trong kho, thay bằng IG owner = AI" for i in dropped))
        return StepResult(O.REVISE, f"IG thiếu dữ liệu kho: {dropped}")
    if dropped:
        plan = {**plan, "ig": remaining,
                "outline": [{**s, "ig_ids": [i for i in s["ig_ids"] if i not in dropped]} for s in plan["outline"]]}
        save(ctx, "GapIgPlan", plan)

    facts = latest(ctx, "FactInventory").payload["claims"]
    base = [c for c in facts if c["origin"] != "kho_tac_gia"]  # P5 chạy lại: thay claim kho cũ
    evidence, kb_claims = build_evidence(ctx.conn, ctx.job_id, selection, _next_claim_index(base))
    save(ctx, "AuthorEvidence", evidence)
    save(ctx, "FactInventory", {"claims": base + kb_claims})
    return StepResult(O.DONE)


# ---------- NEW_FACT_CHECK: số liệu mới từ B4 ----------

def new_fact_check(ctx: StepContext) -> StepResult:
    plan = latest(ctx, "GapIgPlan").payload
    facts = latest(ctx, "FactInventory").payload["claims"]
    base = [c for c in facts if c["origin"] != "nghien_cuu_b4"]
    if not plan["new_claims"]:
        save(ctx, "NewFactCheck", {"items": [], "verified_ig_ids": []})
        if base != facts:
            save(ctx, "FactInventory", {"claims": base})
        return StepResult(O.DONE)

    # Code tự tải source_url mà P4 đưa ra và đưa đoạn liên quan vào prompt: model chỉ đối chiếu,
    # không phải tự tìm web (Gemini/ChatGPT web thường không trích được nguồn → loại oan claim có thật).
    start = _next_claim_index(base)
    fetch = ctx.services.url_fetcher
    mark = datamark_char(ctx)
    claims, provided, blocks = [], {}, []
    for i, c in enumerate(plan["new_claims"]):
        cid = f"C{start + i:03d}"
        claims.append({**c, "claim_id": cid})
        status, text = fetch(c["source_url"]) if fetch else (None, "")
        excerpt = relevant_excerpt(text, c["verbatim"]) if text else ""
        provided[cid] = {c["source_url"]: excerpt} if excerpt else {}
        body = datamark(excerpt, mark) if excerpt else f"(không tải được nguồn: HTTP {status})"
        blocks.append(f'<claim claim_id="{cid}" type="{c["type"]}">{c["verbatim"]}</claim>\n'
                      f'<nguon_ben_ngoai claim_id="{cid}" source_url="{c["source_url"]}">{body}</nguon_ben_ngoai>')
        ctx.heartbeat()

    items, added, verified_igs, factcheck, run_id = [], [], set(), None, None
    if any(provided.values()):
        text_block = "\n\n".join(blocks)
        out = _run(ctx, "P10-S", {"CLAIMS_WITH_SOURCES": text_block, "TODAY": dt.date.today().isoformat()},
                   [hashlib.sha256(text_block.encode()).hexdigest()],
                   ctx={"provided_sources": provided, "datamark": mark})
        factcheck, run_id = out.parsed, out.run_id
    by_id = {it["claim_id"]: it for it in (factcheck or {"items": []})["items"]}

    for c in claims:
        it = by_id.get(c["claim_id"])
        # Validator đã bắt quote phải nằm trong văn bản nguồn đã tải, nên DUNG ở đây là có căn cứ
        # DUNG: giữ claim. CAN_CAP_NHAT: nguồn chỉ xác nhận một phần/giá trị khác → giữ câu đã sửa theo nguồn (current_value).
        # Validator đã bắt quote phải nằm trong văn bản nguồn đã tải, nên cả hai đều có căn cứ.
        verdict = it["verdict"] if it else "KHONG_TAI_DUOC_NGUON"
        fixed = (it or {}).get("current_value", "").strip()
        ok = bool(provided[c["claim_id"]]) and (verdict == "DUNG" or (verdict == "CAN_CAP_NHAT" and bool(fixed)))
        items.append({"claim_id": c["claim_id"], "verdict": verdict, "source_fetched": bool(provided[c["claim_id"]]),
                      "accepted": ok, "used_text": (fixed if verdict == "CAN_CAP_NHAT" else c["verbatim"]) if ok else None})
        if not ok:
            continue
        added.append({"claim_id": c["claim_id"], "verbatim": fixed if verdict == "CAN_CAP_NHAT" else c["verbatim"],
                      "type": c["type"],
                      "in_article_source": c["source_url"], "maybe_outdated": False, "outdated_reason": "",
                      "action": "GIU", "new_value": None, "verification_source": c["source_url"],
                      "status": "DA_DUYET", "origin": "nghien_cuu_b4"})
        if c["ig_id"]:
            verified_igs.add(c["ig_id"])
    save(ctx, "NewFactCheck", {"items": items, "verified_ig_ids": sorted(verified_igs), "factcheck": factcheck},
         run_id=run_id)
    save(ctx, "FactInventory", {"claims": base + added})
    return StepResult(O.DONE)


def _autofix_outline(ctx: StepContext, plan: dict, sel: dict, verified: set[str], fact_ids: set[str]) -> tuple[dict, list[str]]:
    """Sửa outline bằng code thay vì vòng revise: bỏ fact_ids không tồn tại (model tự đặt mã cho số liệu mới)
    và bỏ IG mà mọi số liệu mới đều không kiểm chứng được. Claim chưa kiểm chứng không vào FactInventory nên writer không dùng được."""
    notes = []
    kb = {s["ig_id"] for s in sel["selections"]}
    claimed = {c["ig_id"] for c in plan.get("new_claims", []) if c.get("ig_id")}
    drop_igs = claimed - verified - kb
    outline = []
    for s in plan["outline"]:
        bad_f = [f for f in s["fact_ids"] if f not in fact_ids]
        bad_i = [i for i in s["ig_ids"] if i in drop_igs]
        if bad_f:
            notes.append(f"{s['section_id']}: bỏ fact_ids không tồn tại {bad_f}")
        if bad_i:
            notes.append(f"{s['section_id']}: bỏ {bad_i} (số liệu mới không kiểm chứng được)")
        outline.append({**s, "fact_ids": [f for f in s["fact_ids"] if f in fact_ids],
                        "ig_ids": [i for i in s["ig_ids"] if i not in drop_igs]})
    fixed = {**plan, "outline": outline, "ig": [i for i in plan["ig"] if i["ig_id"] not in drop_igs]}
    return fixed, notes


def outline_gate(ctx: StepContext) -> StepResult:
    plan = latest(ctx, "GapIgPlan")
    sel = latest(ctx, "KnowledgeSelection").payload
    nfc_ = latest(ctx, "NewFactCheck").payload
    facts = latest(ctx, "FactInventory").payload["claims"]
    fact_ids = {c["claim_id"] for c in _approved(facts)}
    skip = frozenset()
    if ctx.config["gates"].get("outline_autofix", True):
        fixed, notes = _autofix_outline(ctx, plan.payload, sel, set(nfc_["verified_ig_ids"]), fact_ids)
        if notes:
            plan = save(ctx, "GapIgPlan", fixed)
            add_warning(ctx, "outline", "autofix", "\n".join(notes))
        backed = ({s["ig_id"] for s in sel["selections"]} | set(nfc_["verified_ig_ids"])) & {i["ig_id"] for i in fixed["ig"]}
        if len(backed) < ctx.config["thresholds"]["ig_min"]:
            # Kho trống/số liệu mới không kiểm chứng được thì revise cũng không sinh thêm dữ liệu thật → bỏ O2/O3 khỏi gate
            skip = frozenset({"O2", "O3"})
            add_warning(ctx, "outline", "autofix",
                        f"chỉ {len(backed)} IG có dữ liệu thật (cần {ctx.config['thresholds']['ig_min']}); đi tiếp với IG hiện có")
    rule = rules.outline_rules(plan.payload, sel, nfc_["verified_ig_ids"], fact_ids,
                               ctx.config["thresholds"]["ig_min"] if not ctx.config["gates"].get("outline_autofix", True) else 0)
    serp = latest(ctx, "SerpSnapshot").payload
    fam = producer_family(ctx, plan) or {ctx.config["models"]["gap_ig"]["provider"]}
    km = latest(ctx, "KeywordMap").payload
    fb = latest(ctx, "Feedback:outline", required=False)
    context = {
        "paa": serp["paa"], "knowledge_selection": sel, "verified_ig_ids": nfc_["verified_ig_ids"],
        # PAA thuộc các keyword này dành cho trang khác, không bắt outline trả lời (checklist O4)
        "keyword_trang_rieng": [r["display"] for r in km["rows"] if r["role"] == "chuyen_trang_khac"],
        "huong_dan_bien_tap": fb.payload["text"] if fb and fb.payload["text"].startswith("Hướng dẫn của biên tập viên") else None,
    }
    return run_gate(ctx, "outline", "GapIgPlan", plan.payload, context, rule, fam, skip_checks=skip)


# ---------- Preflight ----------

def preflight(ctx: StepContext) -> StepResult:
    km = latest(ctx, "KeywordMap").payload
    plan = latest(ctx, "GapIgPlan").payload
    facts = latest(ctx, "FactInventory").payload
    evidence = latest(ctx, "AuthorEvidence").payload
    backed = set(latest(ctx, "NewFactCheck").payload["verified_ig_ids"]) | {i["ig_id"] for i in evidence["items"]}

    script = []
    status = {c["claim_id"]: c["status"] for c in facts["claims"]}
    for s in plan["outline"]:
        script += [{"type": "claim_chua_duyet", "where": f"outline.{s['section_id']}", "quote": f, "blocking": True}
                   for f in s["fact_ids"] if status.get(f) != "DA_DUYET"]
    script += [{"type": "ig_khong_nguon", "where": f"ig.{ig['ig_id']}", "quote": ig["description"], "blocking": False}
               for ig in plan["ig"] if ig["ig_id"] not in backed]
    primary = next((r["display"] for r in km["rows"] if r["role"] == "primary"), "")
    headings = " ".join(strip_diacritics(s["heading"]) for s in plan["outline"])
    if primary and strip_diacritics(primary) not in headings:
        script.append({"type": "keyword_xung_dot", "where": "outline", "quote": primary, "blocking": False})

    data = {"KEYWORD_MAP_JSON": to_json(km), "FACT_INVENTORY_JSON": to_json(facts), "GAP_IG_JSON": to_json(plan),
            "AUTHOR_EVIDENCE_JSON": to_json(evidence), "PF_SCRIPT_RESULTS_JSON": to_json(script)}
    out = _run(ctx, "PF", data, [hashlib.sha256(v.encode()).hexdigest() for v in data.values()],
               ctx={"source_text": "\n".join(data.values())})
    issues = script + out.parsed["issues"]
    save(ctx, "PreflightIssues", {"issues": issues}, run_id=out.run_id)

    blocking = [i for i in issues if i["blocking"]]
    if not blocking:
        return StepResult(O.APPROVE)
    text = "\n".join(f"- [{i['type']}] {i['where']}: {i['quote'][:200]}" for i in blocking)
    outline_issue = all(i["type"] in ("ig_khong_nguon", "thieu_du_lieu", "claim_chua_duyet")
                        or i["where"].startswith(("outline", "ig")) for i in blocking)
    revise_left = ctx.config["gates"]["revise_max"].get("outline", 0) - ctx.state.revise_count.get("outline", 0)
    if outline_issue and revise_left > 0:
        save_feedback(ctx, "outline", "Preflight phát hiện:\n" + text)
        return StepResult(O.REVISE, "preflight blocking ở outline/IG")
    # claim_chua_duyet do script bắt là lỗi dữ liệu thật (outline dùng claim chưa duyệt) → luôn dừng
    script_blocking = any(i in script for i in blocking)
    if not script_blocking and ctx.config["gates"].get("on_ai_escalate", "human") == "proceed":
        add_warning(ctx, "preflight", "escalate", text)
        return StepResult(O.APPROVE, "đi tiếp dù preflight có vấn đề (xem Warnings)")
    return StepResult(O.ESCALATE, "preflight blocking:\n" + text)


# ---------- B6: viết ----------

_FACT_SENTENCE_MAX = 220


def _facts_for_writer(claims: list[dict], mode: str = "sentence") -> list[dict]:
    """Nhánh B (ADR-6): writer không bao giờ thấy đoạn văn nguồn.

    mode = "sentence" (mặc định, lệch spec): mỗi claim bài nguồn là MỘT câu dữ kiện ngắn (≤ 220 ký tự) — dữ kiện không
      có bản quyền, còn chống chép câu chữ do prompt (bắt diễn đạt lại) và UNIQUENESS (containment < 10%) lo.
      Chỉ đưa con số thì claim không số ("Ngoại hạng Anh phát trên FPT Play") thành rỗng, bài viết chung chung.
    mode = "values" (đúng spec): chỉ truyền giá trị đã chuẩn hoá (số, ngày).
    """
    out = []
    for c in _approved(claims):
        if c["origin"] == "bai_nguon":
            if mode == "values":
                values = sorted(claim_value_keys(c["new_value"] or c["verbatim"]))
                text = c["new_value"] or ", ".join(v.split(":", 1)[1] for v in values)
            else:
                text = (c["new_value"] or c["verbatim"]).strip()
                text = text if len(text) <= _FACT_SENTENCE_MAX else text[:_FACT_SENTENCE_MAX].rsplit(" ", 1)[0] + "…"
            c = {**c, "verbatim": text, "in_article_source": ""}
        out.append(c)
    return out


def _writer_vars(ctx: StepContext) -> tuple[dict, list[str]]:
    inp, inp_art = job_input(ctx)
    plan = latest(ctx, "GapIgPlan")
    facts = latest(ctx, "FactInventory")
    evidence = latest(ctx, "AuthorEvidence")
    km = latest(ctx, "KeywordMap").payload
    vp = latest(ctx, "VoiceProfile", required=False)
    voice = {k: vp.payload[k] for k in ("dimensions", "positive_exemplars")} if vp else {}
    kws = [{"keyword": r["display"], "role": r["role"], "section": r["section"]}
           for r in km["rows"] if r["role"] in ("primary", "secondary")]
    variables = {
        "VOICE_PROFILE_POSITIVE_JSON": to_json(voice), "OUTLINE_JSON": to_json(plan.payload["outline"]),
        "APPROVED_FACTS_JSON": to_json(_facts_for_writer(facts.payload["claims"], ctx.config["thresholds"].get("writer_source_facts", "sentence"))),
        "AUTHOR_EVIDENCE_JSON": to_json(evidence.payload), "KEYWORD_PRIMARY_SECONDARY_JSON": to_json(kws),
        "BLACKLIST_HIGH_LIST": to_json(ctx.config["blacklist"]["high"]),
        "SECTION_COUNT": sum(1 for s in plan.payload["outline"] if s["level"] == 2),
        # nhánh A: không bao giờ ngắn hơn bài gốc
        "TARGET_WORDS": max(inp.target_words, round(branch_a.words(inp.article_md or "") * 1.1)),
    }
    return variables, [inp_art.sha256, plan.sha256, facts.sha256, evidence.sha256, sha(vp)]


def _check_draft(md: str, outline: list[dict]) -> list[str]:
    markers = set(SECTION_MARKER.findall(md))
    want = [s["section_id"] for s in outline if s["level"] == 2]
    return [f"thiếu marker <!-- {sid} -->" for sid in want if sid not in markers]


def _write_branch_a(ctx: StepContext, variables: dict, hashes: list[str], outline: list[dict], ig: list[dict],
                    orig: list[dict]) -> tuple[dict, object]:
    """Nhánh A: viết từng H2 từ đoạn gốc tương ứng; code kiểm tra dài ≥ đoạn gốc và độ trùng, sai thì viết lại section đó."""
    from vnseo.uniqueness import containment
    th = ctx.config["thresholds"]
    cmax = th.get("rework_containment_max", 0.25)
    tries = ctx.config["gates"]["revise_max"].get("rework", 2)
    ig_by_id = {i["ig_id"]: i for i in ig}
    strip_h = lambda t: re.sub(r"^#{1,6}\s+.*$|<!--.*?-->", "", t, flags=re.M)  # noqa: E731
    draft = {"body_markdown": "", "keyword_placement": [], "can_nguoi": [], "change_log": []}
    run_id, written, issues = None, [], []
    all_facts = json.loads(variables["APPROVED_FACTS_JSON"])
    for idx, sec in enumerate(outline):
        if sec["level"] != 2:
            continue
        children = []
        for s in outline[idx + 1:]:
            if s["level"] == 2:
                break
            children.append(s)
        original = branch_a.section_original(outline, idx, orig)
        ow = branch_a.words(strip_h(original))
        igs = [ig_by_id[i] for s in (sec, *children) for i in s["ig_ids"] if i in ig_by_id]
        target = max(round(ow * 1.1), 150 if not original else 0, 200 if igs and not original else 0)
        # Chỉ đưa fact của section này: fact outline gán + fact có câu gốc nằm trong đoạn gốc (cả bảng fact cho mọi
        # section là ~13k token/lượt × 12 section)
        sec_ids = {f for x in (sec, *children) for f in x["fact_ids"]}
        orig_n = re.sub(r"\s+", " ", original).lower()
        sec_facts = [f for f in all_facts if f["claim_id"] in sec_ids
                     or (f.get("origin") == "bai_goc" and re.sub(r"\s+", " ", f.get("verbatim", "")).lower()[:60] in orig_n)]
        feedback = feedback_for(ctx, "write")
        for attempt in range(tries + 1):
            out = _run(ctx, "P6-A", {**variables, "APPROVED_FACTS_JSON": to_json(sec_facts),
                                     "SECTION_JSON": to_json([sec, *children]), "IG_JSON": to_json(igs),
                                     "ORIGINAL_SECTION_MD": original or "(section mới, không có bản gốc)",
                                     "ORIGINAL_WORDS": ow, "SECTION_TARGET_WORDS": target,
                                     "PREVIOUS_SECTIONS_SUMMARY": to_json(written), "SECTION_ID": sec["section_id"],
                                     "HEADING": sec["heading"]}, hashes + [sec["section_id"], str(attempt)], feedback=feedback)
            body = branch_a.dedupe_headings(out.parsed["body_markdown"])
            problems = []
            nw = branch_a.words(strip_h(body))
            if original and nw < ow:
                problems.append(f"Section ngắn hơn bản gốc ({nw} < {ow} chữ): viết đủ mọi ý của đoạn gốc, giải thích rõ hơn.")
            if original and (r := containment(strip_h(body), strip_h(original), th.get("shingle_n", 5))).ratio > cmax:
                problems.append(f"Trùng câu chữ với bản gốc {r.ratio:.0%} (tối đa {cmax:.0%}); viết lại bằng câu chữ mới các đoạn: "
                                + " | ".join(p[:100] for p in r.top_passages[:6]))
            ctx.heartbeat()
            if not problems:
                break
            feedback = "Bản viết trước của section này chưa đạt:\n- " + "\n- ".join(problems)
        else:
            issues.append(f"{sec['section_id']}: " + "; ".join(problems))
        part = {**out.parsed, "body_markdown": body}
        draft["body_markdown"] += body.strip() + "\n\n"
        for k in ("keyword_placement", "can_nguoi", "change_log"):
            draft[k] += part[k]
        written.append({"section_id": sec["section_id"], "heading": sec["heading"],
                        "cau_mo_dau": next((ln for ln in body.split("\n") if ln.strip() and not ln.startswith(("#", "<!--"))), "")[:200],
                        "so_lieu_da_dung": sorted({h.raw for h in scan_numbers(body)})})
        run_id = out.run_id
    if issues:
        add_warning(ctx, "write", "branch_a_check", "hết lượt viết lại, còn lỗi: " + " | ".join(issues))
    return draft, run_id


def write(ctx: StepContext) -> StepResult:
    job = job_row(ctx)
    variables, hashes = _writer_vars(ctx)
    plan = latest(ctx, "GapIgPlan").payload
    outline = plan["outline"]
    inp, _ = job_input(ctx)
    orig = branch_a.original_sections(inp.article_md) if job["branch"] == "A" else []
    if orig:
        draft, run_id = _write_branch_a(ctx, variables, hashes, outline, plan.get("ig", []), orig)
    elif job["mode"] != "heavy":
        out = _run(ctx, "P6-S", variables, hashes, feedback=feedback_for(ctx, "write"))
        draft, run_id = out.parsed, out.run_id
    else:
        draft = {"body_markdown": "", "keyword_placement": [], "can_nguoi": [], "change_log": []}
        run_id, written = None, []
        for idx, sec in enumerate(outline):
            if sec["level"] != 2:
                continue
            children = []
            for s in outline[idx + 1:]:  # các H3 đứng ngay sau H2 này
                if s["level"] == 2:
                    break
                children.append(s)
            out = _run(ctx, "P6-H", {**variables, "SECTION_JSON": to_json([sec, *children]),
                                     "PREVIOUS_SECTIONS_SUMMARY": to_json(written), "SECTION_ID": sec["section_id"],
                                     "HEADING": sec["heading"]}, hashes + [sec["section_id"]],
                       feedback=feedback_for(ctx, "write"))
            part = out.parsed
            draft["body_markdown"] += part["body_markdown"].strip() + "\n\n"
            for k in ("keyword_placement", "can_nguoi", "change_log"):
                draft[k] += part[k]
            body = part["body_markdown"]
            written.append({"section_id": sec["section_id"], "heading": sec["heading"],
                            "cau_mo_dau": next((ln for ln in body.split("\n") if ln.strip() and not ln.startswith(("#", "<!--"))), "")[:200],
                            # section sau không trình bày lại các số liệu này (P6-H)
                            "so_lieu_da_dung": sorted({h.raw for h in scan_numbers(body)})})
            run_id = out.run_id
            ctx.heartbeat()
    if problems := _check_draft(draft["body_markdown"], outline):
        raise StepFailed("bài viết sai cấu trúc: " + "; ".join(problems))
    save(ctx, "ArticleDraft", draft, run_id=run_id)
    return StepResult(O.DONE)


# ---------- B7: meta + sửa hẹp ----------

def meta_edit(ctx: StepContext) -> StepResult:
    inp, _ = job_input(ctx)
    draft_art = latest(ctx, "ArticleDraft")
    md = draft_art.payload["body_markdown"]
    km = latest(ctx, "KeywordMap").payload
    th = ctx.config["thresholds"]
    kws = [{"keyword": r["display"], "role": r["role"]} for r in km["rows"] if r["role"] in ("primary", "secondary")]
    pages = [p.model_dump() for p in inp.internal_pages]

    meta = _run(ctx, "P7a", {"ARTICLE_MD": md, "KEYWORD_PRIMARY_SECONDARY_JSON": to_json(kws),
                             "INTERNAL_PAGES_JSON": to_json(pages), "AUTHOR_JSON": to_json(inp.author.model_dump() if inp.author else None),
                             "TITLE_MIN": th["title_chars"][0], "TITLE_MAX": th["title_chars"][1],
                             "META_MIN": th["meta_chars"][0], "META_MAX": th["meta_chars"][1]},
                [draft_art.sha256], ctx={"internal_urls": [p["url"] for p in pages]})
    save(ctx, "Meta", meta.parsed, run_id=meta.run_id)
    ctx.heartbeat()

    hits = [h.__dict__ for h in blacklist_scan(md, ctx.config["blacklist"])]
    out = _run(ctx, "P7b", {"ARTICLE_MD": md, "BLACKLIST_HITS_JSON": to_json(hits),
                            "MAX_EDIT_RATIO_PCT": int(th["max_edit_ratio"] * 100)},
               [draft_art.sha256], ctx={"source_text": md})
    edits = [Edit(**e) for e in out.parsed["edits"]]
    res = apply_edits(md, edits, max_ratio=th["max_edit_ratio"])
    save(ctx, "Edits:P7b", {"edits": out.parsed["edits"], "applied": [e.check_id for e in res.applied],
                            "rejected": [[e.check_id, why] for e, why in res.rejected], "ratio": res.ratio,
                            "batch_rejected": res.batch_rejected, "diff": res.diff}, run_id=out.run_id)
    if res.applied and not res.batch_rejected:
        save(ctx, "ArticleDraft", {**draft_art.payload, "body_markdown": res.md,
                                   "change_log": draft_art.payload["change_log"] + [f"P7b: {len(res.applied)} sửa hẹp"]})
    # P7b chỉ sửa câu chữ: lô vượt tỷ lệ bị bỏ, bài giữ nguyên và đi tiếp (ghi lại để QA_GATE xem)
    return StepResult(O.DONE)


def title_gate(ctx: StepContext) -> StepResult:
    """Chỉ luật cứng của code, không gọi AI (§2)."""
    inp, _ = job_input(ctx)
    km = latest(ctx, "KeywordMap").payload
    primary = next(r["display"] for r in km["rows"] if r["role"] == "primary")
    rule = rules.title_rules(latest(ctx, "Meta").payload, primary, inp.existing_titles, ctx.config["thresholds"])
    save(ctx, "TitleChoice", {"passed": rule.passed, "failures": rule.failures, **rule.data})
    d = GateDecision("TITLE", "code", "auto", "approve" if rule.passed else "escalate",
                     reason="; ".join(rule.failures) or None, rule_failures=rule.failures or None)
    if rule.passed:
        return StepResult(O.APPROVE, decisions=[d])
    return StepResult(O.ESCALATE, "; ".join(rule.failures), decisions=[d])


# ---------- B8: uniqueness ----------

def uniqueness(ctx: StepContext) -> StepResult:
    job = job_row(ctx)
    inp, _ = job_input(ctx)
    md = latest(ctx, "ArticleDraft").payload["body_markdown"]
    n = ctx.config["thresholds"]["shingle_n"]
    if job["branch"] == "B":
        rows = ctx.conn.execute("SELECT source_sha256, shingle_hashes FROM source_fingerprints WHERE job_id = %s",
                                (ctx.job_id,)).fetchall()
        if not rows and _is_new_article(ctx):
            save(ctx, "OverlapReport", {"branch": "B", "ratio": 0.0, "note": "bài mới, không có nguồn để so"})
            return StepResult(O.APPROVE)
        if not rows:
            raise StepFailed("không có fingerprint bài nguồn")
        reports = [containment_hashes(md, unpack_hashes(bytes(r["shingle_hashes"])), n) for r in rows]
        worst = max(reports, key=lambda r: r.ratio)
    else:
        worst = containment_hashes(md, shingle_hashes(inp.article_md or "", n), n)  # so với bản cũ: chỉ để tham khảo
    save(ctx, "OverlapReport", {"branch": job["branch"], **worst.__dict__})
    limit = ctx.config["thresholds"]["branch_b_containment_max"]
    if job["branch"] == "B" and worst.ratio >= limit:
        return StepResult(O.FAIL, f"containment {worst.ratio:.3f} ≥ {limit} so với bài nguồn")
    return StepResult(O.APPROVE)
