from gates.rules import (
    approve_fact_inventory, fact_apply_items, fact_rules, fact_signoff_rules, final_rules, kw_rules,
    normalize_fact_actions, outline_rules, qa_rules, title_rules, update_inventory_after_factcheck,
)
from pipeline.config import load_config
from vnseo.facts import match_numbers, scan_numbers

TH = load_config()["thresholds"]


def row(k, role="secondary", volume=10):
    return {"keyword_nfc": k, "display": k, "key_khongdau": k, "intent": "thong_tin", "cluster": "c",
            "role": role, "section": "", "volume": volume, "note": ""}


def test_kw_rules():
    inp = [row("a", volume=100), row("b")]
    assert kw_rules(inp, {"rows": [row("a", "primary", 100), row("b")]}).passed
    r = kw_rules(inp, {"rows": [row("a", "primary", 999)]})
    assert not r.passed and len(r.failures) == 2
    assert not kw_rules(inp, {"rows": [row("a", volume=100), row("b")]}).passed


def claim(cid, action="GIU", new_value=None, type_="so_lieu", verbatim="phí 0,5%", status="CHUA_DUYET", origin="bai_goc"):
    return {"claim_id": cid, "verbatim": verbatim, "type": type_, "in_article_source": "", "maybe_outdated": False,
            "outdated_reason": "", "action": action, "new_value": new_value, "verification_source": None,
            "status": status, "origin": origin}


def test_fact_flow():
    inv = {"claims": [claim("C001", "CAP_NHAT"), claim("C002", "BO")]}
    assert not fact_rules(inv).passed
    inv = normalize_fact_actions(inv)
    assert inv["claims"][0]["action"] == "XAC_MINH" and fact_rules(inv).passed
    done = approve_fact_inventory(inv)
    assert [c["status"] for c in done["claims"]] == ["DA_DUYET", "BO"]


def test_outline_rules():
    plan = {"ig": [{"ig_id": "IG01"}, {"ig_id": "IG02"}],
            "outline": [{"section_id": "S01", "fact_ids": ["C001"], "ig_ids": ["IG01"]}]}
    sel = {"selections": [{"ig_id": "IG01"}], "unfilled_ig_ids": []}
    assert not outline_rules(plan, sel, [], ["C001"], 2).passed
    assert outline_rules(plan, sel, ["IG02"], ["C001"], 2).passed
    assert not outline_rules(plan, sel, ["IG02"], [], 2).passed


def test_title_rules_picks_valid():
    meta = {"titles": ["Vay tiền online", "Vay tiền online an toàn: 7 cách kiểm tra app trước khi vay",
                       "Cách chọn app uy tín cho người mới bắt đầu tìm hiểu tài chính", "x", "y"],
            "metas": ["ngắn", "Hướng dẫn kiểm tra app vay tiền online an toàn: giấy phép, lãi suất, phí ẩn và "
                              "các dấu hiệu lừa đảo cần tránh trước khi ký hợp đồng."]}
    r = title_rules(meta, "vay tiền online", [], TH)
    assert r.passed and r.data["title"].startswith("Vay tiền online an toàn")
    r = title_rules(meta, "vay tiền online", ["Vay tiền online an toàn: 7 cách kiểm tra app trước khi vay"], TH)
    assert not r.passed and r.force_escalate


def test_qa_rules():
    assert qa_rules({"pass": True, "total": 18, "max_total": 18, "blocking_zero": []}, 0.02, 0.05).passed
    assert not qa_rules({"pass": True, "total": 18, "max_total": 18, "blocking_zero": []}, 0.08, 0.05).passed


def fc(cid, verdict, current="", tier=1, url="https://a.gov.vn/x"):
    sources = [] if verdict == "KHONG_XAC_MINH_DUOC" else [{"url": url, "date": "", "quote": "q", "tier": tier}]
    return {"claim_id": cid, "atomic_claim": "", "verdict": verdict, "current_value": current,
            "sources": sources, "conflict_note": "", "suggested_action": ""}


def test_fact_signoff_classification_and_apply_roundtrip():
    claims = {
        "C001": claim("C001", status="DA_DUYET"),
        "C002": claim("C002", status="DA_DUYET", type_="khuyen_nghi", verbatim="nên gửi dài hạn"),
        "C003": claim("C003", status="DA_DUYET", verbatim="12%"),
        "C004": claim("C004", status="DA_DUYET", origin="kho_tac_gia", verbatim="120 k"),
    }
    factcheck = {"items": [fc("C001", "CAN_CAP_NHAT", "0,6%"), fc("C002", "KHONG_XAC_MINH_DUOC"), fc("C003", "DUNG")]}
    ok = {("https://a.gov.vn/x", "q"): True}
    r = fact_signoff_rules(factcheck, claims, ok, TH["blocking_claim_types"], ymyl=False)
    assert r.passed and r.data == {"apply": ["C001"], "remove": ["C002"], "human": []}

    items = fact_apply_items(factcheck, r.data["apply"], r.data["remove"], claims, {"C001": "S02"})
    assert items[0]["check_id"] == "P10.C001" and "0,6%" in items[0]["fix_hint"] and items[0]["location"] == "S02"

    inv = update_inventory_after_factcheck({"claims": list(claims.values())}, factcheck, r.data["apply"], r.data["remove"])
    # Sau khi P9 sửa bài thành 0,6%, number_scan phải khớp (lỗi #1 của spec)
    assert not match_numbers(scan_numbers("Phí hiện là 0,6%, lãi 12%"), inv["claims"]).override_fail


def test_fact_signoff_blocking_unverified_escalates():
    claims = {"C001": claim("C001", status="DA_DUYET")}
    r = fact_signoff_rules({"items": [fc("C001", "MAU_THUAN")]}, claims, {("https://a.gov.vn/x", "q"): True},
                           TH["blocking_claim_types"], ymyl=False)
    assert r.force_escalate and r.data["human"] == ["C001"]
    r = fact_signoff_rules({"items": [fc("C001", "DUNG")]}, claims, {}, TH["blocking_claim_types"], ymyl=False)
    assert not r.passed  # URL không kiểm được


def test_final_rules():
    pkg = {"article_markdown": "## A\nNội dung.", "open_placeholders": 0,
           "jsonld": [{"@context": "https://schema.org", "@type": "Article", "headline": "H",
                       "author": {"@type": "Person", "name": "An"}}]}
    assert final_rules(pkg, "A", 0.0, TH, ymyl=False, ymyl_final_human=True).passed
    assert final_rules(pkg, "A", 0.0, TH, ymyl=True, ymyl_final_human=True).force_escalate
    bad = {**pkg, "article_markdown": "<!-- S01 -->\n[CẦN NGƯỜI: số liệu]"}
    assert len(final_rules(bad, "B", 0.2, TH, ymyl=False, ymyl_final_human=True).failures) == 3


def test_outline_rejects_ig_with_only_unverified_new_claims():
    """Gặp khi chạy thật: Gemini đề xuất 'Thông tư 29/2026' cho IG03, fact-check không tìm được nguồn."""
    plan = {"ig": [{"ig_id": "IG01"}, {"ig_id": "IG02"}, {"ig_id": "IG03"}],
            "new_claims": [{"ig_id": "IG03", "verbatim": "x", "source_url": "https://a", "section_id": "S02", "type": "luat"}],
            "outline": [{"section_id": "S01", "fact_ids": [], "ig_ids": ["IG01"]},
                        {"section_id": "S02", "fact_ids": [], "ig_ids": ["IG03"]}]}
    sel = {"selections": [{"ig_id": "IG01"}, {"ig_id": "IG02"}]}
    r = outline_rules(plan, sel, [], [], 2)
    assert not r.passed and "S02 dùng ['IG03']" in r.failures[0]
    assert outline_rules(plan, sel, ["IG03"], [], 2).passed  # claim của IG03 đã kiểm chứng
