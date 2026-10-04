from datetime import date

import pytest

from knowledge.repository import (
    EntryInput, KnowledgeValidationError, build_evidence, commit_usage, expire_entries, import_entry,
    release_usage, retrieve,
)
from pipeline import queue

TODAY = date(2026, 9, 28)
W = {"tag": 0.4, "fulltext": 0.3, "vector": 0.3}


def entry(author="a1", **kw):
    base = dict(author_id=author, kind="trai_nghiem", title="Đổi gói cước điện thoại",
                content="Tháng 3/2026 mình chuyển từ gói 120k sang 90k, gọi 0912 345 678 để đăng ký.",
                topic_tags=["gói cước", "tiết kiệm"], valid_until=date(2027, 1, 1),
                facts=[{"label": "gói cũ", "value": "120", "unit": "k", "as_of": "2026-03"},
                       {"label": "gói mới", "value": "90", "unit": "k", "as_of": "2026-03"}])
    base.update(kw)
    return EntryInput(**base)


def job(conn, author="a1"):
    return queue.create_job(conn, topic="t", author_id=author, created_by="x", cost_cap_usd=3)


def test_import_redacts_and_validates(conn):
    r = import_entry(conn, entry(), today=TODAY)
    row = conn.execute("SELECT * FROM author_knowledge WHERE id = %s", (r.entry_id,)).fetchone()
    assert r.pii_redacted == 1 and "[SĐT]" in row["content"] and row["pii_checked"]
    assert row["topic_tags"] == ["goi cuoc", "tiet kiem"]
    with pytest.raises(KnowledgeValidationError, match="chưa khai"):
        import_entry(conn, entry(facts=[{"label": "gói cũ", "value": "120", "unit": "k", "as_of": "2026-03"}]), today=TODAY)
    with pytest.raises(KnowledgeValidationError, match="valid_until"):
        import_entry(conn, entry(valid_until=date(2020, 1, 1)), today=TODAY)


def test_retrieve_filters_and_ranks(conn):
    good = import_entry(conn, entry(author="a2"), today=TODAY).entry_id
    other = import_entry(conn, entry(author="a2", title="Nấu phở bò", content="Nấu nước dùng 8 tiếng.",
                                     topic_tags=["nấu ăn"], facts=[]), today=TODAY).entry_id
    import_entry(conn, entry(author="someone_else"), today=TODAY)
    expired = import_entry(conn, entry(author="a2", valid_until=date(2026, 10, 1)), today=TODAY).entry_id
    j = job(conn, "a2")
    igs = [{"ig_id": "IG01", "description": "Chi phí thực tế khi đổi gói cước", "inputs_needed": "trải nghiệm tiết kiệm"}]
    res = retrieve(conn, j, "a2", igs, W, today=date(2026, 12, 1))
    ids = [c.entry_id for c in res["IG01"]]
    assert ids[0] == good and other not in ids and expired not in ids


def test_evidence_reservation_lifecycle(conn):
    eid = import_entry(conn, entry(author="a3"), today=TODAY).entry_id
    j1, j2 = job(conn, "a3"), job(conn, "a3")
    igs = [{"ig_id": "IG01", "description": "đổi gói cước tiết kiệm", "inputs_needed": ""}]
    sel = {"selections": [{"ig_id": "IG01", "entry_id": eid, "excerpt": "chuyển từ gói 120k sang 90k",
                           "fit_reason": "", "confidence": "cao"}], "unfilled_ig_ids": []}

    evidence, claims = build_evidence(conn, j1, sel, next_claim_index=10)
    assert evidence["items"][0]["content"].startswith("Tháng 3/2026")
    assert [c["claim_id"] for c in claims] == ["C010", "C011"] and claims[0]["origin"] == "kho_tac_gia"
    build_evidence(conn, j1, sel, next_claim_index=10)  # P5 chạy lại: không nhân đôi reservation

    # max_reuse = 1: reservation của j1 chặn j2
    assert retrieve(conn, j2, "a3", igs, W, max_reuse=1, today=TODAY)["IG01"] == []
    assert commit_usage(conn, j1) == 1
    assert conn.execute("SELECT usage_count FROM author_knowledge WHERE id = %s", (eid,)).fetchone()["usage_count"] == 1
    assert retrieve(conn, j2, "a3", igs, W, max_reuse=2, today=TODAY)["IG01"][0].entry_id == eid
    build_evidence(conn, j2, sel, next_claim_index=1)
    assert release_usage(conn, j2) == 1


def test_evidence_rejects_bad_selection(conn):
    eid = import_entry(conn, entry(author="a4"), today=TODAY).entry_id
    j = job(conn, "a4")
    bad_excerpt = {"selections": [{"ig_id": "IG01", "entry_id": eid, "excerpt": "câu bịa", "fit_reason": "",
                                   "confidence": "cao"}]}
    with pytest.raises(KnowledgeValidationError, match="chuỗi con"):
        build_evidence(conn, j, bad_excerpt, 1)
    dup = {"selections": [{"ig_id": i, "entry_id": eid, "excerpt": "gói", "fit_reason": "", "confidence": "cao"}
                          for i in ("IG01", "IG02")]}
    with pytest.raises(KnowledgeValidationError, match="nhiều IG"):
        build_evidence(conn, j, dup, 1)


def test_expire(conn):
    eid = import_entry(conn, entry(author="a5", valid_until=date(2026, 10, 1)), today=TODAY).entry_id
    assert eid in expire_entries(conn, today=date(2026, 10, 2))
