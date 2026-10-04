"""Nhiều website: hồ sơ site, kho dữ liệu tầng site (dùng lại không giới hạn, không lọt sang site khác), job chọn site."""
import datetime as dt
import importlib.util
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from knowledge.repository import EntryInput, import_entry, retrieve
from pipeline import queue
from pipeline.authors import upsert_author
from pipeline.sites import SiteError, get_site, list_sites, set_internal_pages, site_id_from_url, site_owner, upsert_site

WEIGHTS = {"tag": 0.4, "fulltext": 0.3, "vector": 0.3}
IGS = [{"ig_id": "IG01", "description": "tốc độ cập nhật tỷ số của website", "inputs_needed": ""}]


def entry(owner, title, site_id=None, reusable=False):
    return EntryInput(author_id=owner, site_id=site_id, reusable=reusable, kind="quy_trinh", title=title,
                      content="Tỷ số được cập nhật ngay sau mỗi bàn thắng.", topic_tags=["cập nhật tỷ số", "tốc độ"],
                      valid_until=dt.date.today() + dt.timedelta(days=90))


def test_site_crud_and_internal_pages(conn):
    assert site_id_from_url("https://www.KQBD2.com/") == "kqbd2.com" and site_id_from_url("ten-site.vn/abc") == "ten-site.vn"
    with pytest.raises(SiteError):
        upsert_site(conn, "X", "khong-phai-url")
    aid = upsert_author(conn, "Ban biên tập", kind="organization")
    sid = upsert_site(conn, "KQBD2", "https://www.kqbd2.com/", default_author_id=aid)
    assert sid == "kqbd2.com"
    with pytest.raises(SiteError):
        upsert_site(conn, "Y", "https://y.vn", default_author_id="khong-co")
    upsert_site(conn, "KQBD2 mới", "https://kqbd2.com")  # cập nhật tên, không làm mất tác giả mặc định
    assert get_site(conn, sid)["default_author_id"] == aid and get_site(conn, sid)["name"] == "KQBD2 mới"
    n = set_internal_pages(conn, sid, [{"url": "https://kqbd2.com/lich", "title": "Lịch thi đấu"},
                                       {"url": "https://kqbd2.com/lich", "title": "trùng"},
                                       {"url": "javascript:x", "title": "sai"}, {"url": "https://kqbd2.com/bxh", "title": ""}])
    assert n == 1
    row = list_sites(conn)[0]
    assert (row["id"], row["internal_pages"], row["knowledge_entries"], row["authors"]) == (sid, 1, 0, 0)


def test_site_knowledge_is_reusable_and_isolated(conn):
    a = upsert_site(conn, "Site A", "https://a.vn")
    b = upsert_site(conn, "Site B", "https://b.vn")
    site_entry = import_entry(conn, entry(site_owner(a), "Cập nhật tỷ số của A", site_id=a, reusable=True)).entry_id
    author_entry = import_entry(conn, entry("an", "Trải nghiệm của An")).entry_id
    conn.execute("UPDATE author_knowledge SET usage_count = 99")  # đã dùng rất nhiều bài

    def ids(author, site):
        job = queue.create_job(conn, topic="t", author_id=author, created_by="x", cost_cap_usd=1, site_id=site)
        return {c.entry_id for c in retrieve(conn, job, author, IGS, WEIGHTS, max_reuse=5, site_id=site)["IG01"]}

    assert ids("an", a) == {site_entry}           # mục tầng site không bị giới hạn số bài; mục tác giả đã hết lượt
    assert ids("binh", a) == {site_entry}         # tác giả khác của cùng site vẫn dùng được kho của site
    assert ids("an", b) == set() and ids("an", None) == set()  # kho site A không lọt sang site B / job không gắn site
    conn.execute("UPDATE author_knowledge SET usage_count = 0")
    assert ids("an", b) == {author_entry}         # mục của tác giả đi theo tác giả ở mọi site


def test_ui_job_with_site_uses_site_author_pages_and_knowledge_scope(conn, db_url, monkeypatch):
    monkeypatch.setenv("SEO_DATABASE_URL", db_url)
    from app.main import app
    from app.ui import BLOBS
    from pipeline.artifacts import ArtifactStore
    c = TestClient(app)
    assert c.get("/ui/api/sites").json() == {"sites": []}
    aid = c.post("/ui/api/authors", json={"name": "Ban biên tập A", "kind": "organization"}).json()["id"]
    sid = c.post("/ui/api/sites", json={"name": "Site A", "url": "https://a.vn", "default_author_id": aid}).json()["id"]
    assert sid == "a.vn" and c.post("/ui/api/sites", json={"name": "X", "url": "khong-url"}).status_code == 422
    set_internal_pages(conn, sid, [{"url": "https://a.vn/lich", "title": "Lịch thi đấu"}])
    # tác giả gắn với site
    own = c.post("/ui/api/authors", json={"name": "Cây bút A", "site_id": sid}).json()["id"]
    assert {a["id"]: a["site_id"] for a in c.get("/ui/api/authors").json()["authors"]} == {aid: None, own: sid}

    body = {"branch": "A", "topic": "Kết quả bóng đá", "keywords_text": "kết quả bóng đá,9000",
            "article_md": "# Kết quả\n\nBài cũ.", "site_id": sid}  # không chọn tác giả → tác giả mặc định của site
    job_id = c.post("/ui/api/jobs", json=body).json()["id"]
    job = conn.execute("SELECT site_id, author_id FROM jobs WHERE id = %s", (job_id,)).fetchone()
    assert job == {"site_id": sid, "author_id": aid}
    inp = ArtifactStore(BLOBS).latest(conn, job_id, "JobInput").payload
    assert inp["author"] == {"name": "Ban biên tập A", "url": None, "type": "Organization"}
    assert inp["internal_pages"] == [{"url": "https://a.vn/lich", "title": "Lịch thi đấu"}]
    assert c.post("/ui/api/jobs", json={**body, "site_id": "khong-co.vn"}).status_code == 422
    assert c.get("/ui/api/sites").json()["sites"][0]["authors"] == 1


def _site_tool():
    spec = importlib.util.spec_from_file_location("site_tool", Path(__file__).resolve().parent.parent / "scripts" / "site_tool.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_draft_entries_must_be_verbatim_from_the_site():
    tool = _site_tool()
    pages = {"https://a.vn/gioi-thieu": "Chúng tôi theo dõi hơn 2.000 giải đấu. Tỷ số được cập nhật với độ trễ dưới 5 giây, "
                                        "độ chính xác 99,7% theo thống kê nội bộ. Liên hệ quảng cáo.",
             "https://a.vn/": "Trang chủ có lịch thi đấu và bảng xếp hạng của các giải lớn."}
    drafts = [
        {"title": "Độ trễ", "kind": "so_lieu_noi_bo", "topic_tags": ["độ trễ"], "source_url": "https://a.vn/gioi-thieu",
         "content": "Tỷ số được cập nhật với độ trễ dưới 5 giây, độ chính xác 99,7% theo thống kê nội bộ."},
        {"title": "Bịa", "kind": "so_lieu_noi_bo", "topic_tags": ["x"], "source_url": "https://a.vn/gioi-thieu",
         "content": "Chúng tôi có 10 triệu người dùng mỗi tháng."},                       # không có trong trang
        {"title": "Trang chủ", "kind": "quy_trinh", "topic_tags": [], "source_url": "https://sai-url.vn/",
         "content": "Trang chủ có lịch thi đấu và bảng xếp hạng của các giải lớn."},      # URL sai nhưng câu có thật
    ]
    kept, dropped = tool.check_draft_entries(drafts, pages, dt.date(2026, 10, 4))
    assert [k["title"] for k in kept] == ["Độ trễ", "Trang chủ"] and len(dropped) == 1 and "10 triệu" in dropped[0]
    assert kept[0]["facts"] == [{"label": "(sửa nhãn) 99,7%", "value": "99,7", "unit": "%", "as_of": "2026-10"}]
    assert kept[0]["evidence_ref"] == "https://a.vn/gioi-thieu" and kept[0]["valid_until"] == "2027-10-04"
    assert kept[1]["evidence_ref"] == "https://a.vn/" and kept[1]["topic_tags"] == ["website"] and kept[1]["facts"] == []


def test_checked_draft_imports_as_site_tier(conn):
    tool = _site_tool()
    sid = upsert_site(conn, "Site A", "https://a.vn")
    pages = {"https://a.vn/gioi-thieu": "Tỷ số được cập nhật với độ trễ dưới 5 giây, độ chính xác 99,7% theo thống kê nội bộ."}
    kept, _ = tool.check_draft_entries([{"title": "Độ trễ", "kind": "so_lieu_noi_bo", "topic_tags": ["độ trễ"],
                                         "source_url": "https://a.vn/gioi-thieu", "content": pages["https://a.vn/gioi-thieu"]}],
                                       pages, dt.date.today())
    e = kept[0]
    import_entry(conn, EntryInput(author_id=site_owner(sid), site_id=sid, reusable=True, kind=e["kind"], title=e["title"],
                                  content=e["content"], topic_tags=e["topic_tags"],
                                  valid_until=dt.date.fromisoformat(e["valid_until"]), facts=e["facts"],
                                  evidence_ref=e["evidence_ref"]))  # facts tự khai đủ cho số % nên không bị từ chối
    row = conn.execute("SELECT author_id, site_id, reusable FROM author_knowledge").fetchone()
    assert row == {"author_id": "site:a.vn", "site_id": "a.vn", "reusable": True}
    assert list_sites(conn)[0]["knowledge_entries"] == 1
