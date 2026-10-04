"""Luật cứng chạy trước AI gate (§5.13). Fail → revise nếu còn lượt, không thì escalate; không gọi AI.

Bổ sung so với spec:
- FACT: code tự chuyển CAP_NHAT thiếu new_value sang XAC_MINH và tự set status khi gate approve
  (v5 bỏ người nên không còn ai điền), xem approve_fact_inventory().
- OUTLINE: new_claims phải có ig_id để biết IG nào có dữ liệu thật.
- FACT_SIGNOFF: phân loại claim thành apply / human / remove (claim không chặn chưa xác minh được thì xoá câu).
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable

from vnseo import jsonld
from vnseo.seo import char_count, pixel_width
from vnseo.vn_text.markdown import SECTION_MARKER
from vnseo.vn_text.normalize import strip_diacritics

PLACEHOLDER = re.compile(r"\[CẦN NGƯỜI:[^\]]*\]")
CLAIM_ID = re.compile(r"^C\d{3}$")


@dataclass
class RuleResult:
    passed: bool
    failures: list[str] = field(default_factory=list)
    force_escalate: bool = False  # escalate kể cả khi còn lượt revise (vd. YMYL ở FINAL)
    data: dict = field(default_factory=dict)

    @classmethod
    def of(cls, failures: list[str], **kw) -> "RuleResult":
        return cls(passed=not failures, failures=failures, **kw)


# ---------- KW ----------

def kw_rules(input_rows: list[dict], keyword_map: dict) -> RuleResult:
    """recall 100% dòng đầu vào; đúng 1 primary; volume y hệt đầu vào."""
    failures = []
    rows = keyword_map["rows"]
    want = Counter(r["keyword_nfc"] for r in input_rows)
    got = Counter(r["keyword_nfc"] for r in rows)
    if missing := want - got:
        failures.append(f"thiếu dòng: {sorted(missing)[:10]}")
    if extra := got - want:
        failures.append(f"thừa dòng: {sorted(extra)[:10]}")
    primaries = [r["keyword_nfc"] for r in rows if r["role"] == "primary"]
    if len(primaries) != 1:
        failures.append(f"cần đúng 1 primary, có {len(primaries)}")
    volume_in = {r["keyword_nfc"]: r.get("volume") for r in input_rows}
    changed = [r["keyword_nfc"] for r in rows if r["keyword_nfc"] in volume_in and r["volume"] != volume_in[r["keyword_nfc"]]]
    if changed:
        failures.append(f"volume bị đổi: {changed[:10]}")
    return RuleResult.of(failures)


# ---------- FACT ----------

def normalize_fact_actions(inventory: dict) -> dict:
    """CAP_NHAT không có new_value → XAC_MINH (sẽ được P10 kiểm chứng)."""
    claims = []
    for c in inventory["claims"]:
        if c["action"] == "CAP_NHAT" and not c.get("new_value"):
            c = {**c, "action": "XAC_MINH"}
        claims.append(c)
    return {**inventory, "claims": claims}


def fact_rules(inventory: dict) -> RuleResult:
    failures = []
    ids = [c["claim_id"] for c in inventory["claims"]]
    if bad := [i for i in ids if not CLAIM_ID.match(i)]:
        failures.append(f"claim_id sai định dạng: {bad[:10]}")
    if dup := [i for i, n in Counter(ids).items() if n > 1]:
        failures.append(f"claim_id trùng: {dup[:10]}")
    if bad := [c["claim_id"] for c in inventory["claims"] if c["action"] == "CAP_NHAT" and not c.get("new_value")]:
        failures.append(f"CAP_NHAT thiếu new_value: {bad[:10]}")
    return RuleResult.of(failures)


def approve_fact_inventory(inventory: dict) -> dict:
    """Gọi sau khi FACT_GATE approve: BO → BO, còn lại → DA_DUYET (XAC_MINH sẽ được P10 kiểm lại sau khi viết)."""
    claims = [{**c, "status": "BO" if c["action"] == "BO" else "DA_DUYET"} for c in inventory["claims"]]
    return {**inventory, "claims": claims}


# ---------- OUTLINE ----------

def outline_rules(plan: dict, selection: dict, verified_new_claim_ig_ids: Iterable[str],
                  approved_fact_ids: Iterable[str], ig_min: int) -> RuleResult:
    """≥ig_min IG có dữ liệu (mục kho được chọn hoặc new_claim đã DUNG); mọi fact_ids/ig_ids tồn tại;
    outline không dùng IG mà toàn bộ new_claims của nó bị fact-check loại (số liệu có thể do model bịa)."""
    failures = []
    ig_ids = {ig["ig_id"] for ig in plan["ig"]}
    with_kb = {s["ig_id"] for s in selection["selections"]}
    verified = set(verified_new_claim_ig_ids)
    backed = (with_kb | verified) & ig_ids
    if len(backed) < ig_min:
        failures.append(f"chỉ {len(backed)} IG có dữ liệu thật, cần ≥{ig_min}")
    claimed = {c["ig_id"] for c in plan.get("new_claims", []) if c.get("ig_id")}
    unverified = (claimed - verified - with_kb) & ig_ids
    for sec in plan["outline"]:
        if bad := sorted(set(sec["ig_ids"]) & unverified):
            failures.append(f"{sec['section_id']} dùng {bad}: mọi số liệu mới của IG này đều không kiểm chứng được "
                            f"— bỏ IG/section đó hoặc thay bằng IG có dữ liệu (kho tác giả, fact đã duyệt)")
    facts = set(approved_fact_ids)
    for sec in plan["outline"]:
        if bad := [f for f in sec["fact_ids"] if f not in facts]:
            failures.append(f"{sec['section_id']}: fact_ids không tồn tại {bad}")
        if bad := [i for i in sec["ig_ids"] if i not in ig_ids]:
            failures.append(f"{sec['section_id']}: ig_ids không tồn tại {bad}")
    return RuleResult.of(failures, data={"backed_ig_ids": sorted(backed)})


# ---------- TITLE (chỉ code, không gọi AI) ----------

def title_rules(meta: dict, primary_keyword: str, existing_titles: Iterable[str], th: dict) -> RuleResult:
    """Chọn title/meta tốt nhất. Không có title đạt → escalate."""
    t_min, t_max = th["title_chars"]
    m_min, m_max = th["meta_chars"]
    primary = strip_diacritics(primary_keyword)
    taken = {strip_diacritics(t) for t in existing_titles}

    candidates = []
    for t in meta["titles"]:
        key = strip_diacritics(t)
        if primary not in key or key in taken:
            continue
        n = char_count(t)
        score = (2 if t_min <= n <= t_max else 0) + (1 if pixel_width(t) <= th["title_px_warn"] else 0)
        if score >= 2:
            candidates.append((score, -abs(n - (t_min + t_max) / 2), t))
    metas = [m for m in meta["metas"] if m_min <= char_count(m) <= m_max]

    failures = []
    if not candidates:
        failures.append("không có title đạt độ dài, chứa keyword primary và không trùng title khác")
    if not metas:
        failures.append("không có meta đạt độ dài")
    data = {}
    if candidates:
        data["title"] = max(candidates)[2]
    if metas:
        data["meta"] = metas[0]
    return RuleResult.of(failures, force_escalate=bool(failures), data=data)


# ---------- QA ----------

def qa_rules(rubric: dict, edit_ratio: float, max_edit_ratio: float) -> RuleResult:
    failures = []
    if not rubric["pass"]:
        failures.append(f"rubric trượt ({rubric['total']}/{rubric['max_total']}, blocking=0: {rubric['blocking_zero']})")
    if edit_ratio > max_edit_ratio:
        failures.append(f"tỷ lệ câu bị sửa {edit_ratio:.3f} > {max_edit_ratio}")
    return RuleResult.of(failures)


# ---------- FACT_SIGNOFF ----------

def _is_blocking(claim: dict, blocking_types: set[str], ymyl: bool) -> bool:
    return ymyl or claim.get("type") in blocking_types


def fact_signoff_rules(
    factcheck: dict,
    claims_by_id: dict[str, dict],
    url_ok: dict[tuple[str, str], bool],
    blocking_types: Iterable[str],
    ymyl: bool,
    apply_min_tier: int = 2,
) -> RuleResult:
    """url_ok[(url, quote)] = URL trả 200 và chứa quote (substring hoặc fuzzy ≥0,9).

    data: apply = claim SAI/CAN_CAP_NHAT có nguồn đủ tin → FACT_APPLY sửa giá trị
          remove = claim không chặn mà không xác minh được → FACT_APPLY xoá câu
          human  = claim chặn MAU_THUAN / KHONG_XAC_MINH_DUOC / sai mà thiếu nguồn tốt → escalate
    """
    blocking_types = set(blocking_types)
    failures, apply, remove, human = [], [], [], []
    checked = {it["claim_id"] for it in factcheck["items"]}

    for cid, claim in claims_by_id.items():
        if claim.get("origin") == "kho_tac_gia" or claim.get("status") != "DA_DUYET":
            continue
        if cid not in checked and _is_blocking(claim, blocking_types, ymyl):
            failures.append(f"{cid}: claim chặn chưa có verdict")

    for it in factcheck["items"]:
        cid = it["claim_id"]
        claim = claims_by_id.get(cid, {})
        if claim.get("origin") == "kho_tac_gia":
            continue
        bad_urls = [s["url"] for s in it["sources"] if not url_ok.get((s["url"], s["quote"]), False)]
        if bad_urls:
            failures.append(f"{cid}: URL chết hoặc không chứa quote {bad_urls}")
        verdict = it["verdict"]
        good_source = any(s["tier"] <= apply_min_tier for s in it["sources"])
        blocking = _is_blocking(claim, blocking_types, ymyl)
        if verdict == "DUNG":
            continue
        if verdict in ("SAI", "CAN_CAP_NHAT") and good_source and not bad_urls:
            apply.append(cid)
        elif blocking:
            human.append(cid)
        else:
            remove.append(cid)

    if human:
        failures.append(f"claim chặn cần người: {human}")
    return RuleResult.of(failures, force_escalate=bool(human),
                         data={"apply": apply, "remove": remove, "human": human})


def fact_apply_items(factcheck: dict, apply: list[str], remove: list[str], claims_by_id: dict[str, dict],
                     section_of_claim: dict[str, str]) -> list[dict]:
    """Dựng lỗi dạng Verdict item cho P9 (check_id = P10.<claim_id>)."""
    by_id = {it["claim_id"]: it for it in factcheck["items"]}
    items = []
    for cid in apply + remove:
        if cid not in by_id or cid not in claims_by_id:  # model trả claim_id lạ: không có gì để sửa, không để bước đổ vỡ
            continue
        it = by_id[cid]
        src = it["sources"][0]["url"] if it["sources"] else ""
        hint = (f"thay bằng {it['current_value']} (nguồn: {src})" if cid in apply and it["current_value"]
                else "xoá câu chứa claim này")
        items.append({
            "check_id": f"P10.{cid}",
            "evidence": claims_by_id[cid]["verbatim"],
            "verdict": "NO",
            "severity": "chan",
            "fix_hint": hint,
            "location": section_of_claim.get(cid, "GLOBAL"),
        })
    return items


def update_inventory_after_factcheck(inventory: dict, factcheck: dict, apply: list[str], remove: list[str]) -> dict:
    """Phải chạy TRƯỚC FACT_RECHECK: ghi current_value vào new_value để number_scan khớp số mới."""
    by_id = {it["claim_id"]: it for it in factcheck["items"]}
    claims = []
    for c in inventory["claims"]:
        cid = c["claim_id"]
        if cid in apply and by_id[cid]["current_value"]:
            src = by_id[cid]["sources"][0]["url"] if by_id[cid]["sources"] else None
            c = {**c, "action": "CAP_NHAT", "new_value": by_id[cid]["current_value"], "verification_source": src}
        elif cid in remove or (cid in apply and not by_id[cid]["current_value"]):
            c = {**c, "action": "BO", "status": "BO"}
        claims.append(c)
    return {**inventory, "claims": claims}


# ---------- FINAL ----------

def final_rules(package: dict, branch: str, containment: float, th: dict, ymyl: bool, ymyl_final_human: bool) -> RuleResult:
    failures = []
    article = package["article_markdown"]
    placeholders = len(PLACEHOLDER.findall(article))
    if package["open_placeholders"] != 0 or placeholders:
        failures.append(f"còn {max(placeholders, package['open_placeholders'])} placeholder [CẦN NGƯỜI]")
    if SECTION_MARKER.search(article):
        failures.append("còn marker <!-- Sxx -->")
    if errs := jsonld.validate(package["jsonld"] if isinstance(package["jsonld"], list) else [package["jsonld"]]):
        failures.append(f"JSON-LD: {errs}")
    if branch == "B" and containment >= th["branch_b_containment_max"]:
        failures.append(f"containment {containment:.3f} ≥ ngưỡng")
    force = bool(ymyl and ymyl_final_human)
    if force:
        failures.append("YMYL: luôn cần người duyệt cuối")
    return RuleResult.of(failures, force_escalate=force or bool(failures))
