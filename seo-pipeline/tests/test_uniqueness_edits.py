from vnseo.edits import Edit, apply_edits, changed_sentence_ratio
from vnseo.uniqueness import containment, containment_hashes, pack_hashes, shingle_hashes, unpack_hashes


def test_containment_fixtures():
    a = "Hôm nay trời đẹp và mình đi dạo ở công viên gần nhà"
    assert containment(a, a).ratio == 1.0
    assert containment(a, "Một câu hoàn toàn khác không có chữ nào giống cả đâu").ratio == 0.0
    r = containment("Lãi suất tăng 0,5%", "lãi suất tăng 0,5 %")
    assert r.ratio == 1.0 and r.n == 4
    assert containment("", "abc").ratio == 0.0


def test_containment_from_stored_hashes():
    src = "một hai ba bốn năm sáu bảy tám chín mười"
    blob = pack_hashes(shingle_hashes(src))
    new = "<!-- S01 -->\nmột hai ba bốn năm sáu khác hẳn luôn nhé bạn"
    r = containment_hashes(new, unpack_hashes(blob))
    assert 0 < r.ratio < 1 and r.top_passages[0] == "một hai ba bốn năm sáu"
    assert "S01" in r.by_section


def test_changed_ratio():
    old = [f"câu {i}" for i in range(60)]
    assert changed_sentence_ratio(old, old) == 0
    new = old.copy()
    new[5] = "khác"
    assert changed_sentence_ratio(old, new) == 1 / 60
    new[6], new[7] = "x", "y"
    assert changed_sentence_ratio(old, new) == 3 / 60


MD = "<!-- S01 -->\n## A\nCâu một ở đây. Câu hai lặp lặp.\n<!-- S02 -->\n## B\nCâu ba ở đây."


def test_apply_edits_rules():
    edits = [
        Edit("P9.1", 0, "lặp lặp", "lặp"),
        Edit("P9.2", 0, "ở đây", "x"),            # xuất hiện 2 lần → loại
        Edit("P9.3", 0, "Câu ba", "Câu 3"),       # ngoài location S01 → loại
        Edit("P9.4", 0, "<!-- S02 -->", ""),      # chạm marker → loại
    ]
    locs = {"P9.1": "S01", "P9.2": "GLOBAL", "P9.3": "S01", "P9.4": "GLOBAL"}
    r = apply_edits(MD, edits, locs)
    assert [e.check_id for e in r.applied] == ["P9.1"]
    reasons = dict((e.check_id, why) for e, why in r.rejected)
    assert reasons["P9.2"].startswith("find_count=2")
    assert reasons["P9.3"].startswith("outside_location")
    assert reasons["P9.4"] == "touches_section_marker"
    assert "lặp lặp" not in r.md and r.diff


def test_apply_edits_ratio_cap_rejects_batch():
    r = apply_edits(MD, [Edit("P7b.1", 1, "Câu một", "Câu 1")], max_ratio=0.05)
    assert r.batch_rejected and r.md == MD
