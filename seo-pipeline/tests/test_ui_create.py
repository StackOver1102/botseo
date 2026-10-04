import base64

from fastapi.testclient import TestClient

from pipeline.keyword_io import decode, parse_keywords


def test_parse_keywords_formats():
    ahrefs = "#\tKeyword\tCountry\tVolume\n1\ttrực tiếp bóng đá\tvn\t1240000\n2\txem bóng đá\tvn\t103000\n3\tXem bóng đá\tvn\t5\n"
    rows = parse_keywords(decode(("﻿" + ahrefs).encode("utf-16")))
    assert rows == [{"keyword": "trực tiếp bóng đá", "volume": 1240000}, {"keyword": "xem bóng đá", "volume": 103000}]
    assert parse_keywords("vay tiền,5000\nvay online") == [{"keyword": "vay tiền", "volume": 5000},
                                                          {"keyword": "vay online", "volume": None}]


def test_ui_creates_branch_a_job(conn, db_url, monkeypatch):
    monkeypatch.setenv("SEO_DATABASE_URL", db_url)
    from app.main import app
    c = TestClient(app)
    body = {"branch": "A", "topic": "Vay tiền online", "keywords_text": "vay tiền online,9000",
            "keywords_b64": base64.b64encode("Keyword,Volume\nvay online uy tín,300\n".encode()).decode(),
            "article_md": "# Vay tiền online\n\nBài cũ.", "article_url": "https://site.vn/vay"}
    r = c.post("/ui/api/jobs", json=body)
    assert r.status_code == 201, r.text
    job = conn.execute("SELECT branch, state, created_by FROM jobs WHERE id = %s", (r.json()["id"],)).fetchone()
    assert job == {"branch": "A", "state": "TRIAGE_AI", "created_by": "ui"} and r.json()["keywords"] == 2  # mặc định: full
    rw = c.post("/ui/api/jobs", json={**body, "flow": "rework"}).json()["id"]
    assert conn.execute("SELECT state FROM jobs WHERE id = %s", (rw,)).fetchone()["state"] == "REWORK"
    assert c.post("/ui/api/jobs", json={**body, "article_md": "", "article_url": None}).status_code == 422  # thiếu bài
    # nhánh A không cần keyword (tự lấy từ bài) — cả full lẫn rework; nhánh B thì cần
    for flow in ("full", "rework"):
        assert c.post("/ui/api/jobs", json={**body, "flow": flow, "keywords_text": "", "keywords_b64": None}).status_code == 201
    assert c.post("/ui/api/jobs", json={"branch": "B", "topic": "x y z", "auto_source": 0}).status_code == 422


def test_normalize_article_plain_text():
    from pipeline.article_io import normalize_article
    raw = ("Mở bài dài dòng giới thiệu trang web cho người đọc mới.\n\n- Ý một của danh sách.\n\n"
           "Mục tiêu - Tầm nhìn - Sứ mệnh của website\nMục tiêu\n\nChuyên trang hướng tới việc trở thành nền tảng số 1.\n\n"
           "Chúng tôi cam kết:\n\n- Không có độ trễ.\n\nKết luận\nKQBD là nền tảng cung cấp kết quả bóng đá.")
    heads = [ln for ln in normalize_article(raw).splitlines() if ln.startswith("## ")]
    assert heads == ["## Mục tiêu - Tầm nhìn - Sứ mệnh của website", "## Mục tiêu", "## Kết luận"]
    assert normalize_article("# Có sẵn\n\nNội dung") == "# Có sẵn\n\nNội dung"  # đã là Markdown: giữ nguyên


def test_ui_branch_a_without_topic(conn, db_url, monkeypatch):
    monkeypatch.setenv("SEO_DATABASE_URL", db_url)
    from app.main import app
    r = TestClient(app).post("/ui/api/jobs", json={"branch": "A", "keywords_text": "kqbd,40000\nkết quả bóng đá,9000",
                                                   "article_md": "Mở bài dài một chút cho có nội dung.\n\nGiới thiệu\nĐoạn văn dài hơn heading."})
    assert r.status_code == 201, r.text
    job = conn.execute("SELECT topic FROM jobs WHERE id = %s", (r.json()["id"],)).fetchone()
    assert job["topic"] == "kqbd"


def test_parse_keywords_quoted_csv():
    """Ahrefs/Semrush xuất CSV dấu phẩy, mọi ô trong ngoặc kép, volume có dấu phân cách hàng nghìn."""
    text = '"#","Keyword","Country","Volume"\n"1","kqbd","vn","450000"\n"2","ket qua bong da","vn","1,200,000"\n"3","#","vn",""\n'
    assert parse_keywords(text) == [{"keyword": "ket qua bong da", "volume": 1200000}, {"keyword": "kqbd", "volume": 450000}]


def _html_client(page):
    import httpx
    return httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, text=page, headers={"content-type": "text/html; charset=utf-8"})))


PAGE = ("<html><body><nav>Trang chủ | Kết quả</nav><article><h1>Kết quả bóng đá hôm nay</h1>"
        + "".join(f"<h2>Mục {i}</h2><p>{'Nội dung mục có hơn 1.600 giải đấu được cập nhật theo thời gian thực. ' * 4}</p>"
                  for i in range(3))
        + "</article><footer>Bản quyền 2026</footer></body></html>")


def test_fetch_article_keeps_headings_and_splits_title():
    from pipeline.article_io import fetch_article
    title, md = fetch_article("https://site.vn/bai-cu", _html_client(PAGE))
    assert title == "Kết quả bóng đá hôm nay"
    assert [l for l in md.splitlines() if l.startswith("#")] == ["## Mục 0", "## Mục 1", "## Mục 2"]
    assert "Trang chủ" not in md and "Bản quyền" not in md


def test_fetch_article_rejects_empty_page():
    import pytest
    from pipeline.article_io import FetchError, fetch_article
    with pytest.raises(FetchError, match="không trích được"):
        fetch_article("https://site.vn/x", _html_client("<html><body><div id='app'></div></body></html>"))


def test_authors_list_and_job_uses_selected_author(conn, db_url, monkeypatch):
    """Tác giả lấy từ bảng authors: tên/URL vào JobInput.author, hồ sơ giọng văn vào JobInput.voice_profile."""
    import json

    from pipeline.artifacts import ArtifactStore, LocalBlobStore
    from pipeline.authors import set_voice_profile, slugify
    monkeypatch.setenv("SEO_DATABASE_URL", db_url)
    from app.main import app
    from app.ui import BLOBS
    c = TestClient(app)
    assert c.get("/ui/api/authors").json() == {"authors": []}
    assert slugify("Nguyễn Văn Ân") == "nguyen-van-an"
    aid = c.post("/ui/api/authors", json={"name": "Ban biên tập Thể Thao", "url": "https://site.vn/gioi-thieu"}).json()["id"]
    assert aid == "ban-bien-tap-the-thao"
    assert c.post("/ui/api/authors", json={"name": "X", "url": "khong-phai-url"}).status_code == 422
    assert c.post("/ui/api/authors", json={"name": "Tên mới", "id": aid, "url": None}).status_code == 201  # cập nhật theo mã
    profile = {"dimensions": [], "positive_exemplars": [], "negative_exemplars_qa_only": [], "checklist_qa": []}
    set_voice_profile(conn, aid, profile, "https://site.vn/bai-1")
    conn.execute("INSERT INTO author_knowledge (id, author_id, kind, title, content, topic_tags, valid_until, search_text) "
                 "VALUES ('K0001', %s, 'quan_diem', 't', 'c', '{x}', current_date + 30, 'c')", (aid,))
    listed = c.get("/ui/api/authors").json()["authors"]
    assert [(a["id"], a["name"], a["url"], a["has_voice"], a["knowledge_entries"]) for a in listed] == [(aid, "Tên mới", None, True, 1)]

    body = {"branch": "A", "topic": "Vay tiền online", "keywords_text": "vay tiền online,9000",
            "article_md": "# Vay tiền online\n\nBài cũ.", "author_id": aid}
    job_id = c.post("/ui/api/jobs", json=body).json()["id"]
    inp = ArtifactStore(BLOBS).latest(conn, job_id, "JobInput").payload
    assert inp["author"] == {"name": "Tên mới", "url": None, "type": "Person"} and inp["voice_profile"] == profile
    assert conn.execute("SELECT author_id FROM jobs WHERE id = %s", (job_id,)).fetchone()["author_id"] == aid
    # Mã không có trong bảng: như trước (default_author của config, không có giọng văn)
    other = c.post("/ui/api/jobs", json={**body, "author_id": "tac-gia-01"}).json()["id"]
    assert ArtifactStore(BLOBS).latest(conn, other, "JobInput").payload["voice_profile"] is None


def test_steps_tab_lists_every_step_with_edit_info(conn, db_url, monkeypatch):
    """Tab "Các bước": đủ mọi bước của luồng, bước đang chạy được đánh dấu, mỗi bước chỉ ra chỗ chỉnh."""
    monkeypatch.setenv("SEO_DATABASE_URL", db_url)
    from app.main import app
    from app.step_info import STEP_INFO
    from app.ui import PIPELINE
    assert [s for s in PIPELINE if s != "DONE" and s not in STEP_INFO] == []  # thêm state mới thì phải mô tả cho UI
    c = TestClient(app)
    job_id = c.post("/ui/api/jobs", json={"branch": "A", "keywords_text": "kqbd,40000",
                                          "article_md": "# KQBD\n\nBài cũ của site."}).json()["id"]
    steps = c.get(f"/ui/api/jobs/{job_id}/steps").json()["steps"]
    assert [s["state"] for s in steps] == [s for s in PIPELINE if s != "DONE"]
    by = {s["state"]: s for s in steps}
    assert by["TRIAGE_AI"]["status"] == "running" and by["WRITE"]["status"] == "pending"
    write = by["WRITE"]["edit"]
    assert write["code"].startswith("pipeline/steps/m0.py:") and write["code"].endswith("(write)")
    p6 = write["prompts"][0]
    assert p6["file"] == "prompts/P6-S.yaml" and p6["model_key"] == "models.writer" and p6["model"]["provider"]
    assert {"key": "models.writer", "value": by["WRITE"]["edit"]["config"][0]["value"]} == by["WRITE"]["edit"]["config"][0]
    assert by["KW_NORMALIZE"]["edit"]["prompts"] == [] and by["KW_NORMALIZE"]["runs"] == []
    rw = c.post("/ui/api/jobs", json={"branch": "A", "flow": "rework", "article_md": "# KQBD\n\nBài cũ của site."}).json()["id"]
    assert [s["state"] for s in c.get(f"/ui/api/jobs/{rw}/steps").json()["steps"]] == ["REWORK", "REWORK_CHECK"]
