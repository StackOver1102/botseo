"""Luồng rework (nhánh A đơn giản, jobs.flow = 'rework'): REWORK → REWORK_CHECK → DONE.

Đầu vào chỉ là bài có sẵn (thường dán từ CMS) + keyword tuỳ chọn. Không keyword map, SERP, gap/IG, title, meta, QA rubric:
một lượt model viết lại cả bài, rồi code kiểm tra những điều đo được. Sai thì gửi lại kèm lỗi cụ thể (tối đa
gates.revise_max.rework lượt), hết lượt thì đi tiếp kèm Warnings (hệ thống không có người duyệt).
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter

from vnseo.facts import scan_numbers
from vnseo.uniqueness import containment

from ..executor import StepContext, StepResult
from ..states import Outcome as O
from .common import StepFailed, add_warning, feedback_for, job_input, latest, runner, base_vars, save, save_feedback

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.M)
_WORD = re.compile(r"\w+", re.U)


def _headings(md: str) -> list[str]:
    return [m.group(2) for m in _HEADING.finditer(md)]


def _words(md: str) -> int:
    return len(_WORD.findall(md))


def _numbers(md: str) -> Counter:
    return Counter(h.key for h in scan_numbers(md))


def _body(md: str) -> str:
    """Bỏ heading: heading được giữ nguyên có chủ đích, không tính vào độ trùng câu chữ."""
    return _HEADING.sub("", md)


_SPELLED = {1: "một", 2: "hai", 3: "ba", 4: "bốn", 5: "năm", 6: "sáu", 7: "bảy", 8: "tám", 9: "chín", 10: "mười"}


def _spelled_ok(key: str, new: str) -> bool:
    """Số nhỏ viết thành chữ ("3 năm" → "ba năm") vẫn là giữ đúng con số."""
    try:
        v = float(key.split(":", 1)[1])
    except (IndexError, ValueError):
        return False
    word = _SPELLED.get(int(v)) if v == int(v) else None
    return bool(word) and re.search(rf"\b{word}\b", new.lower()) is not None


def split_chunks(md: str, max_words: int = 700) -> list[str]:
    """Cắt bài theo heading thành các phần ≤ max_words (một section dài hơn thì đứng riêng). Nối lại bằng "\n\n"."""
    starts = [m.start() for m in _HEADING.finditer(md)]
    bounds = [0, *[s for s in starts if s > 0], len(md)]
    sections = [md[a:b].strip() for a, b in zip(bounds, bounds[1:]) if md[a:b].strip()]
    chunks, cur = [], []
    for sec in sections:
        if cur and _words("\n\n".join(cur + [sec])) > max_words:
            chunks.append("\n\n".join(cur))
            cur = []
        cur.append(sec)
    if cur:
        chunks.append("\n\n".join(cur))
    return chunks


def _raw_numbers(md: str) -> dict[str, str]:
    return {h.key: h.raw for h in scan_numbers(md)}


def _kn(s: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", s)).strip().lower()


def brief(ctx: StepContext, inp, art) -> dict:
    """Tự xác định YMYL + keyword (thay triage/keyword map). Keyword người dùng nhập được ưu tiên."""
    out = runner(ctx).run(job_id=ctx.job_id, step=ctx.state.state.value, prompt_id="RWA",
                          variables={**base_vars(ctx), "ARTICLE_MD": (inp.article_md or "")[:20000]},
                          input_hashes=[art.sha256], ctx={"source_text": inp.article_md or ""})
    ai = out.parsed
    user = [k.keyword.strip() for k in inp.keywords if k.keyword.strip()]
    primary = user[0] if user else ai["primary_keyword"]
    kws = [k for k in dict.fromkeys([*user[1:], *([] if user else []), *ai["keywords"], ai["primary_keyword"]])
           if k and _kn(k) != _kn(primary)][:10]
    ymyl = bool(ai["ymyl"] or inp.ymyl)
    b = {"ymyl": ymyl, "ymyl_reason": ai["ymyl_reason"], "primary_keyword": primary, "keywords": kws,
         "source": "nguoi_dung" if user else "ai"}
    save(ctx, "ReworkBrief", b, run_id=out.run_id)
    ctx.conn.execute("UPDATE jobs SET ymyl = %s WHERE id = %s", (ymyl, ctx.job_id))
    return b


def chunk_keywords(b: dict, old_chunks: list[str]) -> list[dict]:
    """Yêu cầu keyword cho từng phần: keyword có trong phần gốc phải còn trong phần viết lại; keyword chưa có trong bài
    gốc thì chèn ở phần 1; keyword chính phải có ở phần 1 (đoạn mở đầu hoặc heading)."""
    whole = _kn("\n".join(old_chunks))
    allk = [k for k in [b.get("primary_keyword"), *b.get("keywords", [])] if k]
    specs = []
    for i, old in enumerate(old_chunks):
        o = _kn(old)
        keep = [k for k in allk if _kn(k) in o]
        add = [k for k in allk if _kn(k) not in whole] if i == 0 else []
        specs.append({"keep": keep, "add": add, "primary": b.get("primary_keyword") if i == 0 else None})
    return specs


def rework(ctx: StepContext) -> StepResult:
    """Viết lại theo từng phần (~700 chữ): một lượt cả bài dài thì model viết kỹ phần đầu rồi chép gần nguyên phần cuối.
    Vòng sửa (REWORK_CHECK gửi lại): chỉ viết lại phần còn lỗi, kèm lỗi của riêng phần đó."""
    inp, art = job_input(ctx)
    th = ctx.config["thresholds"]
    original = inp.article_md or ""
    if not original.strip():
        raise StepFailed("job rework không có nội dung bài gốc (article_md rỗng)")
    b = latest(ctx, "ReworkBrief", required=False)
    b = b.payload if b else brief(ctx, inp, art)
    old_chunks = split_chunks(original, th.get("rework_chunk_words", 700))
    specs = chunk_keywords(b, old_chunks)
    ymyl_note = ("Chủ đề YMYL (" + (b.get("ymyl_reason") or "ảnh hưởng tiền bạc/sức khoẻ/pháp lý") + "): giữ nguyên mọi lời "
                 "khuyến cáo, điều kiện, rủi ro của bản cũ; không viết tuyệt đối hoá (chắc chắn, 100%, cam kết lãi…) nếu bản "
                 "cũ không viết vậy.") if b.get("ymyl") else ""
    prev = latest(ctx, "ArticleDraft", required=False)
    prev_chunks = prev.payload.get("chunks") if prev and feedback_for(ctx, "rework") else None
    if prev_chunks and len(prev_chunks) != len(old_chunks):
        prev_chunks = None
    new_chunks, run_id = [], None
    for i, old in enumerate(old_chunks):
        feedback = None
        if prev_chunks:
            errs, _ = check(old, prev_chunks[i], th, specs[i])
            if not errs:
                new_chunks.append(prev_chunks[i])  # phần này đã đạt: giữ nguyên
                continue
            feedback = "Bản viết lại trước của phần này chưa đạt:\n- " + "\n- ".join(errs)
        sp = specs[i]
        kw_text = "; ".join(filter(None, [
            f"keyword chính (phải có ở đoạn mở đầu hoặc một heading): {sp['primary']}" if sp["primary"] else "",
            f"giữ nguyên chữ: {', '.join(sp['keep'])}" if sp["keep"] else "",
            f"chèn tự nhiên mỗi cụm ít nhất 1 lần: {', '.join(sp['add'])}" if sp["add"] else ""])) or "(không có)"
        variables = {**base_vars(ctx), "ARTICLE_MD": old, "KEYWORDS": kw_text, "YMYL_NOTE": ymyl_note,
                     "SECTION_COUNT": len(_headings(old)),
                     "WORD_COUNT": _words(old), "TARGET_WORDS": round(_words(old) * 1.08), "PART": f"phần {i + 1}/{len(old_chunks)}" if len(old_chunks) > 1 else "cả bài"}
        out = runner(ctx).run(job_id=ctx.job_id, step=ctx.state.state.value, prompt_id="RW", variables=variables,
                              input_hashes=[art.sha256, str(i)], feedback=feedback, ctx={})
        new_chunks.append(out.parsed.strip())
        run_id = out.run_id
        ctx.heartbeat()
    md = "\n\n".join(new_chunks)
    save(ctx, "ArticleDraft", {"body_markdown": md, "chunks": new_chunks, "keyword_placement": [], "can_nguoi": [],
                               "change_log": []}, run_id=run_id)
    return StepResult(O.DONE)


def check(original: str, new: str, th: dict, kw: dict | None = None) -> tuple[list[str], dict]:
    """Lỗi đo được bằng code (mỗi lỗi là một hướng dẫn sửa cụ thể) và số liệu báo cáo. kw: yêu cầu keyword của phần."""
    errs: list[str] = []
    kw = kw or {}
    n = _kn(new)
    lost = [k for k in kw.get("keep", []) + kw.get("add", []) if _kn(k) not in n]
    if lost:
        errs.append(f"Thiếu từ khoá (giữ đúng chữ, có dấu): {', '.join(lost)} — đưa vào câu văn tự nhiên, không nhồi.")
    if kw.get("primary"):
        p = _kn(kw["primary"])
        intro = _kn(" ".join(new[:m.start()].split()[:150])) if (m := _HEADING.search(new)) else _kn(new)
        heads = [_kn(h) for h in _headings(new)]
        if p not in intro and not any(p in h for h in heads):
            errs.append(f"Keyword chính '{kw['primary']}' phải xuất hiện ở đoạn mở đầu hoặc trong một heading.")
    h_old, h_new = _headings(original), _headings(new)
    if len(h_new) != len(h_old):
        errs.append(f"Bản cũ có {len(h_old)} heading, bản mới có {len(h_new)}. Giữ đủ và đúng thứ tự các heading: "
                    + " | ".join(h_old))
    w_old, w_new = _words(original), _words(new)
    lo, hi = th.get("rework_length_ratio", [1.0, 1.3])
    ratio = w_new / w_old if w_old else 1.0
    if not lo <= ratio <= hi:
        errs.append(f"Độ dài {w_new} chữ so với bản cũ {w_old} chữ ({ratio:.0%}); cần {lo:.0%}–{hi:.0%}. "
                    + (f"Bài phải dài ít nhất bằng bản cũ: viết thêm khoảng {int(w_old * lo) - w_new + int(w_old * 0.05)} chữ "
                       "bằng cách giải thích rõ hơn, thêm ví dụ minh hoạ, cách dùng cụ thể cho người đọc — dựa trên ý đã có, "
                       "không thêm con số hay dữ kiện mới." if ratio < lo else "Bớt phần độn chữ, lặp ý."))
    n_old, n_new = _numbers(original), _numbers(new)
    raw_old, raw_new = _raw_numbers(original), _raw_numbers(new)
    missing = [raw_old[k] for k in n_old if k not in n_new and not _spelled_ok(k, new)]
    added = [raw_new[k] for k in n_new if k not in n_old]
    if missing:
        errs.append(f"Thiếu các con số của bản cũ: {', '.join(missing[:15])} — giữ y hệt.")
    if added:
        errs.append(f"Có con số không có trong bản cũ: {', '.join(added[:15])} — bỏ đi, không thêm dữ kiện.")
    overlap = containment(_body(new), _body(original), th.get("shingle_n", 5))
    cmax = th.get("rework_containment_max", 0.25)
    if overlap.ratio > cmax:
        errs.append(f"Còn trùng câu chữ với bản cũ {overlap.ratio:.0%} (tối đa {cmax:.0%}). Viết lại các đoạn sau bằng câu "
                    "chữ mới: " + " | ".join(p[:120] for p in overlap.top_passages[:8]))
    report = {"headings_old": len(h_old), "headings_new": len(h_new), "words_old": w_old, "words_new": w_new,
              "length_ratio": round(ratio, 3), "overlap": round(overlap.ratio, 3), "overlap_passages": overlap.top_passages[:10],
              "numbers_missing": missing, "numbers_added": added, "keywords_missing": lost}
    return errs, report


def rework_check(ctx: StepContext) -> StepResult:
    inp, _ = job_input(ctx)
    new = latest(ctx, "ArticleDraft").payload["body_markdown"]
    if not new.strip():
        raise StepFailed("bản viết lại rỗng")
    th = ctx.config["thresholds"]
    errs, report = check(inp.article_md or "", new, th)
    # keyword: kiểm tra theo từng phần (để vòng sửa chỉ viết lại phần thiếu)
    b = latest(ctx, "ReworkBrief", required=False)
    chunks = latest(ctx, "ArticleDraft").payload.get("chunks") or []
    old_chunks = split_chunks(inp.article_md or "", th.get("rework_chunk_words", 700))
    if b and len(chunks) == len(old_chunks):
        for i, (spec, c) in enumerate(zip(chunk_keywords(b.payload, old_chunks), chunks)):
            kw_errs = [e for e in check(old_chunks[i], c, th, spec)[0] if "eyword" in e or "từ khoá" in e]
            errs += [f"phần {i + 1}: {e}" for e in kw_errs]
        report["ymyl"] = b.payload["ymyl"]
        report["primary_keyword"] = b.payload["primary_keyword"]
    save(ctx, "OverlapReport", {"branch": "A", "ratio": report["overlap"], "rework": report})
    if errs:
        left = ctx.config["gates"]["revise_max"].get("rework", 2) - ctx.state.revise_count.get("rework", 0)
        if left > 0:
            save_feedback(ctx, "rework", "Bản viết lại trước chưa đạt:\n- " + "\n- ".join(errs))
            return StepResult(O.REVISE, "; ".join(e[:120] for e in errs))
        add_warning(ctx, "rework", "rework_check", "hết lượt sửa, đi tiếp với lỗi: " + "; ".join(errs))
    save(ctx, "FinalPackage", {"title": "", "meta": "", "slug": "", "article_markdown": new, "jsonld": None,
                               "change_log_md": "\n".join(f"- {k}: {v}" for k, v in report.items() if k != "overlap_passages"),
                               "sha256": hashlib.sha256(new.encode()).hexdigest()})
    return StepResult(O.APPROVE, f"{report['words_new']} chữ, trùng {report['overlap']:.0%}" + (" (có cảnh báo)" if errs else ""))
