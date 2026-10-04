"""Validator phía code cho output LLM (§4.1 `validators`). Mỗi validator: (output, ctx) -> list lỗi.

Lỗi có dạng "đường_dẫn | quy tắc | giá trị" để đưa thẳng vào repair_v1.
Validator cần dữ liệu ngữ cảnh (bài viết, dòng keyword…) đọc từ ctx; thiếu key thì bị bỏ qua và ghi vào `skipped`.
"""
from __future__ import annotations

import difflib
import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable

import jsonschema

from vnseo.facts import scan_numbers
from vnseo.seo import slug_is_valid

NOT_FOUND = "KHÔNG TÌM THẤY"
_WS = re.compile(r"\s+")


_QUOTES = re.compile(r'["\\“”]')


def _norm(s: str) -> str:
    """So khớp câu trích: bỏ dấu ngoặc kép/backslash (model hay escape sai khi trích từ JSON) và gộp khoảng trắng."""
    return _WS.sub(" ", _QUOTES.sub("", unicodedata.normalize("NFC", s))).strip()


def _contains(haystack: str, needle: str) -> bool:
    return _norm(needle) in _norm(haystack)


def _enum_casefix(schema: dict, value: Any) -> Any:
    """Anthropic không đảm bảo hoa/thường của enum (§4.3): chuẩn hoá trước khi báo lỗi."""
    if isinstance(value, dict) and "properties" in schema:
        return {k: _enum_casefix(schema["properties"].get(k, {}), v) for k, v in value.items()}
    if isinstance(value, list) and "items" in schema:
        return [_enum_casefix(schema["items"], v) for v in value]
    if isinstance(value, str) and "enum" in schema and value not in schema["enum"]:
        for opt in schema["enum"]:
            if isinstance(opt, str) and opt.lower() == value.lower():
                return opt
    return value


class Missing(Exception):
    pass


def _need(ctx: dict, key: str) -> Any:
    if key not in ctx:
        raise Missing(key)
    return ctx[key]


def _items(out: dict, key: str = "items") -> list[dict]:
    return out.get(key) or []


# ---------- validator ----------

_EVIDENCE_SPLIT = re.compile(r"\s*(?:\||…|\.\.\.)\s*")


def _evidence_ok(src: str, evidence: str) -> bool:
    """Judge hay gộp nhiều câu trích bằng " | " hoặc "…": mỗi đoạn (≥8 ký tự) phải là chuỗi con của nguồn."""
    if evidence.strip() == NOT_FOUND or _contains(src, evidence):
        return True
    parts = [p for p in _EVIDENCE_SPLIT.split(evidence) if len(p.strip()) >= 8]
    return bool(parts) and all(_contains(src, p) for p in parts)


def evidence_substring_or_notfound(out, ctx):
    if ctx.get("soft_evidence"):  # QA: bằng chứng chỉ để audit, trích sai không đáng một vòng repair qua web
        return []
    src = _need(ctx, "source_text")
    return [f"$.items[{i}].evidence | substring | {it['evidence'][:80]!r}"
            for i, it in enumerate(_items(out) or _items(out, "checks"))
            if not _evidence_ok(src, it["evidence"])]


def _unmark(ctx: dict, text: str) -> str:
    """Bài nguồn nhánh B được datamark (ADR-8, từ nối bằng ký hiệu): so khớp trên bản đã bỏ ký hiệu."""
    mark = ctx.get("datamark")
    return text.replace(mark, " ") if mark else text


def _verbatim_ok(src: str, verbatim: str, ctx: dict) -> bool:
    # _evidence_ok: model hay nối các ô của một dòng bảng bằng " | "
    return _evidence_ok(src, _unmark(ctx, verbatim))


def verbatim_substring_nfc(out, ctx):
    src = _unmark(ctx, _need(ctx, "source_text"))
    return [f"$.claims[{i}].verbatim | substring | {c['verbatim'][:80]!r}"
            for i, c in enumerate(out["claims"]) if not _verbatim_ok(src, c["verbatim"], ctx)]


def _prune_claims(out, ctx):
    """P2a/P2b: claim có verbatim không nằm trong bài thì bỏ riêng claim đó (và đánh số lại) thay vì repair cả lô —
    một lô 90 claim qua Claude mất ~2 phút; chỉ repair khi hơn một nửa sai (model hiểu sai yêu cầu)."""
    src = _unmark(ctx, _need(ctx, "source_text"))
    claims = out["claims"]
    keep = [c for c in claims if _verbatim_ok(src, c["verbatim"], ctx)]
    dropped = len(claims) - len(keep)
    if not dropped or len(keep) * 2 < len(claims):
        return out, []
    start = ctx.get("first_claim_index", 1)
    out["claims"] = [{**c, "claim_id": f"C{start + i:03d}"} for i, c in enumerate(keep)]
    return out, [f"bỏ {dropped}/{len(claims)} claim có verbatim không nằm trong bài"]


def quote_is_substring(out, ctx):
    src = _need(ctx, "source_text")
    return [f"$.issues[{i}].quote | substring | {it['quote'][:80]!r}"
            for i, it in enumerate(out["issues"]) if not _contains(src, it["quote"])]


def evidence_quotes_substring(out, ctx):
    src = _need(ctx, "source_text")
    errs = []
    for i, d in enumerate(out["dimensions"]):
        errs += [f"$.dimensions[{i}].evidence_quotes | substring | {q[:60]!r}" for q in d["evidence_quotes"] if not _contains(src, q)]
    return errs


def ten_dimensions(out, ctx):
    ids = [d["dim_id"] for d in out["dimensions"]]
    errs = [] if sorted(ids) == sorted(f"D{i}" for i in range(1, 11)) else [f"$.dimensions | D1–D10 mỗi chiều 1 lần | {ids}"]
    errs += [f"$.dimensions[{i}] | DU_MAU cần ≥2 trích dẫn | {len(d['evidence_quotes'])}"
             for i, d in enumerate(out["dimensions"]) if d["status"] == "DU_MAU" and len(d["evidence_quotes"]) < 2]
    return errs


def claim_id_sequence(out, ctx):
    start = ctx.get("first_claim_index", 1)
    want = [f"C{i:03d}" for i in range(start, start + len(out["claims"]))]
    got = [c["claim_id"] for c in out["claims"]]
    return [] if got == want else [f"$.claims[*].claim_id | tuần tự từ C{start:03d} | {got[:5]}…"]


def no_duplicate_verbatim(out, ctx):
    existing = {_norm(v) for v in ctx.get("existing_verbatims", [])}
    return [f"$.claims[{i}].verbatim | trùng claim đã có | {c['verbatim'][:60]!r}"
            for i, c in enumerate(out["claims"]) if _norm(c["verbatim"]) in existing]


def kw_row_reconcile(out, ctx):
    want = Counter(r["keyword_nfc"] for r in _need(ctx, "input_rows"))
    got = Counter(r["keyword_nfc"] for r in out["rows"])
    errs = [f"$.rows | thiếu dòng | {k!r}" for k in (want - got)]
    return errs + [f"$.rows | thừa dòng | {k!r}" for k in (got - want)]


def single_primary(out, ctx):
    n = sum(1 for r in out["rows"] if r["role"] == "primary")
    if ctx.get("secondary_batch"):  # lô keyword thứ 2 trở đi: primary nằm ở lô đầu
        return [] if n == 0 else [f"$.rows[*].role | lô phụ không có primary | {n}"]
    return [] if n == 1 else [f"$.rows[*].role | đúng 1 primary | {n}"]


def volume_equals_input(out, ctx):
    vol = {r["keyword_nfc"]: r.get("volume") for r in _need(ctx, "input_rows")}
    return [f"$.rows[{i}].volume | bằng đầu vào | {r['volume']} ≠ {vol[r['keyword_nfc']]}"
            for i, r in enumerate(out["rows"]) if r["keyword_nfc"] in vol and r["volume"] != vol[r["keyword_nfc"]]]


def ig_min_2(out, ctx):
    return [] if len(out["ig"]) >= 2 else [f"$.ig | ≥2 IG | {len(out['ig'])}"]


def outline_refs_exist(out, ctx):
    facts = set(_need(ctx, "approved_fact_ids"))
    igs = {ig["ig_id"] for ig in out["ig"]}
    errs = []
    for i, s in enumerate(out["outline"]):
        errs += [f"$.outline[{i}].fact_ids | tồn tại | {f}" for f in s["fact_ids"] if f not in facts]
        errs += [f"$.outline[{i}].ig_ids | tồn tại | {g}" for g in s["ig_ids"] if g not in igs]
    return errs


def url_key(url: str) -> str:
    """So URL không phân biệt mã hoá %XX / chữ có dấu, tham số truy vấn, www., dấu / cuối, http/https."""
    from urllib.parse import unquote, urlsplit
    p = urlsplit(unquote(url.strip()))
    host = p.netloc.lower().removeprefix("www.")
    # bỏ dấu: model hay "thêm dấu" vào slug không dấu (1-tran-bong-da → 1-trận-bóng-đá)
    from vnseo.vn_text.normalize import strip_diacritics
    return strip_diacritics(f"{host}{p.path.rstrip('/')}")


def coverage_urls_in_serp(out, ctx):
    serp = {url_key(u) for u in _need(ctx, "serp_urls")}
    return [f"$.coverage[{i}].url | thuộc SERP | {c['url']}" for i, c in enumerate(out["coverage"])
            if url_key(c["url"]) not in serp]


def new_claims_have_url(out, ctx):
    igs = {ig["ig_id"] for ig in out["ig"]}
    errs = [f"$.new_claims[{i}].source_url | http(s) | {c['source_url']!r}"
            for i, c in enumerate(out["new_claims"]) if not c["source_url"].startswith(("http://", "https://"))]
    return errs + [f"$.new_claims[{i}].ig_id | tồn tại | {c['ig_id']}"
                   for i, c in enumerate(out["new_claims"]) if c["ig_id"] and c["ig_id"] not in igs]


def entry_ids_exist(out, ctx):
    ids = set(_need(ctx, "candidate_contents"))
    return [f"$.selections[{i}].entry_id | có trong ung_vien_kho | {s['entry_id']}"
            for i, s in enumerate(out["selections"]) if s["entry_id"] not in ids]


def excerpt_is_substring(out, ctx):
    contents = _need(ctx, "candidate_contents")
    return [f"$.selections[{i}].excerpt | substring của {s['entry_id']} | {s['excerpt'][:60]!r}"
            for i, s in enumerate(out["selections"])
            if s["entry_id"] in contents and not _contains(contents[s["entry_id"]], s["excerpt"])]


def one_entry_per_ig(out, ctx):
    """Prompt P5: mỗi IG ≤2 mục; một mục không dùng cho 2 IG."""
    per_ig = Counter(s["ig_id"] for s in out["selections"])
    per_entry = Counter(s["entry_id"] for s in out["selections"])
    return ([f"$.selections | IG ≤2 mục | {k}" for k, n in per_ig.items() if n > 2]
            + [f"$.selections | 1 mục 1 IG | {k}" for k, n in per_entry.items() if n > 1])


def max_entries(out, ctx):
    limit = _need(ctx, "max_entries")
    return [] if len(out["selections"]) <= limit else [f"$.selections | ≤{limit} | {len(out['selections'])}"]


def entry_not_expired(out, ctx):
    expired = set(ctx.get("expired_entry_ids", []))
    return [f"$.selections[{i}].entry_id | còn hiệu lực | {s['entry_id']}" for i, s in enumerate(out["selections"]) if s["entry_id"] in expired]


def all_checks_present(out, ctx):
    want = set(_need(ctx, "expected_check_ids"))
    got = [it["check_id"] for it in (_items(out) or _items(out, "checks"))]
    errs = [f"$.items | thiếu check | {c}" for c in sorted(want - set(got))]
    return errs + [f"$.items | check lạ hoặc trùng | {c}" for c, n in Counter(got).items() if c not in want or n > 1]


def decision_consistent_with_checks(out, ctx):
    has_no = any(c["verdict"] == "NO" for c in out["checks"])
    errs = []
    if out["decision"] == "approve" and has_no:
        errs.append("$.decision | có NO thì không được approve | approve")
    if out["decision"] == "revise" and not out["revise_instructions"].strip():
        errs.append("$.revise_instructions | bắt buộc khi revise | ''")
    if out["decision"] == "revise" and ctx.get("revise_left", 1) <= 0:
        errs.append("$.decision | hết lượt revise, chỉ approve/escalate | revise")
    return errs


def find_unique_substring(out, ctx):
    src = unicodedata.normalize("NFC", _need(ctx, "source_text"))
    return [f"$.edits[{i}].find | xuất hiện đúng 1 lần | {src.count(unicodedata.normalize('NFC', e['find']))}"
            for i, e in enumerate(out["edits"]) if src.count(unicodedata.normalize("NFC", e["find"])) != 1]


def _prune_edits(out, ctx):
    """P7b/P9: edit có `find` không xuất hiện đúng 1 lần thì bỏ riêng edit đó (apply_edits cũng từ chối từng edit),
    không repair cả lô — một lô 28 edit qua Haiku ~0,05 USD/lượt. Chỉ repair khi hơn một nửa sai."""
    src = unicodedata.normalize("NFC", _need(ctx, "source_text"))
    edits = out["edits"]
    keep = [e for e in edits if src.count(unicodedata.normalize("NFC", e["find"])) == 1]
    dropped = len(edits) - len(keep)
    if not dropped or len(keep) * 2 < len(edits):
        return out, []
    out["edits"] = keep
    return out, [f"bỏ {dropped}/{len(edits)} edit có find không khớp đúng 1 chỗ"]


def _kw_norm(s: str) -> str:
    return _WS.sub(" ", unicodedata.normalize("NFC", s)).strip().lower()


def keywords_in_article(out, ctx):
    src = _kw_norm(_need(ctx, "source_text"))
    bad = [k for k in [out["primary_keyword"], *out["keywords"]] if k and _kw_norm(k) not in src]
    return [f"$.keywords | xuất hiện nguyên văn trong bài | {k}" for k in bad]


def _prune_keywords(out, ctx):
    """RWA: bỏ keyword không có trong bài; keyword chính không có thì lấy keyword phụ đầu tiên (không repair)."""
    src = _kw_norm(_need(ctx, "source_text"))
    ok = lambda k: bool(k.strip()) and _kw_norm(k) in src  # noqa: E731
    kws = [k for k in dict.fromkeys(out["keywords"]) if ok(k)]
    notes = [f"bỏ keyword không có trong bài: {k}" for k in out["keywords"] if not ok(k)]
    if not ok(out["primary_keyword"]):
        notes.append(f"keyword chính '{out['primary_keyword']}' không có trong bài")
        out["primary_keyword"] = kws.pop(0) if kws else ""
    out["keywords"] = [k for k in kws if _kw_norm(k) != _kw_norm(out["primary_keyword"])]
    return out, notes


def check_ids_subset(out, ctx):
    allowed = set(_need(ctx, "allowed_check_ids"))
    return [f"$.edits[{i}].check_id | thuộc lỗi được giao | {e['check_id']}" for i, e in enumerate(out["edits"]) if e["check_id"] not in allowed]


_PLACEHOLDER = re.compile(r"\[\s*(CẦN NGƯỜI|CAN NGUOI|TODO|TBD)[^\]]*\]", re.I)


def no_placeholders(out, ctx):
    """Chạy tự động, không có người điền: bài viết/edit không được chứa placeholder (hard rule 2)."""
    texts = []
    if isinstance(out, str):
        texts.append(("$", out))
    if isinstance(out, dict) and "body_markdown" in out:
        texts.append(("$.body_markdown", out["body_markdown"]))
    if isinstance(out, dict) and "edits" in out:
        texts += [(f"$.edits[{i}].replace", e["replace"]) for i, e in enumerate(out["edits"])]
    return [f"{path} | không placeholder: bỏ ý hoặc viết tổng quát | {m.group(0)[:80]!r}"
            for path, text in texts for m in _PLACEHOLDER.finditer(text)]


def quote_in_provided_sources(out, ctx):
    """P10-S: URL phải là nguồn đã đưa cho claim đó, quote phải nằm trong văn bản đã tải; DUNG phải có nguồn."""
    provided = _need(ctx, "provided_sources")  # {claim_id: {url: text}}
    mark = ctx.get("datamark")
    unmark = (lambda q: q.replace(mark, " ")) if mark else (lambda q: q)
    errs = []
    for i, it in enumerate(out["items"]):
        texts = provided.get(it["claim_id"], {})
        if it["verdict"] in ("DUNG", "SAI", "CAN_CAP_NHAT") and not it["sources"]:
            errs.append(f"$.items[{i}].sources | {it['verdict']} phải có nguồn | []")
        for j, s in enumerate(it["sources"]):
            if s["url"] not in texts:
                errs.append(f"$.items[{i}].sources[{j}].url | là source_url đã cho | {s['url'][:80]}")
            elif not _contains(texts[s["url"]], unmark(s["quote"])):
                errs.append(f"$.items[{i}].sources[{j}].quote | câu nguyên văn trong nguồn | {s['quote'][:80]!r}")
    return errs


def slug_ascii(out, ctx):
    return [] if slug_is_valid(out["slug"]) else [f"$.slug | không dấu, gạch nối, ≤6 từ | {out['slug']!r}"]


def internal_links_whitelist(out, ctx):
    allowed = set(_need(ctx, "internal_urls"))
    return [f"$.internal_links[{i}].target_url | thuộc trang_noi_bo | {l['target_url']}"
            for i, l in enumerate(out["internal_links"]) if l["target_url"] not in allowed]


def markdown_nonempty(out, ctx):
    return [] if isinstance(out, str) and out.strip() else ["$ | Markdown không rỗng | ''"]


def no_new_numbers(out, ctx):
    allowed = {h.key for h in scan_numbers(json.dumps(_need(ctx, "input_data"), ensure_ascii=False))}
    return [f"$ | số không có trong dữ liệu | {h.raw}" for h in scan_numbers(out) if h.key not in allowed]


VALIDATORS: dict[str, Callable[[Any, dict], list[str]]] = {
    f.__name__: f for f in [
        evidence_substring_or_notfound, verbatim_substring_nfc, quote_is_substring, evidence_quotes_substring,
        ten_dimensions, claim_id_sequence, no_duplicate_verbatim, kw_row_reconcile, single_primary,
        volume_equals_input, ig_min_2, outline_refs_exist, coverage_urls_in_serp, new_claims_have_url,
        entry_ids_exist, excerpt_is_substring, one_entry_per_ig, max_entries, entry_not_expired,
        all_checks_present, decision_consistent_with_checks, find_unique_substring, check_ids_subset,
        quote_in_provided_sources, no_placeholders, keywords_in_article,
        slug_ascii, internal_links_whitelist, markdown_nonempty, no_new_numbers,
    ]
}
VALIDATORS["evidence_is_substring"] = evidence_substring_or_notfound
VALIDATORS["verbatim_substring"] = verbatim_substring_nfc

# Không phải kiểm tra mà là sửa/ghi đè bằng code — chạy sau khi validate (§5.7, §5.10, prompt G)
def _blocking_recompute(out, ctx):
    out["blocking_fail"] = any(it["verdict"] == "NO" and it["severity"] == "chan" for it in out["items"])
    return out


def _confidence_floor(out, ctx):
    if out["decision"] == "approve" and out["confidence"] < ctx.get("min_confidence", 0.8):
        out["decision"] = "escalate"
        out["escalate_reason"] = f"confidence {out['confidence']} < ngưỡng"
    return out


def _prune_coverage(out, ctx):
    """P4-G: URL coverage ngoài SERP (model bịa/cắt "...") thì bỏ entry đó thay vì repair cả output qua web."""
    serp = {url_key(u) for u in _need(ctx, "serp_urls")}
    keep = [c for c in out["coverage"] if url_key(c["url"]) in serp]
    dropped = len(out["coverage"]) - len(keep)
    if keep:  # bỏ hết thì để validator báo lỗi
        out["coverage"] = keep
    return out, [f"bỏ {dropped} coverage ngoài SERP"] if dropped and keep else []


def _prune_voice_quotes(out, ctx):
    """P3 ở chế độ tự học giọng văn từ trang SERP (ctx["voice_learning"]): câu trích không khớp nguồn thì bỏ câu đó;
    chiều còn < 2 câu trích thì chuyển KHONG_DU_MAU. Hồ sơ loại này chỉ giữ mô tả phong cách (authors.style_only xoá mọi
    câu trích trước khi lưu), nên bắt model chép đúng từng ký tự — nhất là dòng bảng tỷ số — chỉ tốn lượt repair.
    Chế độ thường (bài của chính tác giả) không đụng tới: câu trích là mẫu cho writer nên phải đúng nguyên văn."""
    if not ctx.get("voice_learning"):
        return out, []
    src = _need(ctx, "source_text")
    notes = []
    for d in out.get("dimensions", []):
        kept = [q for q in d.get("evidence_quotes", []) if _contains(src, q)]
        dropped = len(d.get("evidence_quotes", [])) - len(kept)
        d["evidence_quotes"] = kept
        if dropped:
            notes.append(f"{d.get('dim_id')}: bỏ {dropped} câu trích không khớp nguồn")
        if d.get("status") == "DU_MAU" and len(kept) < 2:
            d["status"] = "KHONG_DU_MAU"
    return out, notes


_NEAR_QUOTE_MIN = 0.9  # cùng ngưỡng fuzzy của UrlVerifier (FACT_SIGNOFF kiểm lại quote trên trang thật)


def _near_quote(text: str, quote: str) -> str | None:
    """Đoạn liên tục trong `text` khớp ≥90% độ dài `quote` (quote ≥ 40 ký tự) — trả đúng đoạn đó của nguồn."""
    q, t = _norm(quote), _norm(text)
    if len(q) < 40:
        return None
    m = difflib.SequenceMatcher(None, q, t, autojunk=False).find_longest_match(0, len(q), 0, len(t))
    return t[m.b:m.b + m.size].strip() if m.size / len(q) >= _NEAR_QUOTE_MIN else None


def _prune_fact_sources(out, ctx):
    """P10-S: URL khớp theo url_key; quote không tìm thấy trong nguồn thì bỏ source đó; claim hết nguồn → KHONG_XAC_MINH_DUOC
    (FACT_SIGNOFF xử lý như claim chưa kiểm chứng: bỏ khỏi bài), không repair cả lô qua web."""
    provided = _need(ctx, "provided_sources")
    mark = ctx.get("datamark")
    unmark = (lambda q: q.replace(mark, " ")) if mark else (lambda q: q)
    notes = []
    for it in out["items"]:
        texts = provided.get(it["claim_id"], {})
        by_key = {url_key(u): u for u in texts}
        kept = []
        for s in it["sources"]:
            url = s["url"] if s["url"] in texts else by_key.get(url_key(s["url"]))
            quote = unmark(s["quote"])
            if url and _evidence_ok(texts[url], quote):
                kept.append({**s, "url": url})
            elif url and (near := _near_quote(texts[url], quote)):
                # Model chép lệch vài ký tự ("Đorgu" thay cho "Dorgu"): dùng đúng câu trong nguồn thay vì bỏ nguồn —
                # bỏ thì claim đúng bị coi là không xác minh được và bị xoá khỏi bài
                kept.append({**s, "url": url, "quote": near})
                notes.append(f"{it['claim_id']}: quote lệch nhẹ, thay bằng câu nguyên văn trong nguồn")
            else:
                notes.append(f"{it['claim_id']}: bỏ nguồn {s['url'][:60]} (url/quote không khớp)")
        it["sources"] = kept
        if it["verdict"] in ("DUNG", "SAI", "CAN_CAP_NHAT") and not kept:
            notes.append(f"{it['claim_id']}: {it['verdict']} → KHONG_XAC_MINH_DUOC (hết nguồn hợp lệ)")
            it["verdict"] = "KHONG_XAC_MINH_DUOC"
    return out, notes


# Sửa output lỗi nhỏ bằng code TRƯỚC khi validate (thay cho một vòng repair qua web ~1-4 phút)
PRUNERS: dict[str, Callable[[Any, dict], tuple[Any, list[str]]]] = {
    "coverage_urls_in_serp": _prune_coverage,
    "verbatim_substring_nfc": _prune_claims,
    "find_unique_substring": _prune_edits,
    "keywords_in_article": _prune_keywords,
    "verbatim_substring": _prune_claims,
    "quote_in_provided_sources": _prune_fact_sources,
    "evidence_quotes_substring": _prune_voice_quotes,
}

POSTPROCESSORS: dict[str, Callable[[Any, dict], Any]] = {
    "blocking_recompute": _blocking_recompute,
    "confidence_floor": _confidence_floor,
}
# Xử lý ở bước sau (step handler / module code), không phải lúc parse output
DEFERRED = {"jsonschema", "code_override_blacklist", "code_override_numbers", "number_regex_vs_facts", "blacklist_scan",
            "section_markers", "heading_match_outline", "no_source_text_in_context_B", "edit_ratio_max",
            "edit_within_location", "char_count_title_meta", "url_200_and_quote_present", "grounding_url_subset",
            "tier_rules"}


@dataclass
class ValidationReport:
    parsed: Any = None
    errors: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _string_pairs(schema: dict) -> set[tuple[str, str]]:
    """Các cặp (trường chuỗi P, trường Q đứng ngay sau P trong `required`) ở mọi object của schema."""
    pairs: set[tuple[str, str]] = set()

    def walk(node):
        if isinstance(node, dict):
            props, req = node.get("properties"), node.get("required")
            if props and req:
                for a, b in zip(req, req[1:]):
                    t = props.get(a, {}).get("type")
                    if t == "string" or (isinstance(t, list) and "string" in t):
                        pairs.add((a, b))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(schema)
    return pairs


def escape_inner_quotes(text: str, schema: dict) -> str:
    """Web UI hay trả `"evidence":"trích "có" ngoặc","verdict":…`. Biết trường đứng sau (verdict) nên
    xác định được đâu là dấu " kết thúc thật, rồi escape các dấu " còn lại trong giá trị."""
    for p, q in _string_pairs(schema):
        pattern = re.compile(rf'("{re.escape(p)}"\s*:\s*")(.*?)("\s*,\s*"{re.escape(q)}"\s*:)', re.S)
        text = pattern.sub(lambda m: m.group(1) + re.sub(r'(?<!\\)"', r'\\"', m.group(2)) + m.group(3), text)
    return text


def _json_error(text: str, e: json.JSONDecodeError) -> str:
    near = text[max(0, e.pos - 40):e.pos + 40].replace("\n", " ")
    return (f"$ | JSON hợp lệ | {e.msg} ở vị trí {e.pos}, gần: …{near}… "
            f"(dấu \" nằm bên trong một chuỗi phải viết thành \\\"; hoặc bỏ dấu ngoặc kép khỏi câu trích)")


def validate_output(text: str, schema: dict | None, names: list[str], ctx: dict, lenient_json: bool = False) -> ValidationReport:
    """lenient_json: output từ web UI (không ép được cú pháp) — thử sửa JSON lỗi trước khi báo lỗi.
    JSON đã sửa vẫn phải qua schema và mọi validator, nên nội dung bị sửa sai sẽ bị bắt ở bước sau."""
    rep = ValidationReport()
    if schema is None:
        rep.parsed = text
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            repaired, how = None, None
            if lenient_json:
                try:
                    repaired, how = json.loads(escape_inner_quotes(text, schema)), "escape_inner_quotes"
                except json.JSONDecodeError:
                    import json_repair
                    repaired, how = json_repair.repair_json(text, return_objects=True), "json_repair"
            if not isinstance(repaired, dict):
                rep.errors.append(_json_error(text, e))
                return rep
            data = repaired
            rep.skipped.append(f"{how}: đã sửa JSON lỗi cú pháp ({e.msg} ở vị trí {e.pos})")
        data = _enum_casefix(schema, data)
        validator = jsonschema.Draft202012Validator(schema)
        for err in sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path)):
            path = "$" + "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in err.absolute_path)
            rep.errors.append(f"{path} | {err.validator} | {json.dumps(err.instance, ensure_ascii=False)[:80]}")
        rep.parsed = data
        if rep.errors:
            return rep
    for name in names:
        if name in POSTPROCESSORS or name in DEFERRED:
            continue
        fn = VALIDATORS.get(name)
        if fn is None:
            rep.skipped.append(f"{name}: chưa cài")
            continue
        try:
            if name in PRUNERS:
                rep.parsed, notes = PRUNERS[name](rep.parsed, ctx)
                rep.skipped += [f"{name}: {n}" for n in notes]
            rep.errors += fn(rep.parsed, ctx)
        except Missing as m:
            rep.skipped.append(f"{name}: thiếu ctx['{m}']")
    if rep.ok:
        for name in names:
            if name in POSTPROCESSORS:
                rep.parsed = POSTPROCESSORS[name](rep.parsed, ctx)
    return rep
