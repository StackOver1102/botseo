"""Luồng rework (nhánh A đơn giản): REWORK → REWORK_CHECK → DONE."""
import json
from pipeline.artifacts import ArtifactStore, LocalBlobStore
from pipeline.executor import Worker
from pipeline.inputs import JobInput, create_job_with_input
from pipeline.llm.base import LLMResult
from pipeline.prompts import load_registry, load_schemas, load_shared
from pipeline.steps import HANDLERS, Services
from pipeline.steps.rework import check

OLD = ("Mở bài: hiện nay có hàng loạt website cung cấp kết quả bóng đá nhưng tỷ số cập nhật chậm sau 1-2 phút.\n\n"
       "## Tính năng livescore\n\nHệ thống hỗ trợ hơn 1.600 giải đấu lớn nhỏ từ Ngoại hạng Anh tới V-League, cập nhật "
       "bàn thắng, thẻ phạt và thay người theo thời gian thực cho người xem.\n\n"
       "## Kết luận\n\nKQBD.Mobi là nền tảng cung cấp kết quả bóng đá hôm nay trực tiếp và nhận định chuyên sâu.")
NEW = ("Nhiều trang kết quả bóng đá hiện nay vẫn trễ 1-2 phút, khiến người xem lỡ những khoảnh khắc đáng chú ý nhất.\n\n"
       "## Livescore theo thời gian thực\n\nTừ Ngoại hạng Anh cho tới V-League, hơn 1.600 giải đấu lớn nhỏ đều có mặt; "
       "bàn thắng, thẻ phạt hay lượt thay người hiện lên gần như ngay khi diễn ra trên sân cỏ.\n\n"
       "## Tổng kết\n\nNếu cần kết quả bóng đá trực tiếp trong ngày kèm nhận định sâu, KQBD.Mobi là lựa chọn gọn nhẹ.")
TH = {"rework_containment_max": 0.25, "rework_length_ratio": [0.7, 1.4], "shingle_n": 5}


def test_check_passes_good_rewrite():
    errs, report = check(OLD, NEW, TH)
    assert errs == [], errs
    assert report["headings_new"] == 2 and report["overlap"] <= 0.25


def test_check_catches_copy_cut_and_new_numbers():
    errs, _ = check(OLD, OLD, TH)
    assert any("trùng câu chữ" in e for e in errs)  # chép nguyên
    cut = NEW.split("## Tổng kết")[0].replace("1.600", "2.000")
    errs, _ = check(OLD, cut, TH)
    joined = " ".join(errs)
    assert "heading" in joined and "1.600" in joined and "2.000" in joined


class FakeWriter:
    def __init__(self, outputs):
        self.outputs, self.requests = list(outputs), []

    def complete(self, req, key):
        self.requests.append(req)
        return LLMResult(text=self.outputs.pop(0), stop_reason="end_turn", model=req.model, tokens_in=100, tokens_out=100,
                         cost_usd=0.01)


def test_rework_flow_revises_then_done(conn, config, blob_dir):
    brief = json.dumps({"ymyl": False, "ymyl_reason": "", "primary_keyword": "kết quả bóng đá",
                        "keywords": ["livescore", "từ khoá bịa không có trong bài"]}, ensure_ascii=False)
    fake = FakeWriter([brief, OLD, NEW])  # phân tích; lượt 1 chép nguyên → gửi lại kèm lỗi; lượt 2 đạt
    config = {**config, "thresholds": {**config["thresholds"], "rework_length_ratio": [0.7, 1.4]}}
    services = Services(load_registry(), load_schemas(), load_shared(), lambda name, job, step: fake,
                        lambda url, quote: None)
    artifacts = ArtifactStore(LocalBlobStore(blob_dir))
    job = create_job_with_input(conn, artifacts, JobInput(topic="kqbd", branch="A", article_md=OLD,
                                                          keywords=[{"keyword": "kết quả bóng đá"}]),
                                author_id="an", created_by="t", config=config, flow="rework")
    assert conn.execute("SELECT state FROM jobs WHERE id = %s", (job,)).fetchone()["state"] == "REWORK"
    Worker(conn, config, artifacts, HANDLERS, services=services).run_until_idle()
    row = conn.execute("SELECT state, cost_usd FROM jobs WHERE id = %s", (job,)).fetchone()
    assert row["state"] == "DONE"
    path = [r["to_state"] for r in conn.execute("SELECT to_state FROM job_transitions WHERE job_id = %s ORDER BY id", (job,))]
    assert path == ["REWORK_CHECK", "REWORK", "REWORK_CHECK", "DONE"]
    assert "trùng câu chữ" in fake.requests[2].user  # lượt 2 nhận lỗi cụ thể
    b = artifacts.latest(conn, job, "ReworkBrief").payload
    assert b["primary_keyword"] == "kết quả bóng đá" and b["keywords"] == ["livescore"]  # cụm bịa bị bỏ
    assert "kết quả bóng đá" in fake.requests[1].user
    assert artifacts.latest(conn, job, "FinalPackage").payload["article_markdown"] == NEW


def test_split_chunks_by_headings():
    from pipeline.steps.rework import split_chunks
    md = "Mở bài " + "chữ " * 50 + "\n\n" + "\n\n".join(f"## Mục {i}\n\n" + "từ " * 300 for i in range(5))
    chunks = split_chunks(md, max_words=700)
    assert len(chunks) == 3 and chunks[0].startswith("Mở bài") and all(c.strip() for c in chunks)
    assert "\n\n".join(chunks).count("## Mục") == 5


def test_check_keywords():
    spec = {"keep": ["Ngoại hạng Anh"], "add": [], "primary": "kết quả bóng đá"}
    bad = NEW.replace("kết quả bóng đá", "tỷ số").replace("Ngoại hạng Anh", "giải Anh")
    joined = " ".join(check(OLD, bad, TH, spec)[0])
    assert "Ngoại hạng Anh" in joined and "Keyword chính" in joined
    assert not [e for e in check(OLD, NEW, TH, spec)[0] if "eyword" in e or "từ khoá" in e]
