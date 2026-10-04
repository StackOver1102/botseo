"""Nhánh A trong luồng full: outline phải phủ mọi section gốc, writer nhận đoạn gốc của section."""
from pipeline.steps.branch_a import cover_original, original_sections, section_original

MD = ("Mở bài giới thiệu ngắn về chủ đề.\n\n## Một\nNội dung một.\n\n### Một chấm một\nChi tiết.\n\n"
      "## Hai\nNội dung hai.\n\n## Ba\nNội dung ba.")


def test_original_sections_ids_and_intro():
    secs = original_sections(MD)
    assert [s["id"] for s in secs] == ["O00", "O01", "O02", "O03", "O04"]
    assert secs[0]["heading"] == "(mở bài)" and secs[2]["level"] == 3 and secs[1]["words"] == 3


def test_cover_original_fills_gaps_into_previous_h2():
    secs = original_sections(MD)
    outline = [{"section_id": "S01", "level": 2, "heading": "A", "source_sections": ["O00", "O01"]},
               {"section_id": "S02", "level": 3, "heading": "A1", "source_sections": ["O02"]},
               {"section_id": "S03", "level": 2, "heading": "Mới (IG)", "source_sections": []},
               {"section_id": "S04", "level": 2, "heading": "C", "source_sections": ["O04", "O99"]}]
    fixed, notes = cover_original(outline, secs)
    # O03 bị bỏ sót: section gốc đứng trước nó (O02) thuộc H3 S02 → gán lên H2 cha S01; mã lạ O99 bị bỏ
    assert fixed[0]["source_sections"] == ["O00", "O01", "O03"] and fixed[3]["source_sections"] == ["O04"]
    assert notes == ["O03 gán vào S01"]
    text = section_original(fixed, 0, secs)
    assert "Mở bài" in text and "Chi tiết." in text and "Nội dung hai." in text and "Nội dung ba." not in text
    assert "[Mục gốc: Một]" in text and "## " not in text  # heading gốc thành nhãn, writer không chép lại
    assert section_original(fixed, 2, secs) == ""  # section mới cho IG: không có bản gốc


def test_dedupe_headings():
    from pipeline.steps.branch_a import dedupe_headings
    md = "<!-- S01 -->\n## Giới thiệu A\n\nMở bài.\n\n## Giới thiệu  A\nNội dung.\n\n### Con\nX."
    assert dedupe_headings(md) == "<!-- S01 -->\n## Giới thiệu A\n\nMở bài.\n\nNội dung.\n\n### Con\nX."


def test_cleanup_leading_ellipsis():
    from pipeline.steps.branch_a import dedupe_headings
    assert dedupe_headings("## A\n\n...phần lớn không đạt.\n\n… còn nữa") == "## A\n\nPhần lớn không đạt.\n\nCòn nữa"
