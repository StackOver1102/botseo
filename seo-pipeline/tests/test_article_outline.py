from pipeline.steps.m0 import _article_outline

# Nhánh A: bài dán từ web, không có tiêu đề, heading là dòng thường (không có #)
PLAIN = ("Hiện nay, có hàng loạt website cung cấp kết quả bóng đá trực tiếp. Người dùng gặp phải những tình trạng:\n\n"
         "- Tỷ số cập nhật chậm sau 1-2 phút.\n\n"
         "Giới thiệu Kqbd.mobi - Tỷ số kết quả bóng đá hôm nay trực tiếp NHANH NHẤT\n"
         "Kqbd Mobi được mệnh danh là giải pháp toàn diện cho Fan bóng đá.\n\n"
         "Cam kết từ KQBD.Mobi\nChúng tôi cam kết:\n\n- Không có độ trễ.\n\n"
         "Chính xác: Ưu tiên độ tin cậy của dữ liệu.\n\n"
         "Kết luận\nKQBD.Mobi là nền tảng chuyên cung cấp kết quả bóng đá.\n")


def test_markdown_headings_are_used_when_present():
    assert _article_outline("## Vay tiền online là gì\nNội dung.\n\n## Rủi ro\nNội dung.\n") == "Vay tiền online là gì\nRủi ro"


def test_plain_article_without_title_or_hash_headings():
    assert _article_outline(PLAIN) == ("Giới thiệu Kqbd.mobi - Tỷ số kết quả bóng đá hôm nay trực tiếp NHANH NHẤT\n"
                                       "Cam kết từ KQBD.Mobi\nKết luận")


def test_empty_article_has_no_outline():
    assert _article_outline(None) is None
    assert _article_outline("  \n") is None
    assert _article_outline("Chỉ một đoạn văn dài kết thúc bằng dấu chấm.") is None
