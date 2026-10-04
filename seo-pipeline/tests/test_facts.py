from vnseo.facts import knowledge_facts_to_claims, match_numbers, scan_numbers, uncovered_numbers


def kinds(text):
    return [(h.kind, h.key) for h in scan_numbers(text)]


def test_currency_and_multipliers():
    assert kinds("3 tỷ") == [("CUR", "num:3000000000")]
    assert kinds("$14") == [("CUR", "num:14")]
    assert kinds("1.000.000 đ") == [("CUR", "num:1000000")]
    assert kinds("giá 50k") == [("CUR", "num:50000")]


def test_no_false_currency():
    assert all(k != "CUR" for k, _ in kinds("5 trong số đó"))
    assert all(k != "CUR" for k, _ in kinds("5 đến 10 ngày"))
    assert kinds("P8a") == []


def test_pct_and_dates():
    assert kinds("2,5%") == [("PCT", "num:2.5")]
    assert kinds("15/09/2026") == [("DATE", "date:2026-09-15")]
    assert kinds("ngày 15 tháng 9 năm 2026") == [("DATE", "date:2026-09-15")]
    assert kinds("năm 2024") == [("YEAR", "num:2024")]


def test_ignore_rules_and_comments():
    md = "<!-- S01 -->\n1. Bước 2 trong top 10\n<!-- 99% -->\n```\n5%\n```\nNăm 2026 tăng"
    hits = scan_numbers(md, ignore_patterns=[r"^\s*\d+\.", r"Bước \d+", r"top \d+"], ignore_years=[2026])
    assert hits == []


def test_match_uses_new_value_and_override():
    claims = [
        {"claim_id": "C001", "verbatim": "phí 0,5%", "new_value": "0,6%", "status": "DA_DUYET", "action": "CAP_NHAT"},
        {"claim_id": "C002", "verbatim": "3 tỷ đồng", "new_value": None, "status": "DA_DUYET", "action": "GIU"},
        {"claim_id": "C003", "verbatim": "12%", "new_value": None, "status": "BO", "action": "BO"},
    ]
    ok = match_numbers(scan_numbers("Phí 0,6% cho khoản 3.000.000.000 đồng"), claims)
    assert not ok.override_fail and len(ok.matched) == 2
    bad = match_numbers(scan_numbers("Phí 0,5%, lãi 12%"), claims)
    assert bad.override_fail and {h.raw for h in bad.unmatched} == {"0,5%", "12%"}


def test_knowledge_facts_match_numbers():
    claims = knowledge_facts_to_claims("K0042", [{"label": "gói cũ", "value": "120", "unit": "k", "as_of": "2026-03"}], 50)
    assert claims[0]["claim_id"] == "C050"
    assert not match_numbers(scan_numbers("Mình trả 120k mỗi tháng"), claims).override_fail


def test_uncovered_numbers_in_knowledge_entry():
    content = "Tháng 3/2026 mình chuyển từ gói 120k sang 90k."
    assert uncovered_numbers(content, [{"value": "120", "unit": "k"}]) == ["90k"]
