import pytest

from vnseo.vn_text import decode, normalize, split_sentences, strip_diacritics, syllables


@pytest.mark.parametrize("raw,expected", [
    ("hòa", "hoà"),
    ("thủy", "thuỷ"),
    ("Đà  Nẵng​", "Đà Nẵng"),
    ("khỏe mạnh", "khoẻ mạnh"),
    ("hoàng", "hoàng"),      # có phụ âm cuối: giữ nguyên
    ("quý", "quý"),          # qu là phụ âm đầu
    ("hoài", "hoài"),        # vần oai: không đổi
    ("Hòa Bình", "Hoà Bình"),
])
def test_normalize_new_style(raw, expected):
    assert normalize(raw) == expected


def test_normalize_old_style():
    assert normalize("hoà thuỷ", tone_style="old") == "hòa thủy"


def test_normalize_keep_newlines():
    assert normalize("a  b\n\nc­", keep_newlines=True) == "a b\n\nc"


def test_strip_diacritics():
    assert strip_diacritics("Đường hoà") == "duong hoa"


def test_decode_cp1258_and_utf8():
    assert decode("hoà".encode("utf-8"))[1] == "utf8"
    text, enc, review = decode("đi hoà".encode("cp1258"))
    assert enc == "cp1258" and text == "đi hoà" and not review
    assert decode("café".encode("cp1258"))[2]  # không có chữ đặc trưng tiếng Việt → cần người xem


def test_split_sentences_basic_and_abbrev():
    md = "<!-- S01 -->\n## Tiêu đề\nTôi sống ở TP. HCM đã lâu. Lãi suất 2,5% là cao. ốc sên chậm.\n- mục một\n```\nNot. A sentence.\n```"
    ss = split_sentences(md)
    texts = [s.text for s in ss]
    assert texts == ["## Tiêu đề", "Tôi sống ở TP. HCM đã lâu.", "Lãi suất 2,5% là cao. ốc sên chậm.", "- mục một"]
    assert {s.section_id for s in ss} == {"S01"}
    assert [s.kind for s in ss] == ["heading", "text", "text", "bullet"]
    assert md[ss[1].start:ss[1].end] == ss[1].text


def test_syllables_number_normalization():
    assert syllables("Lãi suất tăng 0,5%") == syllables("lãi suất tăng 0,5 %") == ["lãi", "suất", "tăng", "0.5"]
    assert syllables("1.000.000 đồng") == ["1000000", "đồng"]
