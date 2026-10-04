"""Tự học giọng văn dần từ nguồn SERP của từng job (config voice_learning) và tác giả là tổ chức trong JSON-LD."""
from types import SimpleNamespace

from pipeline import queue
from pipeline.artifacts import ArtifactStore, LocalBlobStore
from pipeline.authors import get_author, record_learned_voice, set_voice_profile, style_only, upsert_author
from pipeline.executor import StepContext
from pipeline.steps import m0
from vnseo import jsonld

SOURCE = "[Nguồn 01 | Kết quả bóng đá | https://a.vn]\n" + "Trận đấu kết thúc sau chín mươi phút thi đấu chính thức. " * 80


def profile(tag):
    return {
        "dimensions": [{"dim_id": f"D{i}", "name": f"chiều {i}", "description": f"{tag} mô tả {i}",
                        "evidence_quotes": ["Trận đấu kết thúc sau chín mươi phút"], "status": "DU_MAU"} for i in range(1, 11)],
        "positive_exemplars": [{"function": "mo_bai", "quote": "Trận đấu kết thúc sau chín mươi phút"}],
        "negative_exemplars_qa_only": ["câu của site khác"],
        "checklist_qa": [{"check_id": f"V{i}", "question": f"câu hỏi {i}?"} for i in range(1, 11)],
    }


def make_ctx(conn, config, blob_dir, author_id):
    job_id = queue.create_job(conn, topic="t", author_id=author_id, created_by="x", cost_cap_usd=3, branch="B", mode="standard")
    job = conn.execute("SELECT * FROM jobs WHERE id = %s", (job_id,)).fetchone()
    store = ArtifactStore(LocalBlobStore(blob_dir))
    return StepContext(conn, job, queue.job_state(job), config, store, "w1")


def fake_run(calls, tag):
    def run(sctx, prompt_id, variables, hashes, **kw):
        calls.append({"prompt_id": prompt_id, "feedback": kw.get("feedback"), "samples": variables["VOICE_SAMPLES_MD"]})
        return SimpleNamespace(parsed=profile(tag), run_id=None)
    return run


def test_style_only_drops_third_party_quotes():
    p = style_only(profile("x"))
    assert all(d["evidence_quotes"] == [] for d in p["dimensions"]) and len(p["dimensions"]) == 10
    assert p["positive_exemplars"] == [] and p["negative_exemplars_qa_only"] == [] and len(p["checklist_qa"]) == 10
    assert p["dimensions"][0]["description"] == "x mô tả 1"


def test_voice_is_learned_gradually_then_frozen(conn, config, blob_dir, monkeypatch):
    config["voice_learning"] = {"enabled": True, "max_jobs": 2, "max_sample_chars": 30000, "min_sample_words": 100}
    aid = upsert_author(conn, "Ban biên tập", kind="organization")
    art = SimpleNamespace(sha256="h")
    calls = []

    # Job 1: chưa có hồ sơ → học từ nguồn, không kèm hồ sơ cũ
    ctx = make_ctx(conn, config, blob_dir, aid)
    monkeypatch.setattr(m0, "_run", fake_run(calls, "lần 1"))
    learned, _ = m0._learn_voice(ctx, ctx.job, art, get_author(conn, aid), SOURCE)
    assert calls[0]["prompt_id"] == "P3" and calls[0]["feedback"] is None and "chín mươi phút" in calls[0]["samples"]
    assert learned["positive_exemplars"] == [] and learned["dimensions"][0]["evidence_quotes"] == []
    row = get_author(conn, aid)
    assert row["voice_origin"] == "serp" and row["voice_learned_from"] == [str(ctx.job_id)]
    assert row["voice_profile"]["dimensions"][0]["description"] == "lần 1 mô tả 1"
    assert ctx.artifacts.latest(conn, ctx.job_id, "Warnings").payload["items"][0]["ai_action"] == "learned"

    # Bước chạy lại trong cùng job: không học lần hai
    assert m0._learn_voice(ctx, ctx.job, art, get_author(conn, aid), SOURCE) is None and len(calls) == 1

    # Job 2: học tiếp, hồ sơ hiện tại được đưa cho model để điều chỉnh
    ctx2 = make_ctx(conn, config, blob_dir, aid)
    monkeypatch.setattr(m0, "_run", fake_run(calls, "lần 2"))
    m0._learn_voice(ctx2, ctx2.job, art, get_author(conn, aid), SOURCE)
    assert "lần 1 mô tả 1" in calls[1]["feedback"] and "học từ 1 lượt trước" in calls[1]["feedback"]
    row = get_author(conn, aid)
    assert len(row["voice_learned_from"]) == 2 and row["voice_profile"]["dimensions"][0]["description"] == "lần 2 mô tả 1"

    # Job 3: đã đủ max_jobs → giữ nguyên hồ sơ
    ctx3 = make_ctx(conn, config, blob_dir, aid)
    assert m0._learn_voice(ctx3, ctx3.job, art, get_author(conn, aid), SOURCE) is None and len(calls) == 2


def test_no_learning_without_source_or_when_author_has_own_voice(conn, config, blob_dir, monkeypatch):
    config["voice_learning"] = {"enabled": True, "max_jobs": 5, "min_sample_words": 100}
    aid = upsert_author(conn, "Nguyễn An")
    calls = []
    monkeypatch.setattr(m0, "_run", fake_run(calls, "x"))
    ctx = make_ctx(conn, config, blob_dir, aid)
    art = SimpleNamespace(sha256="h")
    assert m0._learn_voice(ctx, ctx.job, art, get_author(conn, aid), None) is None            # không có nguồn SERP
    assert m0._learn_voice(ctx, ctx.job, art, get_author(conn, aid), "quá ngắn") is None      # mẫu quá ít
    assert m0._learn_voice(ctx, ctx.job, art, None, SOURCE) is None                           # tác giả không có trong bảng
    config["voice_learning"]["enabled"] = False
    assert m0._learn_voice(ctx, ctx.job, art, get_author(conn, aid), SOURCE) is None          # tắt trong config
    config["voice_learning"]["enabled"] = True

    own = profile("của tác giả")
    set_voice_profile(conn, aid, own, "https://site.vn/bai-1")
    assert m0._learn_voice(ctx, ctx.job, art, get_author(conn, aid), SOURCE) is None and calls == []
    # Hồ sơ dựng từ bài của chính tác giả không bị bản học từ SERP ghi đè
    record_learned_voice(conn, aid, profile("serp"), ctx.job_id, "x")
    row = get_author(conn, aid)
    assert row["voice_origin"] == "author" and row["voice_profile"] == own and row["voice_learned_from"] == []


def test_jsonld_author_type_for_organization():
    data = {"headline": "Tiêu đề", "author_name": "Ban biên tập", "author_url": None}
    assert jsonld.build(data)[0]["author"] == {"@type": "Person", "name": "Ban biên tập"}
    assert jsonld.build({**data, "author_type": "Organization"})[0]["author"] == {"@type": "Organization", "name": "Ban biên tập"}


def test_learning_mode_drops_mismatched_quotes_instead_of_failing():
    """Đo thật (job afcae0cd): nguồn SERP là trang bảng tỷ số, model chép dòng bảng lệch → P3 hỏng sau 2 lần repair,
    job kẹt ở FACT_VOICE_FANOUT. Ở chế độ học, câu trích lệch chỉ bị bỏ (đằng nào cũng không được lưu)."""
    from pipeline.llm.validators import validate_output
    from pipeline.prompts import load_registry, load_schemas
    spec, schema = load_registry()["P3"], load_schemas()["VoiceProfile"]
    p = profile("x")
    p["positive_exemplars"] = [{"function": f, "quote": "Trận đấu kết thúc sau chín mươi phút"}
                               for f in ("mo_bai", "giai_thich", "vi_du", "chuyen_y", "ket_bai")]
    for d in p["dimensions"]:
        d["evidence_quotes"] = ["Trận đấu kết thúc sau chín mươi phút", "thi đấu chính thức"]
    p["dimensions"][9]["evidence_quotes"] = ["| NGÀY GIỜ | TRẬN ĐẤU | KẾT QUẢ | dòng bảng chép lệch", "thi đấu chính thức"]
    text = __import__("json").dumps(p, ensure_ascii=False)

    strict = validate_output(text, schema, spec.validators, {"source_text": SOURCE})
    assert not strict.ok and any("dimensions[9].evidence_quotes" in e for e in strict.errors)

    lenient = validate_output(text, schema, spec.validators, {"source_text": SOURCE, "voice_learning": True})
    assert lenient.ok, lenient.errors
    d10 = lenient.parsed["dimensions"][9]
    assert d10["evidence_quotes"] == ["thi đấu chính thức"] and d10["status"] == "KHONG_DU_MAU"
    assert lenient.parsed["dimensions"][0]["status"] == "DU_MAU"


def test_failed_learning_does_not_block_the_job(conn, config, blob_dir, monkeypatch):
    from pipeline.llm.runner import InvalidOutput
    config["voice_learning"] = {"enabled": True, "max_jobs": 5, "min_sample_words": 100}
    aid = upsert_author(conn, "Ban biên tập", kind="organization")
    ctx = make_ctx(conn, config, blob_dir, aid)

    def boom(sctx, prompt_id, variables, hashes, **kw):
        assert kw["ctx"]["voice_learning"] is True
        raise InvalidOutput("P3: output sai sau 2 lần repair")

    monkeypatch.setattr(m0, "_run", boom)
    assert m0._learn_voice(ctx, ctx.job, SimpleNamespace(sha256="h"), get_author(conn, aid), SOURCE) is None
    assert get_author(conn, aid)["voice_profile"] is None and get_author(conn, aid)["voice_learned_from"] == []
    assert ctx.artifacts.latest(conn, ctx.job_id, "Warnings").payload["items"][-1]["ai_action"] == "learn_failed"
