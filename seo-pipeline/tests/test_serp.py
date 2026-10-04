import json

import httpx
import pytest

from pipeline.llm.base import ProviderError, TransientError
from pipeline.serp import SerperProvider

RESP = {
    "organic": [{"title": "Vay online", "link": "https://a.vn/vay", "snippet": "Hướng dẫn", "position": 1},
                {"title": "App uy tín", "link": "https://b.vn/app", "snippet": "Danh sách", "position": 2}],
    "peopleAlsoAsk": [{"question": "Vay online có an toàn không?", "snippet": "…"}],
}


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_serper_request_and_parse(monkeypatch):
    monkeypatch.setenv("SERPER_API_KEY", "k")
    seen = {}

    def handler(req):
        seen["body"], seen["key"] = json.loads(req.content), req.headers["X-API-KEY"]
        return httpx.Response(200, json=RESP)

    serp = SerperProvider({}, client(handler)).fetch("vay tiền online")
    assert seen["body"] == {"q": "vay tiền online", "gl": "vn", "hl": "vi", "num": 10} and seen["key"] == "k"
    assert [o.url for o in serp.organic] == ["https://a.vn/vay", "https://b.vn/app"]
    assert serp.paa == ["Vay online có an toàn không?"] and serp.aio_present is None and serp.provider == "serper"


def test_serper_errors(monkeypatch):
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    with pytest.raises(ProviderError, match="SERPER_API_KEY"):
        SerperProvider({}).fetch("x")
    monkeypatch.setenv("SERPER_API_KEY", "k")
    with pytest.raises(TransientError):
        SerperProvider({}, client(lambda r: httpx.Response(429))).fetch("x")
    with pytest.raises(ProviderError):
        SerperProvider({}, client(lambda r: httpx.Response(403, text="bad key"))).fetch("x")


# ---------- auto_source: tư liệu nhánh B từ top SERP ----------

from pipeline.autosource import collect_sources  # noqa: E402
from pipeline.config import load_config  # noqa: E402
from pipeline.inputs import SerpIn  # noqa: E402
from pipeline.urlcheck import UrlVerifier  # noqa: E402


def _serp(*urls):
    return SerpIn(organic=[{"rank": i, "url": u, "title": f"T{i}"} for i, u in enumerate(urls, 1)], provider="serper")


def test_collect_sources_filters_and_backfills():
    cfg = {**load_config()["adapters"]["auto_source"], "max_sources": 2, "min_chars": 50, "max_chars_per_source": 120}
    long = "Một trận bóng đá chính thức kéo dài 90 phút, chia hai hiệp. " * 5
    pages = {"https://a.vn/1": (200, long), "https://www.a.vn/2": (200, long), "https://ngan.vn/x": (200, "ngắn"),
             "https://loi.vn/x": (403, ""), "https://b.vn/y": (200, long), "https://c.vn/z": (200, long)}
    serp = _serp("https://www.youtube.com/watch?v=1", "https://soikeo.vn/1-tran", "https://w88.com/x", "https://a.vn/1",
                 "https://www.a.vn/2", "https://ngan.vn/x", "https://loi.vn/x", "https://b.vn/y", "https://c.vn/z")
    sources, skipped = collect_sources(serp, lambda u: pages.get(u, (200, long)), cfg)
    assert [s.url for s in sources] == ["https://a.vn/1", "https://b.vn/y"]  # đủ 2 thì dừng, c.vn không tải
    reasons = {sk.url: sk.reason for sk in skipped}
    assert "domain chặn" in reasons["https://www.youtube.com/watch?v=1"]
    assert "mẫu chặn" in reasons["https://soikeo.vn/1-tran"] and "mẫu chặn" in reasons["https://w88.com/x"]
    assert reasons["https://www.a.vn/2"] == "trùng domain" and "ngắn" in reasons["https://ngan.vn/x"]
    assert "HTTP 403" in reasons["https://loi.vn/x"]
    assert all(len(s.text) <= 120 and s.text.endswith(".") for s in sources)  # cắt ở ranh giới câu
    assert sources[1].as_source_text(2).startswith("[Nguồn 02 | T8 | https://b.vn/y]\n")


def test_main_text_extracts_article_body():
    body = "".join(f"<p>Đoạn {i}: một trận bóng đá chính thức có hai hiệp, mỗi hiệp 45 phút cộng bù giờ.</p>" for i in range(8))
    page = (f"<html><head><title>x</title></head><body><nav><a href='/'>Trang chủ</a> <a href='/kq'>Kết quả</a></nav>"
            f"<article><h1>1 trận bóng đá bao nhiêu phút</h1>{body}</article><footer>Bản quyền 2026 Công ty ABC</footer></body></html>")

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text=page, headers={"content-type": "text/html; charset=utf-8"})

    status, text = UrlVerifier(client(handler)).main_text("https://a.vn/bai")
    assert status == 200 and "mỗi hiệp 45 phút" in text
    assert "Bản quyền 2026" not in text and "Trang chủ" not in text
