from pipeline.config import load_config
from vnseo import jsonld
from vnseo.pii import redact
from vnseo.seo import char_count, slug_is_valid, slugify
from vnseo.style import blacklist_scan, sentence_stats

CFG = load_config()["blacklist"]


def test_blacklist_high_phrase_and_quote_skip():
    md = "Tóm lại, đây là bài.\n> Tóm lại trong trích dẫn\nAnh ấy nói “Tóm lại” thôi."
    hits = blacklist_scan(md, CFG)
    assert [h.rule for h in hits] == ["Tóm lại"]


def test_blacklist_patterns_and_openers():
    md = ("Nó không chỉ rẻ mà còn bền. Nó không chỉ nhẹ mà còn đẹp.\n"
          "Ngoài ra, a. Bên cạnh đó, b. Hơn nữa, c.")
    rules = {h.rule for h in blacklist_scan(md, CFG)}
    assert "không chỉ .{1,60}? mà còn" in rules
    assert "sentence_opener" in rules


def test_blacklist_under_limit():
    assert blacklist_scan("Nó không chỉ rẻ mà còn bền. Ngoài ra, a.", CFG) == []


def test_sentence_stats():
    st = sentence_stats("Một hai ba. Một hai ba. Một hai ba.")
    assert st.count == 3 and st.cv == 0


def test_pii_does_not_eat_money():
    text, found = redact("Doanh thu 10.000.000.000 đồng, 100.000.000.000 đồng")
    assert found == [] and "10.000.000.000" in text


def test_pii_phone_email_cccd():
    text, found = redact("Gọi 0912 345 678 hoặc +84 912345678, mail a.b@x.vn, CCCD 001204012345")
    assert [f.kind for f in found] == ["SĐT", "SĐT", "EMAIL", "CCCD"]
    assert "0912" not in text and "[EMAIL]" in text


def test_char_count_graphemes_and_slug():
    assert char_count("hòa") == 3
    assert slugify("Vay tiền online có an toàn không năm 2026") == "vay-tien-online-co-an-toan"
    assert slug_is_valid("vay-tien-online") and not slug_is_valid("Vay_tien")


def test_jsonld_drops_null_and_validates():
    blocks = jsonld.build({"headline": "H", "author_name": "An", "author_url": None,
                           "date_published": None, "date_modified": None, "breadcrumb": []})
    assert blocks[0]["author"] == {"@type": "Person", "name": "An"}
    assert "datePublished" not in blocks[0]
    assert jsonld.validate(blocks) == []
    assert "Article thiếu author" in jsonld.validate(jsonld.build({"headline": "H", "breadcrumb": []}))


import pytest


@pytest.mark.parametrize("text,expected", [
    ("(+84) 912 345 678", "[SĐT]"), ("(+84)912345678", "[SĐT]"), ("84 912 345 678", "[SĐT]"), ("0084912345678", "[SĐT]"),
    ("SĐT:0912345678,0987654321", "SĐT:[SĐT],[SĐT]"), ("Hotline.0912345678", "Hotline.[SĐT]"),
    ("0912 - 345 - 678", "[SĐT]"), ("0912  345  678", "[SĐT]"), ("0912.345.678", "[SĐT]"), ("091 234 56 78", "[SĐT]"),
    ("(028) 3822 1234", "[SĐT]"), ("0283 822 1234", "[SĐT]"),
    ("CCCD 001 204 012 345", "[CCCD]"), ("CMND 123456789", "[CCCD]"), ("STK Vietcombank 0011004123456", "[STK]"),
    # không nuốt số đứng cạnh
    ("Gọi 0912 345 678 2 lần mỗi ngày", "Gọi [SĐT] 2 lần mỗi ngày"), ("Zalo 0912345678 9h sáng", "Zalo [SĐT] 9h sáng"),
])
def test_pii_phone_and_id_formats(text, expected):
    assert redact(text)[0] == expected


@pytest.mark.parametrize("text", [
    "hệ số 0.123456789", "Giảm 0 5 10 15 20 25 30 phần trăm", "ngày 01.02.2026 5 6 lần", "năm 2026 có 84 trận",
    "tài khoản của bạn có 1.000.000 đồng", "1,000,000,000 đồng", "giá 0912345 đồng", "tỷ số 2-0, 3-1",
    "Doanh thu 10.000.000.000 đồng, 100.000.000.000 đồng",
])
def test_pii_does_not_touch_ordinary_numbers(text):
    assert redact(text) == (text, [])
