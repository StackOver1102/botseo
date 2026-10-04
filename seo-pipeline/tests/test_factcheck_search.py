"""FACTCHECK mode search: code tìm nguồn (SERP provider), tải trang, model chỉ đối chiếu bằng P10-S theo từng lô."""
from types import SimpleNamespace

from pipeline import queue
from pipeline.artifacts import ArtifactStore, LocalBlobStore
from pipeline.executor import StepContext
from pipeline.inputs import JobInput, SerpIn
from pipeline.steps import m1
from pipeline.steps.common import Services, datamark_char, undatamark

ARTICLE = ("<!-- S01 -->\n## Thời lượng\nMột trận bóng đá kéo dài 90 phút theo Luật 7. "
           "Trang này có độ chính xác 99,7% theo tự công bố.\n\n"
           "<!-- S02 -->\n## Hiệp phụ\nHiệp phụ gồm hai hiệp, mỗi hiệp 15 phút. Premier League có 42% người quan tâm.\n")
PAGES = {
    "https://www.theifab.com/laws/7": "Law 7. Một trận đấu kéo dài hai hiệp bằng nhau, mỗi hiệp 45 phút, tổng 90 phút thi đấu.",
    "https://vnexpress.net/hiep-phu": "Hiệp phụ gồm hai hiệp, mỗi hiệp 15 phút theo quy định của IFAB.",
}


def claim(cid, verbatim, ctype="so_lieu"):
    return {"claim_id": cid, "verbatim": verbatim, "type": ctype, "in_article_source": "", "maybe_outdated": False,
            "outdated_reason": "", "action": "GIU", "new_value": None, "verification_source": None,
            "status": "DA_DUYET", "origin": "bai_nguon"}


CLAIMS = [claim("C001", "Một trận bóng đá kéo dài 90 phút theo Luật 7", "luat"),
          claim("C002", "Trang này có độ chính xác 99,7%"),
          claim("C003", "Hiệp phụ gồm hai hiệp, mỗi hiệp 15 phút"),
          claim("C004", "Premier League | 19,2 | Anh | 42%")]


class FakeSerp:
    def __init__(self):
        self.queries = []

    def fetch(self, keyword, page=1):
        self.queries.append(keyword)
        if "90 phút" in keyword:
            urls = ["https://nguon-goc.vn/bai", "https://www.theifab.com/laws/7", "https://ca-cuoc.example/keo-nha-cai"]
        elif "15 phút" in keyword:
            urls = ["https://vnexpress.net/hiep-phu"]
        elif "99,7%" in keyword:
            urls = ["https://nguon-goc.vn/gioi-thieu"]           # chỉ có chính trang nguồn nói con số này
        else:
            urls = ["https://loi-mang.example/x"]                 # tìm thấy nhưng không tải được
        return SerpIn(organic=[{"rank": i + 1, "url": u} for i, u in enumerate(urls)], provider="fake")


def make_ctx(conn, config, blob_dir, serp):
    store = ArtifactStore(LocalBlobStore(blob_dir))
    job_id = queue.create_job(conn, topic="t", author_id="a", created_by="x", cost_cap_usd=3, branch="B", mode="standard",
                              ymyl=False)
    conn.execute("UPDATE jobs SET state = 'FACTCHECK', leased_by = 'w1', lease_until = now() + interval '1 hour' WHERE id = %s", (job_id,))
    inp = JobInput(topic="một trận bóng đá bao nhiêu phút", keywords=[], source_urls=["https://www.nguon-goc.vn/bai"])
    for kind, payload in [("JobInput", inp.model_dump(mode="json")), ("FactInventory", {"claims": CLAIMS}),
                          ("ArticleDraft", {"body_markdown": ARTICLE, "keyword_placement": [], "can_nguoi": [], "change_log": []})]:
        store.save(conn, job_id, kind, payload, "test")
    job = conn.execute("SELECT * FROM jobs WHERE id = %s", (job_id,)).fetchone()
    services = Services({}, {}, {}, None, None, serp, lambda url: (200, PAGES[url]) if url in PAGES else (None, ""))
    return StepContext(conn, job, queue.job_state(job), config, store, "w1", services)


def fake_p10s(calls):
    def run(sctx, prompt_id, variables, hashes, **kw):
        provided = kw["ctx"]["provided_sources"]
        calls.append({"prompt_id": prompt_id, "claims": list(provided), "text": variables["CLAIMS_WITH_SOURCES"]})
        items = [{"claim_id": cid, "atomic_claim": "x", "verdict": "DUNG", "current_value": "",
                  "sources": [{"url": next(iter(src)), "date": "", "quote": "trích", "tier": 1}],
                  "conflict_note": "", "suggested_action": ""} for cid, src in provided.items()]
        items.append({"claim_id": "C999", "atomic_claim": "lạ", "verdict": "DUNG", "current_value": "", "sources": [],
                      "conflict_note": "", "suggested_action": ""})  # model bịa claim_id
        return SimpleNamespace(parsed={"items": items}, run_id=None)
    return run


def test_factcheck_searches_fetches_and_verifies(conn, config, blob_dir, monkeypatch):
    config["factcheck"] = {"mode": "search", "results_per_claim": 2, "max_searches": 40, "claims_per_call": 1}
    serp, calls = FakeSerp(), []
    ctx = make_ctx(conn, config, blob_dir, serp)
    monkeypatch.setattr(m1, "_run", fake_p10s(calls))

    res = m1.factcheck(ctx)
    assert "4 lượt tìm" in res.reason and "2/4 claim có nguồn" in res.reason

    # Truy vấn là chính câu claim, ký tự bảng được bỏ; claim loại chặn (luật, số liệu) được tìm trước
    assert serp.queries[0] == "Một trận bóng đá kéo dài 90 phút theo Luật 7"
    assert "Premier League 19,2 Anh 42%" in serp.queries

    search = ctx.artifacts.latest(conn, ctx.job_id, "FactSearch").payload
    # Bỏ trang nguồn của chính job (xác minh vòng tròn) và trang bị chặn (nhà cái)
    assert search["urls"]["C001"] == ["https://www.theifab.com/laws/7"] and search["excluded_hosts"] == ["nguon-goc.vn"]
    assert search["urls"]["C002"] == [] and search["urls"]["C003"] == ["https://vnexpress.net/hiep-phu"]

    # Mỗi lượt P10-S một claim (claims_per_call = 1); chỉ claim có nguồn tải được mới gọi model
    assert [c["claims"] for c in calls] == [["C001"], ["C003"]] and {c["prompt_id"] for c in calls} == {"P10-S"}
    mark = datamark_char(ctx)
    assert 'source_url="https://www.theifab.com/laws/7"' in calls[0]["text"]
    assert "mỗi hiệp 45 phút" in undatamark(calls[0]["text"], mark)

    items = {it["claim_id"]: it for it in ctx.artifacts.latest(conn, ctx.job_id, "FactCheck").payload["items"]}
    assert list(items) == ["C001", "C002", "C003", "C004"]  # đủ mọi claim, đúng thứ tự, không có claim_id lạ
    assert items["C001"]["verdict"] == "DUNG" and items["C001"]["sources"][0]["url"] == "https://www.theifab.com/laws/7"
    assert items["C002"]["verdict"] == "KHONG_XAC_MINH_DUOC" and items["C002"]["conflict_note"] == "không tìm được nguồn độc lập"
    assert items["C004"]["verdict"] == "KHONG_XAC_MINH_DUOC" and items["C004"]["conflict_note"] == "không tải được nguồn nào"

    # Bước chạy lại: dùng lại kết quả tìm đã lưu, không gọi SERP provider lần nữa
    m1.factcheck(ctx)
    assert len(serp.queries) == 4


def test_search_budget_prioritises_blocking_claims(conn, config, blob_dir, monkeypatch):
    config["factcheck"] = {"mode": "search", "results_per_claim": 2, "max_searches": 2, "claims_per_call": 6}
    serp = FakeSerp()
    ctx = make_ctx(conn, config, blob_dir, serp)
    ctx.artifacts.save(conn, ctx.job_id, "FactInventory", {"claims": [
        claim("C001", "Hiệp phụ gồm hai hiệp, mỗi hiệp 15 phút", "ten_rieng"), *CLAIMS[:2],
        claim("C005", "Premier League có 42% người quan tâm")]}, "test")
    calls = []
    monkeypatch.setattr(m1, "_run", fake_p10s(calls))
    m1.factcheck(ctx)
    assert len(serp.queries) == 2 and all("Hiệp phụ" not in q for q in serp.queries)  # ten_rieng không thuộc loại chặn
    assert len(calls) <= 1  # các claim có nguồn đi chung một lượt P10-S


def test_model_mode_or_missing_provider_keeps_old_path(conn, config, blob_dir, monkeypatch):
    seen = []

    def run(sctx, prompt_id, variables, hashes, **kw):
        seen.append(prompt_id)
        return SimpleNamespace(parsed={"items": []}, run_id=None)

    monkeypatch.setattr(m1, "_run", run)
    config["factcheck"] = {"mode": "model"}
    m1.factcheck(make_ctx(conn, config, blob_dir, FakeSerp()))
    config["factcheck"] = {"mode": "search"}
    m1.factcheck(make_ctx(conn, config, blob_dir, None))  # không có SERP provider (adapters.serp.provider: manual)
    assert seen == ["P10", "P10"]


def test_near_quote_is_replaced_by_source_text_instead_of_dropping_the_source():
    """Model chép lệch 1 ký tự (đo thật: "Đorgu" thay cho "Dorgu") không được làm claim đúng thành không xác minh được."""
    from pipeline.llm.validators import _near_quote, _prune_fact_sources
    source = ("Tin nhanh. Dorgu và Cunha vẽ siêu phẩm, MU đánh bại Arsenal ở hiệp hai giúp MU lội ngược dòng hạ Arsenal 3-2, "
              "qua đó vươn lên top 4 Ngoại hạng Anh sau vòng 23. Tin khác.")
    quote = "Đorgu và Cunha vẽ siêu phẩm, MU đánh bại Arsenal ở hiệp hai giúp MU lội ngược dòng hạ Arsenal 3-2, qua đó vươn lên top 4"
    near = _near_quote(source, quote)
    assert near and near in source and near.startswith("orgu và Cunha")
    assert _near_quote(source, "MU thua Arsenal 0-5 trong một trận đấu hoàn toàn khác không có trong nguồn này") is None
    assert _near_quote(source, "MU hạ Arsenal") is None  # quote quá ngắn: không nới lỏng

    def item(q):
        return {"claim_id": "C1", "verdict": "DUNG", "sources": [{"url": "https://a.vn/x", "quote": q, "tier": 2, "date": ""}]}

    out, notes = _prune_fact_sources({"items": [item(quote)]}, {"provided_sources": {"C1": {"https://a.vn/x": source}}})
    assert out["items"][0]["verdict"] == "DUNG" and out["items"][0]["sources"][0]["quote"] == near and "lệch nhẹ" in notes[0]
    out, _ = _prune_fact_sources({"items": [item("Một câu bịa hoàn toàn không hề có trong văn bản nguồn đã tải về.")]},
                                 {"provided_sources": {"C1": {"https://a.vn/x": source}}})
    assert out["items"][0]["verdict"] == "KHONG_XAC_MINH_DUOC" and out["items"][0]["sources"] == []


def test_fix_over_edit_limit_applies_most_severe_edits_instead_of_dropping_all(conn, config, blob_dir, monkeypatch):
    """Đo thật: P9 sửa 29% số câu, vượt ngưỡng 15% → trước đây bỏ cả lô, lỗi chặn cũng không được sửa."""
    config["thresholds"]["fix_edit_ratio_max"] = 0.15
    ctx = make_ctx(conn, config, blob_dir, None)
    body = "<!-- S01 -->\n## Mục một\n" + " ".join(f"Câu số {i} của bài viết này." for i in range(1, 11)) + "\n"
    ctx.artifacts.save(conn, ctx.job_id, "ArticleDraft",
                       {"body_markdown": body, "keyword_placement": [], "can_nguoi": [], "change_log": []}, "test")
    items = [{"check_id": "P8c.3", "severity": "thap", "location": "S01", "evidence": "", "verdict": "NO", "fix_hint": ""},
             {"check_id": "P8d.1", "severity": "chan", "location": "S01", "evidence": "", "verdict": "NO", "fix_hint": ""},
             {"check_id": "P8a.3", "severity": "cao", "location": "S01", "evidence": "", "verdict": "NO", "fix_hint": ""}]
    edits = [{"check_id": "P8c.3", "error_type": 0, "find": "Câu số 2 của bài viết này.", "replace": "Câu hai đã sửa nhẹ.", "reason": ""},
             {"check_id": "P8d.1", "error_type": 0, "find": "Câu số 5 của bài viết này.", "replace": "Câu năm đã sửa số liệu sai.", "reason": ""},
             {"check_id": "P8a.3", "error_type": 0, "find": "Câu số 8 của bài viết này.", "replace": "Câu tám đã thêm trải nghiệm.", "reason": ""}]
    monkeypatch.setattr(m1, "_run", lambda sctx, pid, variables, hashes, **kw: SimpleNamespace(parsed={"edits": edits}, run_id=None))

    md, report = m1._apply_p9(ctx, items, "P9 sửa QA", "Edits:P9")
    # 3 edit = 30% số câu (kể cả heading: 11 câu → 27%) > 15%: chỉ lỗi chặn được áp
    assert "Câu năm đã sửa số liệu sai." in md and "Câu hai đã sửa nhẹ." not in md and "Câu tám đã thêm trải nghiệm." not in md
    assert report["applied"] == ["P8d.1"] and report["batch_rejected"] is False and report["over_limit_ratio"] > 0.15
    assert sorted(r[0] for r in report["rejected"]) == ["P8a.3", "P8c.3"] and {r[1] for r in report["rejected"]} == {"over_edit_ratio"}
    assert ctx.artifacts.latest(conn, ctx.job_id, "ArticleDraft").payload["body_markdown"] == md
    warn = ctx.artifacts.latest(conn, ctx.job_id, "Warnings").payload["items"][-1]
    assert warn["ai_action"] == "fix_partial" and "áp 1/3 edit" in warn["reason"]
