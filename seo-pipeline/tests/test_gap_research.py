"""GAP_IG_AI: code tìm và tải tài liệu nghiên cứu theo PAA + chủ đề để P4 đề xuất dữ kiện mới có nguồn."""
from types import SimpleNamespace

from pipeline import queue
from pipeline.artifacts import ArtifactStore, LocalBlobStore
from pipeline.executor import StepContext
from pipeline.inputs import JobInput, SerpIn
from pipeline.steps import m0
from pipeline.steps.common import Services

PAGE = "Theo Luật 7 của IFAB, một trận đấu gồm hai hiệp, mỗi hiệp 45 phút. " * 8


class FakeSerp:
    def __init__(self):
        self.queries = []

    def fetch(self, keyword, page=1):
        self.queries.append(keyword)
        urls = ["https://nguon-goc.vn/da-dung", "https://keo-nha-cai.example/soi-keo", "https://ngan.example/qua-ngan",
                f"https://bao.vn/{len(self.queries)}", "https://bao.vn/chung", "https://bao.vn/thu-ba"]
        return SerpIn(organic=[{"rank": i + 1, "url": u, "title": f"Tiêu đề {i}"} for i, u in enumerate(urls)], provider="fake")


def make_ctx(conn, config, blob_dir, serp):
    job_id = queue.create_job(conn, topic="t", author_id="a", created_by="x", cost_cap_usd=3, branch="B", mode="standard")
    conn.execute("UPDATE jobs SET leased_by = 'w1', lease_until = now() + interval '1 hour' WHERE id = %s", (job_id,))
    job = conn.execute("SELECT * FROM jobs WHERE id = %s", (job_id,)).fetchone()
    fetch = lambda url: (200, "ngắn") if "ngan.example" in url else (200, PAGE)
    services = Services({}, {}, {}, None, None, serp, fetch)
    return StepContext(conn, job, queue.job_state(job), config, ArtifactStore(LocalBlobStore(blob_dir)), "w1", services)


def test_gap_research_searches_paa_and_topic_and_is_cached(conn, config, blob_dir):
    config["gap_research"] = {"enabled": True, "max_searches": 3, "results_per_query": 2, "excerpt_chars": 600}
    serp = FakeSerp()
    ctx = make_ctx(conn, config, blob_dir, serp)
    inp = JobInput(topic="một trận bóng đá bao nhiêu phút", keywords=[], paa_extra=["Hiệp phụ dài bao lâu?"],
                   source_urls=["https://www.nguon-goc.vn/bai"])
    snapshot = {"paa": ["Bù giờ tính thế nào?", "Hiệp phụ dài bao lâu?"]}

    items = m0._gap_research(ctx, inp, snapshot)
    # PAA của SERP + PAA nhập tay (bỏ trùng) + chủ đề, cắt ở max_searches
    assert serp.queries == ["Bù giờ tính thế nào?", "Hiệp phụ dài bao lâu?", "một trận bóng đá bao nhiêu phút"]
    urls = [i["url"] for i in items]
    # Bỏ trang nguồn của job, trang nhà cái, trang quá ngắn; không lấy trùng URL giữa các truy vấn
    assert urls == ["https://bao.vn/1", "https://bao.vn/chung", "https://bao.vn/2", "https://bao.vn/thu-ba", "https://bao.vn/3"]
    assert all("45 phút" in i["excerpt"] and len(i["excerpt"]) <= 700 for i in items) and items[0]["query"] == "Bù giờ tính thế nào?"

    assert m0._gap_research(ctx, inp, snapshot) == items and len(serp.queries) == 3  # vòng revise: dùng lại, không tìm lại

    # Không có PAA: keyword dạng câu hỏi của job được dùng làm truy vấn, trước chủ đề
    serp2 = FakeSerp()
    m0._gap_research(make_ctx(conn, config, blob_dir, serp2), JobInput(topic="kết quả bóng đá", keywords=[]), {"paa": []},
                     ["kết quả bóng đá hôm nay", "tỷ số FT là gì", "hiệp phụ bao nhiêu phút"])
    assert serp2.queries == ["tỷ số FT là gì", "hiệp phụ bao nhiêu phút", "kết quả bóng đá"]


def test_gap_research_off_or_without_provider(conn, config, blob_dir):
    inp = JobInput(topic="chủ đề", keywords=[])
    config["gap_research"] = {"enabled": False}
    assert m0._gap_research(make_ctx(conn, config, blob_dir, FakeSerp()), inp, {"paa": ["a?"]}) == []
    config["gap_research"] = {"enabled": True}
    assert m0._gap_research(make_ctx(conn, config, blob_dir, None), inp, {"paa": ["a?"]}) == []
