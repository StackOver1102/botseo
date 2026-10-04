"""vnseo.purge: xoá câu / dòng bảng còn số chưa kiểm chứng, bỏ bảng rỗng kèm câu dẫn."""
from vnseo.facts import OVERRIDE_KINDS, scan_numbers
from vnseo.purge import purge_unverified

ARTICLE = """<!-- S05 -->
### Sức hút của Ngoại hạng Anh

Riêng khu vực châu Âu hiện có nhiều giải đấu lớn nhỏ. Nhìn vào bảng thống kê lượt theo dõi, bức tranh tổng thể trở nên rõ ràng hơn:

| Giải đấu | Lượt theo dõi (triệu) | Tỷ lệ quan tâm |
|---|---|---|
| Premier League | 19,2 | 42% |
| Champions League | 15,6 | 35% |

Dễ thấy Premier League là cái tên có sức hút vượt trội.

<!-- S06 -->
### Nguồn dữ liệu

Dữ liệu đến từ nhiều nơi. Khoảng 65% đến từ API chính thức với độ chính xác 99,9%. Phần còn lại do đội ngũ nhập tay.

- Đối tác liên đoàn: 25%
- Đội ngũ tại sân: cập nhật trực tiếp

| Nguồn | Tỷ trọng |
|---|---|
| API | 65% |
| Báo chí | đã kiểm chứng |
"""


def bad_spans(md, keep=()):
    return [(h.start, h.end) for h in scan_numbers(md) if h.kind in OVERRIDE_KINDS and h.raw not in keep]


def test_purge_removes_rows_sentences_bullets_and_empty_table_with_lead_in():
    res = purge_unverified(ARTICLE, bad_spans(ARTICLE))
    md = res.md
    # Bảng 1: mọi dòng dữ liệu có % chưa kiểm chứng → bỏ cả bảng + câu dẫn kết thúc bằng ":"
    assert "| Giải đấu |" not in md and "Premier League | 19,2" not in md
    assert "bức tranh tổng thể trở nên rõ ràng hơn" not in md
    assert "Riêng khu vực châu Âu hiện có nhiều giải đấu lớn nhỏ." in md  # câu trước câu dẫn được giữ
    assert "Dễ thấy Premier League là cái tên có sức hút vượt trội." in md
    # Câu văn chứa % bị xoá trọn câu, câu bên cạnh giữ nguyên
    assert "65% đến từ API" not in md and "Dữ liệu đến từ nhiều nơi. Phần còn lại do đội ngũ nhập tay." in md
    # Bullet chứa % bị xoá cả dòng; bullet khác giữ
    assert "Đối tác liên đoàn" not in md and "- Đội ngũ tại sân: cập nhật trực tiếp" in md
    # Bảng 2 còn dòng dữ liệu hợp lệ → giữ tiêu đề bảng, chỉ bỏ dòng có %
    assert "| Nguồn | Tỷ trọng |" in md and "| API | 65% |" not in md and "| Báo chí | đã kiểm chứng |" in md
    # Heading và marker section không bị đụng; không còn số loại override
    assert md.count("<!-- S0") == 2 and "### Nguồn dữ liệu" in md
    assert bad_spans(md) == [] and "\n\n\n" not in md
    assert len(res.removed) >= 7


def test_purge_keeps_verified_numbers_and_is_noop_without_spans():
    assert purge_unverified(ARTICLE, []).md == ARTICLE
    res = purge_unverified(ARTICLE, bad_spans(ARTICLE, keep={"42%", "35%"}))
    assert "| Premier League | 19,2 | 42% |" in res.md and "| Champions League | 15,6 | 35% |" in res.md
    assert "bức tranh tổng thể trở nên rõ ràng hơn:" in res.md  # bảng còn nguyên → giữ câu dẫn


def test_purge_removes_table_rows_matching_removed_claims_without_override_numbers():
    md = "Mở bài.\n\n| Khu vực | Số giải |\n|---|---|\n| Châu Âu | 750+ |\n| Châu Á | 520+ |\n\nKết bài.\n"
    res = purge_unverified(md, [], removed_rows=["Châu Âu | 750+", "một claim không phải dòng bảng"])
    assert "| Châu Âu | 750+ |" not in res.md and "| Châu Á | 520+ |" in res.md and "| Khu vực | Số giải |" in res.md
    res = purge_unverified(md, [], removed_rows=["Châu Âu | 750+", "Châu Á | 520+"])
    assert "|" not in res.md and res.md == "Mở bài.\n\nKết bài.\n"


def test_purge_does_not_delete_headings():
    md = "## Top 5 giải có 42% người xem\n\nNội dung không có số.\n"
    res = purge_unverified(md, bad_spans(md))
    assert res.md == md and res.removed == []


def test_purge_drops_table_already_left_with_header_only():
    """Model (P9) xoá hết dòng dữ liệu nhưng để lại tiêu đề bảng và câu dẫn — đúng lỗi gặp ở job thật."""
    md = ("Có nhiều giải đấu. Nhìn vào bảng, bức tranh rõ ràng hơn:\n\n| Giải đấu | Lượt theo dõi |\n|---|---|\n\n\n\n"
          "Dễ thấy Premier League có sức hút lớn.\n")
    res = purge_unverified(md, [])
    assert res.md == "Có nhiều giải đấu.\n\nDễ thấy Premier League có sức hút lớn.\n"
    assert any("bỏ cả bảng" in r for r in res.removed)
