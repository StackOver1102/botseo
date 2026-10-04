"""Step handler mốc M1: QA_FANOUT → GSC_28D (B9–B12).

Khác spec, để chạy liền mạch (config `gates`):
- RUBRIC: sửa mọi mục NO (trừ mức thấp) 1 vòng, kể cả khi rubric đạt; hết vòng mà vẫn trượt thì
  `rubric_fail_after_fix: proceed` → ghi Warnings và đi tiếp (trừ khi tiêu chí accuracy = 0: dừng).
- FACT_SIGNOFF: claim không xác minh được hoặc mâu thuẫn → `fact_unverified: remove` xoá câu chứa claim thay vì chờ người.
  Không gọi AI gate ở chốt này: luật cứng của code (URL sống + chứa quote) đã quyết định đủ.
- ASSEMBLE: change log do code dựng (không gọi P11) để bớt một lượt model web.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
from datetime import timedelta
from urllib.parse import urlsplit

from gates import rules
from knowledge.repository import commit_usage
from vnseo import jsonld
from vnseo.edits import GLOBAL, Edit, apply_edits, changed_sentence_ratio
from vnseo.facts import OVERRIDE_KINDS, approved_value_keys, claim_value_keys, match_numbers, scan_numbers
from vnseo.purge import purge_unverified
from vnseo.rubric import CodeChecks, JobInfo, Thresholds, aggregate
from vnseo.style import blacklist_scan, sentence_stats
from vnseo.vn_text.markdown import SECTION_MARKER, SectionIndex
from vnseo.vn_text.normalize import strip_diacritics
from vnseo.vn_text.sentences import sentence_texts

from .. import autosource
from ..executor import StepContext, StepResult
from ..urlcheck import relevant_excerpt
from ..llm.base import AwaitingExternal
from ..states import Outcome as O
from ..states import State
from .common import (
    add_warning, datamark, datamark_char, job_input, job_row, latest, run_gate, save, save_feedback, source_text, to_json,
)
from .m0 import _approved, _facts_for_writer, _run

PASSES = ["P8a", "P8b", "P8c", "P8d"]
_LOCATION = re.compile(r"^S\d{2,}$")
CHECK_TYPES = {"so_lieu", "ten_rieng", "ngay", "gia", "luat", "link", "trich_dan"}  # trải nghiệm/khuyến nghị không kiểm trên web
LITE_CHECK_TYPES = {"so_lieu", "gia", "luat"}


# ---------- tiện ích ----------

def _article(ctx: StepContext) -> tuple[str, object]:
    art = latest(ctx, "ArticleDraft")
    return art.payload["body_markdown"], art


def _save_article(ctx: StepContext, md: str, note: str) -> None:
    art = latest(ctx, "ArticleDraft")
    save(ctx, "ArticleDraft", {**art.payload, "body_markdown": md, "change_log": art.payload["change_log"] + [note]})


def _number_report(ctx: StepContext, md: str, claims: list[dict]):
    km = latest(ctx, "KeywordMap").payload
    plan = latest(ctx, "GapIgPlan").payload
    literals = [r["display"] for r in km["rows"]] + [s["heading"] for s in plan["outline"]]
    title = (latest(ctx, "TitleChoice", required=False) or type("x", (), {"payload": {}})).payload.get("title", "")
    years = {dt.date.today().year} | {int(y) for y in re.findall(r"\b(?:19|20)\d{2}\b", title)}
    hits = scan_numbers(md, ctx.config["thresholds"]["number_ignore_patterns"], literals, years)
    return match_numbers(hits, claims)


def _jsonld_data(ctx: StepContext, meta: dict, title: str | None) -> dict:
    """Dữ liệu JSON-LD: tác giả lấy từ JobInput (tên đã chọn khi tạo job), không theo chữ model P7a chép lại."""
    data = {**meta["jsonld_data"], "headline": title}
    author = job_input(ctx)[0].author
    if author:
        data.update(author_name=author.name, author_url=author.url, author_type=author.type)
    return data


def _passes_for(ctx: StepContext) -> list[str]:
    job = job_row(ctx)
    skip = set(ctx.config["modes"][job["mode"]].get("steps_skip", []))
    vp = latest(ctx, "VoiceProfile", required=False)
    return [p for p in PASSES if p not in skip and not (p == "P8b" and not vp)]


def _expected_ids(pass_id: str, ctx: StepContext) -> list[str]:
    if pass_id == "P8a":
        return [f"P8a.{i}" for i in range(1, 5)]
    if pass_id == "P8b":
        vp = latest(ctx, "VoiceProfile").payload
        return [f"P8b.{c['check_id']}" for c in vp["checklist_qa"]] + ["P8b.B1", "P8b.B2"]
    if pass_id == "P8c":
        return [f"P8c.{i}" for i in range(1, 6)]
    return [f"P8d.{i}" for i in range(1, 4)] + (["P8d.4"] if job_row(ctx)["ymyl"] else [])


def _run_pass(ctx: StepContext, pass_id: str, md: str) -> dict:
    job = job_row(ctx)
    th = ctx.config["thresholds"]
    if pass_id == "P8a":
        plan = latest(ctx, "GapIgPlan").payload
        overlap = latest(ctx, "OverlapReport").payload
        variables = {"ARTICLE_MD": md, "GAP_IG_JSON": to_json(plan),
                     "OVERLAP_REPORT_JSON": to_json({k: overlap[k] for k in ("branch", "ratio") if k in overlap})}
    elif pass_id == "P8b":
        vp = latest(ctx, "VoiceProfile").payload
        checklist = [{"check_id": f"P8b.{c['check_id']}", "question": c["question"]} for c in vp["checklist_qa"]]
        hits = [h.__dict__ for h in blacklist_scan(md, ctx.config["blacklist"])]
        variables = {"ARTICLE_MD": md, "VOICE_CHECKLIST_JSON": to_json(checklist),
                     "NEGATIVE_EXEMPLARS": to_json(vp["negative_exemplars_qa_only"]),
                     "BLACKLIST_HITS_JSON": to_json(hits),
                     "SENTENCE_STATS_JSON": to_json(sentence_stats(md, th["sentence_max_syllables"]).__dict__)}
    elif pass_id == "P8c":
        km = latest(ctx, "KeywordMap").payload
        variables = {"ARTICLE_MD": md, "KEYWORD_MAP_JSON": to_json(km),
                     "PAA_JSON": to_json(latest(ctx, "SerpSnapshot").payload["paa"])}
    else:
        claims = latest(ctx, "FactInventory").payload["claims"]
        variables = {"ARTICLE_MD": md, "APPROVED_FACTS_JSON": to_json(_facts_for_writer(claims, ctx.config["thresholds"].get("writer_source_facts", "sentence"))),
                     "AUTHOR_EVIDENCE_JSON": to_json(latest(ctx, "AuthorEvidence").payload),
                     "NUMBER_MATCH_REPORT_JSON": to_json(_number_report(ctx, md, claims).to_json()),
                     "YMYL": "có" if job["ymyl"] else "không"}
    writer = ctx.config["models"]["writer"]["provider"]
    # Chia giám khảo cho 2 hãng để extension chạy song song (cả hai đều khác họ với writer)
    model_key = "judge_alt" if pass_id in ctx.config["models"].get("judge_alt", {}).get("passes", []) else "judge"
    out = _run(ctx, pass_id, variables, [hashlib.sha256(md.encode()).hexdigest()], avoid_families={writer},
               model_key=model_key,
               # evidence có thể trích từ bài hoặc từ dữ liệu đưa kèm (thống kê câu, bảng fact, kết quả quét)
               ctx={"source_text": "\n".join(str(v) for v in variables.values()),
                    "expected_check_ids": _expected_ids(pass_id, ctx),
                    "soft_evidence": ctx.config["gates"].get("qa_soft_evidence", True)})
    save(ctx, f"Verdict:{pass_id}", out.parsed, run_id=out.run_id)
    ctx.heartbeat()
    return out.parsed


def _verdicts(ctx: StepContext) -> dict[str, dict]:
    return {p: latest(ctx, f"Verdict:{p}").payload for p in _passes_for(ctx)}


def _code_checks(ctx: StepContext, md: str) -> tuple[CodeChecks, dict]:
    th = ctx.config["thresholds"]
    claims = latest(ctx, "FactInventory").payload["claims"]
    numbers = _number_report(ctx, md, claims)
    high = [h for h in blacklist_scan(md, ctx.config["blacklist"]) if h.level == "high"]
    overlap = latest(ctx, "OverlapReport").payload
    title_ok = bool(latest(ctx, "TitleChoice").payload.get("passed"))
    meta = latest(ctx, "Meta").payload
    ld_errors = jsonld.validate(jsonld.build(_jsonld_data(ctx, meta, latest(ctx, "TitleChoice").payload.get("title"))))
    stats = sentence_stats(md, th["sentence_max_syllables"])
    checks = CodeChecks(number_override_fail=numbers.override_fail, blacklist_high_hits=len(high),
                        containment=overlap.get("ratio", 0.0), title_meta_ok=title_ok, jsonld_ok=not ld_errors,
                        long_sentence_share=stats.long_share)
    detail = {"unmatched_numbers": [h.raw for h in numbers.unmatched if h.kind in ("PCT", "CUR", "DATE")],
              "blacklist_high": sorted({h.text for h in high}), "jsonld_errors": ld_errors}
    return checks, detail


# ---------- B9: QA ----------

def qa_fanout(ctx: StepContext) -> StepResult:
    md, art = _article(ctx)
    # Mốc đo tỷ lệ sửa ở QA_GATE = bài vào QA lần này. Luôn ghi (trùng nội dung thì save giữ bản cũ): bài viết lại
    # sau quality rewind phải có mốc mới, không thì QA_GATE so với bài cũ và báo sửa 100%.
    save(ctx, "QABaseline", {"body_markdown": md, "article_version": art.version})
    _run_passes(ctx, _passes_for(ctx), md)
    return StepResult(O.DONE)


def _run_passes(ctx: StepContext, passes: list[str], md: str) -> None:
    """Tạo task cho mọi pass trước rồi mới chờ, để các pass chạy song song trên extension.
    Lần chạy lại sau khi task xong: pass đã có kết quả lấy từ cache của runner."""
    waiting = None
    for p in passes:
        try:
            _run_pass(ctx, p, md)
        except AwaitingExternal as e:
            waiting = waiting or e
    if waiting:
        raise waiting


def _location(loc: str) -> str:
    loc = (loc or "").strip()
    return loc if _LOCATION.match(loc) else GLOBAL


def rubric(ctx: StepContext) -> StepResult:
    md, _ = _article(ctx)
    job = job_row(ctx)
    th = ctx.config["thresholds"]
    verdicts = _verdicts(ctx)
    checks, detail = _code_checks(ctx, md)
    result = aggregate(verdicts, checks, JobInfo(job["branch"], job["mode"], job["ymyl"]),
                       Thresholds(th["rubric_pass_ratio"], th["branch_b_containment_max"], th["long_sentence_share_max"]))
    save(ctx, "RubricResult", {**result.to_json(), "code_detail": detail})

    # Mục cần sửa: NO của judge (bỏ mức thấp) + override của code
    failed = {it["check_id"]: {**it, "location": _location(it["location"])}
              for v in verdicts.values() for it in v["items"] if it["verdict"] == "NO" and it["severity"] != "thap"}
    if checks.number_override_fail and "P8d" in verdicts:
        failed["P8d.1"] = {"check_id": "P8d.1", "evidence": ", ".join(detail["unmatched_numbers"]), "verdict": "NO",
                           "severity": "chan", "location": GLOBAL,
                           "fix_hint": f"số không có trong fact đã duyệt: {detail['unmatched_numbers']} — bỏ hoặc thay bằng giá trị trong fact"}
    if checks.blacklist_high_hits and "P8b" in verdicts:
        failed["P8b.B1"] = {"check_id": "P8b.B1", "evidence": ", ".join(detail["blacklist_high"]), "verdict": "NO",
                            "severity": "cao", "location": GLOBAL,
                            "fix_hint": f"viết lại các cụm sáo rỗng: {detail['blacklist_high']}"}

    log = (latest(ctx, "FixLog", required=False) or type("x", (), {"payload": {"rounds": {}}})).payload["rounds"]
    max_rounds = th["fix_rounds_per_check"]
    fixable = [it for cid, it in failed.items() if log.get(cid, 0) < max_rounds]
    if fixable:
        save(ctx, "FixQueue", {"items": fixable})
        return StepResult(O.REVISE, f"sửa {len(fixable)} mục QA: {[i['check_id'] for i in fixable]}")
    if result.pass_:
        return StepResult(O.APPROVE)
    reason = f"rubric trượt sau khi sửa ({result.total}/{result.max_total}, blocking=0: {result.blocking_zero})"
    if rewind := _quality_rewind(ctx, result, verdicts, reason):
        return rewind
    # Chỉ dừng khi code thấy số liệu sai (không khớp fact đã duyệt). Accuracy = 0 do judge (khẳng định chưa có claim)
    # để FACTCHECK phía sau kiểm chứng và xoá câu không xác minh được; thiếu IG chỉ là thiếu nội dung, không phải sai.
    if checks.number_override_fail or ctx.config["gates"].get("rubric_fail_after_fix", "proceed") != "proceed":
        return StepResult(O.ESCALATE, reason)
    add_warning(ctx, "qa", "rubric_fail", reason + f"; còn lỗi: {sorted(failed)}")
    return StepResult(O.APPROVE, "đi tiếp dù rubric trượt (xem Warnings)")


# Tiêu chí blocking = 0 mà sửa hẹp (P9) không cứu được → lỗi nằm ở bước trước, quay về đó (automation.quality_rewinds_max)
_QUALITY_PASSES = {"accuracy": ("P8d",), "information_gain": ("P8a",)}


def _quality_rewind(ctx: StepContext, result, verdicts: dict, reason: str) -> StepResult | None:
    auto = ctx.config.get("automation", {})
    zero = [c for c in result.blocking_zero if c in _QUALITY_PASSES]
    if not auto.get("enabled") or not zero:
        return None
    # Nhánh A (làm mới bài có sẵn): thông tin mới bị giới hạn bởi dữ liệu của chính bài gốc — viết lại cả bài không tạo
    # thêm dữ liệu (bài kqbd bị viết 3 lần vì vậy). Chỉ quay lại khi sai độ chính xác.
    if job_row(ctx)["branch"] == "A" and zero == ["information_gain"]:
        return None
    used = ctx.conn.execute("SELECT count(*) AS n FROM job_transitions WHERE job_id = %s AND from_state = 'RUBRIC' "
                            "AND outcome = 'AUTO_REWIND'", (ctx.job_id,)).fetchone()["n"]
    if used >= auto.get("quality_rewinds_max", 2):
        return None
    problems = [f"- {it['check_id']}: {it.get('fix_hint') or it['evidence']}"[:300]
                for c in zero for p in _QUALITY_PASSES[c] for it in verdicts.get(p, {}).get("items", [])
                if it["verdict"] == "NO"]
    n_facts = len(_approved(latest(ctx, "FactInventory").payload["claims"]))
    inp, _ = job_input(ctx)
    has_source = bool(inp.article_md or inp.source_urls or source_text(ctx))
    # Đủ fact thì lỗi nằm ở khâu viết (bịa trải nghiệm, không dựng IG từ fact có sẵn) — quay về WRITE là rẻ và đúng chỗ;
    # chỉ quay về khâu lấy dữ kiện khi thật sự thiếu fact.
    few = n_facts < auto.get("min_facts_for_write", 6)
    if few and has_source:
        target, key = State.FACT_VOICE_FANOUT, "fact"
        text = (f"Bài viết thiếu dữ kiện (chỉ {n_facts} fact đã duyệt) nên QA chấm {', '.join(zero)} = 0. Trích ĐẦY ĐỦ mọi "
                "số liệu, mốc thời gian, quy định, định nghĩa cơ bản trong văn bản — kể cả dữ kiện phổ thông mà bài nào cũng có.")
    elif few:
        target, key = State.GAP_IG_AI, "outline"
        text = (f"QA chấm {', '.join(zero)} = 0 (chỉ {n_facts} fact đã duyệt). Đề xuất thêm new_claims kèm source_url "
                "cho các dữ kiện cốt lõi của chủ đề (con số, quy định chính thức) và IG có dữ liệu kiểm chứng được.")
    else:
        target, key = State.WRITE, "write"
        text = (f"QA chấm {', '.join(zero)} = 0 dù có {n_facts} fact đã duyệt. Viết lại: chỉ khẳng định điều có trong fact, "
                "không kể trải nghiệm cá nhân không có trong author_evidence, và dựng mỗi IG thành nội dung cụ thể từ fact.")
    save_feedback(ctx, key, text + ("\nLỗi QA:\n" + "\n".join(problems) if problems else ""))
    save(ctx, "FixLog", {"rounds": {}})  # bài viết lại được sửa lại từ đầu
    add_warning(ctx, "qa", "quality_rewind", f"{reason} → quay về {target.value}")
    return StepResult(O.REVISE, f"{reason} → quay về {target.value}", rewind_to=target, bump=(key,))


def _apply_p9(ctx: StepContext, items: list[dict], note: str, kind: str) -> tuple[str, dict]:
    """Gọi P9 sửa các lỗi, áp edit trong đúng section; trả (bài mới, báo cáo)."""
    md, art = _article(ctx)
    claims = latest(ctx, "FactInventory").payload["claims"]
    out = _run(ctx, "P9", {"ARTICLE_MD": md, "FAILED_ITEMS_JSON": to_json(items),
                           "APPROVED_FACTS_JSON": to_json(_facts_for_writer(claims, ctx.config["thresholds"].get("writer_source_facts", "sentence")))},
               [art.sha256, hashlib.sha256(to_json(items).encode()).hexdigest()],
               ctx={"source_text": md, "allowed_check_ids": [i["check_id"] for i in items]})
    edits = [Edit(**{**e, "error_type": 0}) for e in out.parsed["edits"]]
    locations = {i["check_id"]: _location(i["location"]) for i in items}
    limit = ctx.config["thresholds"].get("fix_edit_ratio_max", 0.15)
    res = apply_edits(md, edits, locations=locations, max_ratio=limit)
    over_limit = None
    if res.batch_rejected:
        # Cả lô vượt ngưỡng: không bỏ hết (bỏ hết thì lỗi chặn cũng không được sửa) mà áp phần quan trọng nhất còn
        # nằm trong ngưỡng — lỗi nặng trước (chan → cao → trung_binh → thap), edit nào làm vượt ngưỡng thì bỏ qua.
        over_limit = res.ratio
        rank = {"chan": 0, "cao": 1, "trung_binh": 2, "thap": 3}
        severity = {i["check_id"]: rank.get(i.get("severity"), 4) for i in items}
        chosen: list[Edit] = []
        for e in sorted(edits, key=lambda e: severity.get(e.check_id, 4)):
            trial = apply_edits(md, chosen + [e], locations=locations, max_ratio=limit)
            if not trial.batch_rejected and e in trial.applied:
                chosen.append(e)
        res = apply_edits(md, chosen, locations=locations, max_ratio=limit)
        skipped = [e for e in edits if e not in chosen]
        res.rejected += [(e, "over_edit_ratio") for e in skipped if all(e is not r for r, _ in res.rejected)]
        add_warning(ctx, "qa", "fix_partial",
                    f"{kind}: cả lô sửa {over_limit:.0%} số câu (ngưỡng {limit:.0%}) — áp {len(res.applied)}/{len(edits)} edit "
                    f"ưu tiên lỗi nặng, bỏ {len(skipped)} edit")
    report = {"items": [i["check_id"] for i in items], "applied": [e.check_id for e in res.applied],
              "rejected": [[e.check_id, why] for e, why in res.rejected], "ratio": res.ratio,
              "batch_rejected": res.batch_rejected, "over_limit_ratio": over_limit, "diff": res.diff}
    save(ctx, kind, report, run_id=out.run_id)
    if res.batch_rejected:  # không edit nào áp được trong ngưỡng
        return md, report
    if res.applied:
        _save_article(ctx, res.md, f"{note}: {len(res.applied)} edit")
    return res.md, report


def fix(ctx: StepContext) -> StepResult:
    items = latest(ctx, "FixQueue").payload["items"]
    _apply_p9(ctx, items, "P9 sửa QA", "Edits:P9")
    log_art = latest(ctx, "FixLog", required=False)
    rounds = dict(log_art.payload["rounds"]) if log_art else {}
    for it in items:
        rounds[it["check_id"]] = rounds.get(it["check_id"], 0) + 1
    save(ctx, "FixLog", {"rounds": rounds})
    return StepResult(O.DONE)


def qa_rerun_failed(ctx: StepContext) -> StepResult:
    """Chấm lại các pass có mục vừa sửa (pass khác giữ verdict cũ)."""
    md, _ = _article(ctx)
    items = latest(ctx, "FixQueue").payload["items"]
    rerun = sorted({i["check_id"].split(".")[0] for i in items} & set(_passes_for(ctx)))
    _run_passes(ctx, rerun, md)
    return StepResult(O.DONE)


def qa_gate(ctx: StepContext) -> StepResult:
    md, _ = _article(ctx)
    base = latest(ctx, "QABaseline").payload["body_markdown"]
    ratio = changed_sentence_ratio(sentence_texts(base), sentence_texts(md))
    limit = ctx.config["thresholds"].get("fix_edit_ratio_max", 0.15)
    failures = [f"tỷ lệ câu bị sửa trong QA {ratio:.3f} > {limit}"] if ratio > limit else []
    rule = rules.RuleResult.of(failures, data={"edit_ratio": round(ratio, 3)})
    rub = latest(ctx, "RubricResult").payload
    p9 = latest(ctx, "Edits:P9", required=False)
    context = {"rubric": {k: rub[k] for k in ("scores", "total", "max_total", "pass", "blocking_zero")},
               "p9_diff": (p9.payload["diff"][:6000] if p9 else ""), "code_detail": rub.get("code_detail")}
    models = ctx.config["models"]
    families = {models["judge"]["provider"], models["writer"]["provider"]}
    return run_gate(ctx, "qa", "RubricResult", rub["report_md"], context, rule, families, model_key="gate_heavy")


# ---------- B10: fact-check ----------

def _in_article(claim: dict, md: str, article_keys: set[str]) -> bool:
    text = claim.get("new_value") or claim["verbatim"]
    keys = claim_value_keys(text)
    if keys:
        return bool(keys & article_keys)
    return strip_diacritics(text)[:60] in strip_diacritics(md)


def _claims_to_check(ctx: StepContext, md: str) -> list[dict]:
    job = job_row(ctx)
    types = LITE_CHECK_TYPES if job["mode"] == "lite" and not job["ymyl"] else CHECK_TYPES
    article_keys = {h.key for h in scan_numbers(md)}
    return [c for c in _approved(latest(ctx, "FactInventory").payload["claims"])
            if c["origin"] in ("bai_goc", "bai_nguon") and c["type"] in types and _in_article(c, md, article_keys)]


def _host(url: str) -> str:
    try:
        return urlsplit(url).netloc.lower().removeprefix("www.")
    except ValueError:
        return ""


def _fact_query(claim: dict) -> str:
    """Truy vấn tìm nguồn cho một claim: chính câu claim, bỏ ký tự bảng/Markdown, cắt ở ranh giới từ."""
    text = re.sub(r"[|*_`#>\[\]]+", " ", claim.get("new_value") or claim["verbatim"])
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= 180 else text[:180].rsplit(" ", 1)[0]


def _search_sources(ctx: StepContext, claims: list[dict], cfg: dict) -> dict:
    """Tìm nguồn độc lập cho từng claim bằng SerpProvider. Kết quả lưu ở artifact FactSearch: bước chạy lại (retry,
    autopilot) dùng lại, không tốn thêm lượt tìm. Bỏ trang bị chặn (nhà cái, MXH…) và chính các trang job đã lấy
    làm tư liệu — claim trích từ trang X mà lại "xác minh" bằng trang X là vòng tròn."""
    key = hashlib.sha256(to_json([(c["claim_id"], _fact_query(c)) for c in claims]).encode()).hexdigest()
    cached = latest(ctx, "FactSearch", required=False)
    if cached and cached.payload.get("key") == key:
        return cached.payload
    inp, _ = job_input(ctx)
    own = {_host(u) for u in inp.source_urls} - {""}
    block_cfg = ctx.config.get("adapters", {}).get("auto_source", {})
    blocking = set(ctx.config["thresholds"].get("blocking_claim_types", []))
    # Hết hạn mức tìm thì claim loại chặn (số liệu, giá, luật, ngày) được tìm trước
    ordered = sorted(claims, key=lambda c: c["type"] not in blocking)
    budget, per_claim = cfg.get("max_searches", 40), cfg.get("results_per_claim", 2)
    urls: dict[str, list[str]] = {}
    queries: dict[str, str] = {}
    by_query: dict[str, list[str]] = {}
    for c in ordered:
        q = _fact_query(c)
        queries[c["claim_id"]] = q
        if q not in by_query:
            if len(by_query) >= budget:
                urls[c["claim_id"]] = []
                continue
            organic = ctx.services.serp_provider.fetch(q).organic
            by_query[q] = [o.url for o in organic
                           if not autosource._blocked(o.url, block_cfg) and _host(o.url) not in own][:per_claim]
            ctx.heartbeat()
        urls[c["claim_id"]] = by_query[q]
    payload = {"key": key, "queries": queries, "urls": urls, "searches": len(by_query), "excluded_hosts": sorted(own)}
    save(ctx, "FactSearch", payload)
    return payload


def _factcheck_by_search(ctx: StepContext, claims: list[dict], cfg: dict) -> StepResult:
    """Fact-check không phụ thuộc model có tự tìm web hay không (qua proxy/extension, Gemini trả "không xác minh được"
    cho mọi claim mà không tra gì): code tìm nguồn (Serper), tải trang, cắt đoạn liên quan; model chỉ đối chiếu bằng
    P10-S, mỗi lượt vài claim để prompt và output đều ngắn."""
    found = _search_sources(ctx, claims, cfg)
    fetch, mark = ctx.services.url_fetcher, datamark_char(ctx)
    width = max(300, cfg.get("excerpt_chars", 2400) // 2)
    provided: dict[str, dict[str, str]] = {}
    blocks: dict[str, str] = {}
    for c in claims:
        cid, text = c["claim_id"], c.get("new_value") or c["verbatim"]
        sources = {}
        for url in found["urls"].get(cid, []):
            status, page = fetch(url)
            excerpt = relevant_excerpt(page, text, width=width, windows=1) if status == 200 and page else ""
            if excerpt:
                sources[url] = excerpt
        provided[cid] = sources
        parts = [f'<claim claim_id="{cid}" type="{c["type"]}">{text}</claim>']
        parts += [f'<nguon_ben_ngoai claim_id="{cid}" source_url="{u}">{datamark(t, mark)}</nguon_ben_ngoai>'
                  for u, t in sources.items()]
        blocks[cid] = "\n".join(parts)
        ctx.heartbeat()

    by_id: dict[str, dict] = {}
    run_id = None
    with_sources = [c["claim_id"] for c in claims if provided[c["claim_id"]]]
    size = max(1, cfg.get("claims_per_call", 6))
    for i in range(0, len(with_sources), size):
        batch = with_sources[i:i + size]
        text_block = "\n\n".join(blocks[cid] for cid in batch)
        out = _run(ctx, "P10-S", {"CLAIMS_WITH_SOURCES": text_block, "TODAY": dt.date.today().isoformat()},
                   [hashlib.sha256(text_block.encode()).hexdigest()],
                   ctx={"provided_sources": {cid: provided[cid] for cid in batch}, "datamark": mark})
        run_id = out.run_id
        for it in out.parsed["items"]:
            if it["claim_id"] in batch:  # model trả claim_id lạ thì bỏ, không để lọt sang FACT_APPLY
                by_id[it["claim_id"]] = it
        ctx.heartbeat()
    # Model bỏ sót claim trong lô (đo thật: 1/4 claim): hỏi lại riêng các claim đó một lượt, không để claim có nguồn
    # bị coi là không xác minh được chỉ vì model quên trả
    missed = [cid for cid in with_sources if cid not in by_id]
    if missed:
        text_block = "\n\n".join(blocks[cid] for cid in missed)
        out = _run(ctx, "P10-S", {"CLAIMS_WITH_SOURCES": text_block, "TODAY": dt.date.today().isoformat()},
                   [hashlib.sha256(text_block.encode()).hexdigest()],
                   ctx={"provided_sources": {cid: provided[cid] for cid in missed}, "datamark": mark})
        for it in out.parsed["items"]:
            if it["claim_id"] in missed:
                by_id[it["claim_id"]] = it
        ctx.heartbeat()

    items = []
    for c in claims:
        cid = c["claim_id"]
        reason = ("không tìm được nguồn độc lập" if not found["urls"].get(cid)
                  else "không tải được nguồn nào" if not provided[cid] else "model không trả kết quả cho claim này")
        items.append(by_id.get(cid) or {
            "claim_id": cid, "atomic_claim": c.get("new_value") or c["verbatim"], "verdict": "KHONG_XAC_MINH_DUOC",
            "current_value": "", "sources": [], "conflict_note": reason, "suggested_action": ""})
    save(ctx, "FactCheck", {"items": items}, run_id=run_id)
    verdicts = {v: sum(1 for it in items if it["verdict"] == v) for v in sorted({it["verdict"] for it in items})}
    return StepResult(O.DONE, f"{found['searches']} lượt tìm, {len(with_sources)}/{len(claims)} claim có nguồn: {verdicts}")


def factcheck(ctx: StepContext) -> StepResult:
    md, _ = _article(ctx)
    claims = _claims_to_check(ctx, md)
    if not claims:
        save(ctx, "FactCheck", {"items": []})
        return StepResult(O.DONE)
    cfg = ctx.config.get("factcheck") or {}
    if cfg.get("mode") == "search" and ctx.services.serp_provider is not None and ctx.services.url_fetcher is not None:
        return _factcheck_by_search(ctx, claims, cfg)
    payload = [{"claim_id": c["claim_id"], "atomic_claim": c.get("new_value") or c["verbatim"], "type": c["type"]}
               for c in claims]
    out = _run(ctx, "P10", {"CLAIMS_TO_CHECK_JSON": to_json(payload), "TODAY": dt.date.today().isoformat()},
               [hashlib.sha256(to_json(payload).encode()).hexdigest()])
    save(ctx, "FactCheck", out.parsed, run_id=out.run_id)
    return StepResult(O.DONE)


def fact_signoff(ctx: StepContext) -> StepResult:
    fc = latest(ctx, "FactCheck").payload
    verify = ctx.services.url_verifier
    min_tier = ctx.config["thresholds"].get("fact_apply_min_tier", 2)
    ok, apply, remove, human, checks = [], [], [], [], {}
    for it in fc["items"]:
        urls_ok = bool(it["sources"])
        for s in it["sources"]:
            r = verify(s["url"], s["quote"])
            checks[f"{it['claim_id']}|{s['url']}"] = {"ok": r.ok, "status": r.status, "score": round(r.fuzzy_score, 2)}
            urls_ok = urls_ok and r.ok
        good = urls_ok and any(s["tier"] <= min_tier for s in it["sources"])
        v = it["verdict"]
        if v == "DUNG" and urls_ok:
            ok.append(it["claim_id"])
        elif v in ("SAI", "CAN_CAP_NHAT") and good and it["current_value"]:
            apply.append(it["claim_id"])
        elif v == "MAU_THUAN":
            human.append(it["claim_id"])
        else:
            remove.append(it["claim_id"])  # không xác minh được, hoặc nguồn không chứa quote
    if human and ctx.config["gates"].get("fact_unverified", "remove") == "remove":
        remove += human
        add_warning(ctx, "fact_signoff", "remove", f"claim mâu thuẫn nguồn, xoá khỏi bài: {human}")
        human = []
    save(ctx, "FactSignoff", {"ok": ok, "apply": apply, "remove": remove, "human": human, "url_checks": checks})
    if human:
        return StepResult(O.ESCALATE, f"claim mâu thuẫn nguồn cần người: {human}")
    if apply or remove:
        return StepResult(O.APPLY, f"sửa {apply}, xoá {remove}")
    return StepResult(O.APPROVE)


def _section_of_claims(md: str, claims: dict[str, dict]) -> dict[str, str]:
    idx = SectionIndex(md)
    hits = scan_numbers(md)
    low = strip_diacritics(md)
    out = {}
    for cid, c in claims.items():
        keys = claim_value_keys(c.get("new_value") or c["verbatim"])
        pos = next((h.start for h in hits if h.key in keys), None)
        if pos is None:
            i = low.find(strip_diacritics(c["verbatim"])[:40])
            pos = i if i >= 0 else None
        out[cid] = idx.at(pos) if pos is not None else GLOBAL
    return out


def fact_apply(ctx: StepContext) -> StepResult:
    md, _ = _article(ctx)
    fc = latest(ctx, "FactCheck").payload
    so = latest(ctx, "FactSignoff").payload
    inv = latest(ctx, "FactInventory").payload
    by_id = {c["claim_id"]: c for c in inv["claims"]}
    items = rules.fact_apply_items(fc, so["apply"], so["remove"], by_id, _section_of_claims(md, by_id))
    # Cập nhật bảng fact TRƯỚC khi recheck: số mới phải khớp number_scan, claim bị xoá thành BO
    save(ctx, "FactInventory", rules.update_inventory_after_factcheck(inv, fc, so["apply"], so["remove"]))
    if items:
        _apply_p9(ctx, items, "P9 sửa theo fact-check", "Edits:P10")
    _purge_after_factcheck(ctx, [by_id[cid]["verbatim"] for cid in so["remove"] if cid in by_id])
    return StepResult(O.DONE)


def _distinctive(key: str) -> bool:
    """Khoá giá trị của number_scan ("num:750", "num:19.2", "date:2026-10-05") có đủ đặc trưng để truy theo không."""
    kind, _, value = key.partition(":")
    if kind != "num":
        return True
    try:
        number = float(value)
    except ValueError:
        return False
    return number >= 100 or number != int(number)


def _purge_after_factcheck(ctx: StepContext, removed_verbatims: list[str]) -> None:
    """Lưới an toàn sau P9: model xoá sót, hoặc cả lô edit bị từ chối vì vượt ngưỡng, thì code tự xoá câu / dòng bảng
    còn số %, tiền, ngày không khớp fact đã duyệt (đúng tiêu chí của FACT_RECHECK) và dòng bảng của claim bị xoá.
    Bảng hết dòng dữ liệu bị bỏ cả bảng kèm câu dẫn — không để lại tiêu đề bảng trơ trọi."""
    md, _ = _article(ctx)
    claims = latest(ctx, "FactInventory").payload["claims"]
    report = _number_report(ctx, md, claims)
    # Số thường (không phải %, tiền, ngày) của claim bị xoá cũng phải đi, vd. "hơn 750 giải đấu" — nhưng chỉ khi con số
    # đủ đặc trưng (≥ 100 hoặc có phần lẻ): số nhỏ như 2, 10 xuất hiện khắp bài với nghĩa khác
    gone: set[str] = set()
    for text in removed_verbatims:
        gone |= claim_value_keys(text)
    gone = {k for k in gone - approved_value_keys(claims) if _distinctive(k)}
    bad = [(h.start, h.end) for h in report.unmatched if h.kind in OVERRIDE_KINDS or h.key in gone]
    res = purge_unverified(md, bad, removed_verbatims)
    if not res.removed:
        return
    _save_article(ctx, res.md, f"code xoá {len(res.removed)} câu/dòng còn dữ kiện chưa kiểm chứng")
    save(ctx, "FactPurge", {"removed": res.removed})
    add_warning(ctx, "fact_apply", "purged",
                f"code xoá {len(res.removed)} câu/dòng bảng còn dữ kiện không kiểm chứng được sau lượt sửa của model: "
                + " | ".join(t[:70] for t in res.removed[:8]))


def fact_recheck(ctx: StepContext) -> StepResult:
    md, _ = _article(ctx)
    report = _number_report(ctx, md, latest(ctx, "FactInventory").payload["claims"])
    if report.override_fail:
        bad = [h.raw for h in report.unmatched if h.kind in ("PCT", "CUR", "DATE")]
        return StepResult(O.FAIL, f"sau fact-check bài còn số không khớp fact đã duyệt: {bad}")
    return StepResult(O.APPROVE)


# ---------- B11–B12 ----------

def _strip_markers(md: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", SECTION_MARKER.sub("", md)).strip() + "\n"


def assemble(ctx: StepContext) -> StepResult:
    md, _ = _article(ctx)
    article = _strip_markers(md)
    tc = latest(ctx, "TitleChoice").payload
    meta = latest(ctx, "Meta").payload
    inv = latest(ctx, "FactInventory").payload["claims"]
    rub = latest(ctx, "RubricResult").payload
    warnings = (latest(ctx, "Warnings", required=False) or type("x", (), {"payload": {"items": []}})).payload["items"]
    decisions = ctx.conn.execute(
        "SELECT gate, actor, action FROM gate_decisions WHERE job_id = %s ORDER BY created_at", (ctx.job_id,)).fetchall()
    so = (latest(ctx, "FactSignoff", required=False) or type("x", (), {"payload": {}})).payload
    fixes = (latest(ctx, "Edits:P9", required=False) or type("x", (), {"payload": {"applied": []}})).payload["applied"]
    change_log = "\n".join([
        "## Nội dung", f"- Viết theo outline {len(latest(ctx, 'GapIgPlan').payload['outline'])} section; QA sửa {len(fixes)} chỗ.",
        "## Số liệu", f"- Cập nhật theo nguồn: {so.get('apply', [])}; xoá vì không xác minh được: {so.get('remove', [])}.",
        "## SEO", f"- Title: {tc.get('title')}", f"- Rubric: {rub['total']}/{rub['max_total']} ({'đạt' if rub['pass'] else 'trượt'})",
        "## Việc còn lại cho người", *[f"- [{w['gate']}] {w['reason'][:200]}" for w in warnings[-10:]],
    ])
    package = {
        "article_markdown": article, "title": tc.get("title", ""), "meta": tc.get("meta", ""), "slug": meta["slug"],
        "jsonld": jsonld.build(_jsonld_data(ctx, meta, tc.get("title"))),
        "image_plan": meta["image_plan"], "internal_links": meta["internal_links"],
        "fact_table": [{"claim_id": c["claim_id"], "value": c.get("new_value") or c["verbatim"], "origin": c["origin"],
                        "source": c.get("verification_source")} for c in _approved(inv)],
        "rubric": rub, "change_log_md": change_log,
        "open_placeholders": len(rules.PLACEHOLDER.findall(article)),
        "approvals": [f"{d['gate']}:{d['actor']}:{d['action']}" for d in decisions],
    }
    save(ctx, "FinalPackage", package)
    return StepResult(O.DONE)


def final_gate(ctx: StepContext) -> StepResult:
    job = job_row(ctx)
    pkg = latest(ctx, "FinalPackage").payload
    overlap = latest(ctx, "OverlapReport").payload.get("ratio", 0.0)
    g = ctx.config["gates"]
    rule = rules.final_rules(pkg, job["branch"], overlap, ctx.config["thresholds"], bool(job["ymyl"]),
                             g.get("ymyl_final_human", True))
    if rule.force_escalate and job["ymyl"] and g.get("ymyl_final_human", True) and len(rule.failures) == 1:
        return StepResult(O.ESCALATE, "YMYL: bài đã lắp ráp xong (FinalPackage), cần người duyệt cuối trước khi đăng")
    artifact = {"title": pkg["title"], "meta": pkg["meta"], "slug": pkg["slug"], "article_markdown": pkg["article_markdown"]}
    return run_gate(ctx, "final", "FinalPackage", artifact, {"change_log": pkg["change_log_md"]}, rule,
                    {ctx.config["models"]["writer"]["provider"]}, model_key="gate_heavy")


def publish(ctx: StepContext) -> StepResult:
    latest(ctx, "FinalPackage")  # không có bài đã lắp ráp thì không ghi "đã đăng" (StepFailed, không tính vào hạn mức tuần)
    cms = ctx.config["adapters"]["cms"]
    week = ctx.conn.execute("SELECT count(*) AS n FROM publications WHERE published_at > now() - interval '7 days'").fetchone()["n"]
    if week >= cms.get("max_posts_per_week", 10):
        return StepResult(O.DEFER, f"đã đủ {week} bài/tuần", delay=timedelta(hours=12))
    if cms.get("provider", "none") != "none":
        return StepResult(O.ESCALATE, f"CmsPublisher '{cms['provider']}' chưa được cài; đăng tay từ FinalPackage")
    ctx.conn.execute("INSERT INTO publications (job_id, url, cms_status) VALUES (%s, NULL, 'exported') "
                     "ON CONFLICT (job_id) DO NOTHING", (ctx.job_id,))
    commit_usage(ctx.conn, ctx.job_id)
    return StepResult(O.DONE)


def gsc_28d(ctx: StepContext) -> StepResult:
    """GSC API chưa có (§6.4, mode csv): kết thúc job; so sánh 28 ngày trước/sau làm tay từ CSV export."""
    if ctx.config["adapters"]["gsc"].get("mode", "csv") == "csv":
        add_warning(ctx, "gsc", "manual", "đo GSC 28 ngày trước/sau: nhập CSV export thủ công")
        return StepResult(O.DONE)
    return StepResult(O.ESCALATE, "GSC API chưa được cài")
