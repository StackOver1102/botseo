"""Chạy một job nhánh A (Standard) qua toàn bộ bước M0 với LLM giả, gate ở chế độ auto."""
import json
import re
from datetime import date

import pytest

from knowledge.repository import EntryInput, import_entry
from pipeline.artifacts import ArtifactStore, LocalBlobStore
from pipeline.config import load_config
from pipeline.executor import Worker, human_resolve
from pipeline.inputs import JobInput, create_job_with_input
from pipeline.llm.base import LLMResult
from pipeline.prompts import load_registry, load_schemas, load_shared
from pipeline.steps import HANDLERS, Services
from pipeline.urlcheck import UrlCheck

ARTICLE = ("## Vay tiền online là gì\n"
           "Vay tiền online là hình thức vay qua ứng dụng. Năm 2024, lãi suất khoảng 4,8%/năm.\n\n"
           "## Rủi ro khi vay\nMình đã thử vay qua app và thấy phí ẩn khá nhiều.\n")
TITLE = "Vay tiền online an toàn: 7 cách kiểm tra app trước khi vay"
META = ("Hướng dẫn kiểm tra app vay tiền online an toàn: giấy phép, lãi suất, phí ẩn và "
        "các dấu hiệu lừa đảo cần tránh trước khi ký hợp đồng.")


def job_input(with_serp=True):
    return JobInput(
        topic="vay tiền online", brief="bài hướng dẫn", article_md=ARTICLE,
        keywords=[{"keyword": "Vay tiền online", "volume": 1000}, {"keyword": "vay tiền online có an toàn không", "volume": 200}],
        serp={"organic": [{"rank": 1, "url": "https://a.vn/vay", "title": "Vay online", "snippet": "Hướng dẫn vay"}],
              "paa": ["Vay online có an toàn không?"], "aio_present": False} if with_serp else None,
        internal_pages=[{"url": "https://site.vn/lai-suat", "title": "Lãi suất"}],
        author={"name": "An", "url": "https://site.vn/an"},
    )


def gate_review(req):
    checklist = req.user.split("<checklist>")[1].split("</checklist>")[0]
    ids = re.findall(r'"check_id": "(\w+)"', checklist)
    gate = re.search(r"chốt (\w+)", req.system).group(1)
    return {"gate": gate, "checks": [{"check_id": i, "evidence": "KHÔNG TÌM THẤY", "verdict": "YES"} for i in ids],
            "decision": "approve", "confidence": 0.95, "revise_instructions": "", "escalate_reason": ""}


def voice_profile():
    q = ["Vay tiền online là hình thức vay qua ứng dụng.", "Mình đã thử vay qua app"]
    return {"dimensions": [{"dim_id": f"D{i}", "name": f"d{i}", "description": "x", "evidence_quotes": q, "status": "DU_MAU"}
                           for i in range(1, 11)],
            "positive_exemplars": [{"function": "mo_bai", "quote": q[0]}], "negative_exemplars_qa_only": [],
            "checklist_qa": [{"check_id": f"V{i}", "question": "?"} for i in range(1, 11)]}


def gap_plan():
    return {"coverage": [{"url": "https://a.vn/vay", "subtopics": ["định nghĩa"]}], "dominant_format": "hướng dẫn",
            "commodity": ["định nghĩa"], "gaps": [{"gap": "chi phí thật", "evidence": "PAA"}],
            "ig": [{"ig_id": "IG01", "type": "trai_nghiem", "description": "chi phí thực tế khi vay qua app", "owner": "KHO_TAC_GIA",
                    "inputs_needed": "trải nghiệm vay app phí", "placement": "S02"},
                   {"ig_id": "IG02", "type": "du_lieu_rieng", "description": "lãi suất trung bình", "owner": "AI",
                    "inputs_needed": "số liệu", "placement": "S01"}],
            "new_claims": [{"verbatim": "Lãi suất trung bình 5,1%/năm", "source_url": "https://sbv.gov.vn/x",
                            "section_id": "S01", "type": "so_lieu", "ig_id": "IG02"}],
            "outline": [{"section_id": "S01", "level": 2, "heading": "Vay tiền online là gì", "keywords": ["vay tiền online"],
                         "fact_ids": ["C001"], "ig_ids": ["IG02"], "source_sections": ["O01"]},
                        {"section_id": "S02", "level": 2, "heading": "Chi phí thực tế", "keywords": [], "fact_ids": [],
                         "ig_ids": ["IG01"], "source_sections": ["O02"]}]}


# Nhánh A: writer viết từng section từ đoạn gốc (P6-A); dài hơn đoạn gốc, câu chữ mới
P6A = {"S01": "<!-- S01 -->\n## Vay tiền online là gì\nVay tiền online là vay qua app. Đây là cách người dùng đăng ký khoản "
              "vay ngay trên điện thoại thay vì ra quầy giao dịch. Lãi suất trung bình 5,1%/năm.\n\n",
       "S02": "<!-- S02 -->\n## Chi phí thực tế\nMình từng trả phí thẩm định 300k khi đăng ký khoản vay qua ứng dụng, một "
              "khoản không được nhắc rõ lúc đầu. Tóm lại, cần đọc kỹ hợp đồng.\n"}


def fake_outputs(req):
    pid = req.prompt_id
    if pid == "B0-H":
        return {"branch_suggest": "A", "ymyl": False, "mode_suggest": "standard", "evidence": ["vay"]}
    if pid == "P1":
        rows = json.loads(req.user.split("<tu_khoa_da_chuan_hoa>")[1].split("</tu_khoa_da_chuan_hoa>")[0])
        return {"rows": [{**r, "intent": "thong_tin", "cluster": "vay", "role": "primary" if i == 0 else "secondary",
                          "section": "", "note": ""} for i, r in enumerate(rows)], "entities": ["app vay"], "questions_for_human": []}
    if pid == "G":
        return gate_review(req)
    if pid == "P2a":
        return {"claims": [{"claim_id": "C001", "verbatim": "lãi suất khoảng 4,8%/năm", "type": "so_lieu", "in_article_source": "",
                            "maybe_outdated": True, "outdated_reason": "năm 2024", "action": "CAP_NHAT", "new_value": None,
                            "verification_source": None, "status": "CHUA_DUYET", "origin": "bai_goc"}]}
    if pid == "P2b":
        return {"claims": []}
    if pid == "P3":
        return voice_profile()
    if pid == "P4-G":
        return gap_plan()
    if pid == "P5":
        eid = re.search(r'"entry_id": "(K\d+)"', req.user).group(1)
        return {"selections": [{"ig_id": "IG01", "entry_id": eid, "excerpt": "phí thẩm định 300k",
                                "fit_reason": "khớp", "confidence": "cao"}], "unfilled_ig_ids": []}
    if pid == "P10-S":
        ids = re.findall(r'<claim claim_id="(C\d+)"', req.user)
        return {"items": [{"claim_id": i, "atomic_claim": "x", "verdict": "DUNG", "current_value": "",
                           "sources": [{"url": "https://sbv.gov.vn/x", "date": "2026-09-01",
                                        "quote": "lãi suất trung bình 5,1%/năm", "tier": 1}],
                           "conflict_note": "", "suggested_action": ""} for i in ids]}
    if pid == "P10":
        ids = re.findall(r'"claim_id": "(C\d+)"', req.user)
        return {"items": [{"claim_id": i, "atomic_claim": "x", "verdict": "DUNG", "current_value": "",
                           "sources": [{"url": "https://sbv.gov.vn/x", "date": "2026-09-01", "quote": "5,1%", "tier": 1}],
                           "conflict_note": "", "suggested_action": ""} for i in ids]}
    if pid == "PF":
        return {"issues": []}
    if pid == "P6-S":
        body = ("<!-- S01 -->\n## Vay tiền online là gì\nVay tiền online là vay qua app. Lãi suất trung bình 5,1%/năm.\n\n"
                "<!-- S02 -->\n## Chi phí thực tế\nMình từng trả phí thẩm định 300k. Tóm lại, cần đọc kỹ hợp đồng.\n")
        return {"body_markdown": body, "keyword_placement": [{"keyword": "vay tiền online", "section_id": "S01"}],
                "can_nguoi": [], "change_log": ["viết mới"]}
    if pid == "P6-A":
        sid = re.search(r"Viết section (S\d+)", req.user).group(1)
        return {"body_markdown": P6A[sid], "keyword_placement": [{"keyword": "vay tiền online", "section_id": sid}] if sid == "S01" else [],
                "can_nguoi": [], "change_log": ["làm mới"]}
    if pid == "P7a":
        return {"titles": [TITLE, "a", "b", "c", "d"], "metas": [META, "x", "y"], "slug": "vay-tien-online-an-toan",
                "image_plan": [], "internal_links": [{"anchor": "lãi suất", "target_url": "https://site.vn/lai-suat", "section_id": "S01"}],
                "jsonld_data": {"headline": TITLE, "author_name": "An", "author_url": None, "date_published": None,
                                "date_modified": None, "breadcrumb": []}}
    if pid == "P7b":
        return {"edits": [{"check_id": "P7b.1", "error_type": 3, "find": "Tóm lại, cần đọc kỹ hợp đồng.",
                           "replace": "Bạn nhớ đọc kỹ hợp đồng.", "reason": "cụm sáo"}]}
    if pid in ("P8a", "P8b", "P8c", "P8d"):
        return verdict(pid, req)
    if pid == "P9":
        items = json.loads(req.user.split("<loi_can_sua>")[1].split("</loi_can_sua>")[0])
        edits = [{"check_id": it["check_id"], "error_type": 0, "find": "Vay tiền online là vay qua app.",
                  "replace": "Vay tiền online là hình thức vay qua ứng dụng điện thoại, không cần ra quầy.", "reason": "mở bài"}
                 for it in items if it["check_id"] == "P8c.1"]
        return {"edits": edits}
    raise AssertionError(f"prompt chưa có output giả: {pid}")


FIXED_INTRO = "hình thức vay qua ứng dụng điện thoại"


def verdict(pid, req):
    if pid == "P8b":
        ids = re.findall(r'"check_id": "(P8b\.V\d+)"', req.user) + ["P8b.B1", "P8b.B2"]
    else:
        ids = {"P8a": [f"P8a.{i}" for i in range(1, 5)], "P8c": [f"P8c.{i}" for i in range(1, 6)],
               "P8d": ["P8d.1", "P8d.2", "P8d.3"]}[pid]
    items = []
    for cid in ids:
        # P8c.1 (đoạn mở trả lời ý định) trượt cho tới khi P9 viết lại câu mở
        bad = cid == "P8c.1" and FIXED_INTRO not in req.user
        items.append({"check_id": cid, "evidence": "KHÔNG TÌM THẤY", "verdict": "NO" if bad else "YES",
                      "severity": "chan" if bad else "thap", "fix_hint": "mở bài trả lời trực tiếp" if bad else "",
                      "location": "S01"})
    return {"pass_id": pid, "items": items, "blocking_fail": False}


class FakeLLM:
    def __init__(self):
        self.calls = []

    def complete(self, req, key):
        self.calls.append((req.prompt_id, req.provider))
        return LLMResult(text=json.dumps(fake_outputs(req), ensure_ascii=False), stop_reason="end_turn", model=req.model)


@pytest.fixture
def setup(conn, blob_dir):
    def make(gate_mode="auto", with_serp=True, serp_provider=None, full=False, autopilot=False):
        config = load_config(overrides={
            "automation": {"enabled": autopilot},
            "gap_research": {"enabled": False},  # có test riêng (test_gap_research); ở đây SERP provider chỉ được gọi cho SERP_INPUT
            "gates": {"mode": {g: gate_mode for g in ("triage", "kw", "fact", "outline", "qa", "fact_signoff", "final")}},
            # bài test chỉ 6 câu: 1 câu sửa = 17%
            "thresholds": {"max_edit_ratio": 0.2, "fix_edit_ratio_max": 0.5},
        })
        fake = FakeLLM()
        services = Services(load_registry(), load_schemas(), load_shared(), lambda name, job, step: fake,
                            lambda url, quote: UrlCheck(url, True, 200, url, True, 1.0), serp_provider,
                            lambda url: (200, "Theo báo cáo mới, lãi suất trung bình 5,1%/năm cho vay tiêu dùng."))
        artifacts = ArtifactStore(LocalBlobStore(blob_dir))
        import_entry(conn, EntryInput(author_id="an", kind="trai_nghiem", title="Phí khi vay qua app",
                                      content="Tháng 5/2026 mình vay qua app và mất phí thẩm định 300k.",
                                      topic_tags=["vay app", "phí"], valid_until=date(2027, 12, 31),
                                      facts=[{"label": "phí thẩm định", "value": "300", "unit": "k", "as_of": "2026-05"}]))
        job = create_job_with_input(conn, artifacts, job_input(with_serp), author_id="an", created_by="ed1", config=config)
        # Test M0 dừng ở QA_FANOUT (bỏ handler M1); full=True chạy cả M1
        handlers = HANDLERS if full else {k: v for k, v in HANDLERS.items() if k.value not in M1_STATES}
        worker = Worker(conn, config, artifacts, handlers, services=services)
        return config, fake, artifacts, job, worker
    return make


M1_STATES = {"QA_FANOUT", "RUBRIC", "FIX", "QA_RERUN_FAILED", "QA_GATE", "FACTCHECK", "FACT_SIGNOFF", "FACT_APPLY",
             "FACT_RECHECK", "ASSEMBLE", "FINAL_GATE", "PUBLISH", "GSC_28D"}


def state(conn, job):
    return conn.execute("SELECT state, needs_human_reason, resume_state, branch, mode FROM jobs WHERE id = %s", (job,)).fetchone()


def test_branch_a_runs_through_m0(conn, setup):
    config, fake, artifacts, job, worker = setup("auto")
    worker.run_until_idle()
    st = state(conn, job)
    # Hết M0 (test bỏ handler M1)
    assert st["state"] == "NEEDS_HUMAN" and st["resume_state"] == "QA_FANOUT", st
    assert (st["branch"], st["mode"]) == ("A", "standard")

    path = [r["to_state"] for r in conn.execute(
        "SELECT to_state FROM job_transitions WHERE job_id = %s ORDER BY id", (job,)).fetchall()]
    assert path[:17] == ["TRIAGE_GATE", "KW_NORMALIZE", "KW_MAP_AI", "KW_GATE", "FACT_VOICE_FANOUT", "FACT_GATE",
                         "SERP_INPUT", "GAP_IG_AI", "KNOWLEDGE_SELECT", "NEW_FACT_CHECK", "OUTLINE_GATE", "PREFLIGHT",
                         "WRITE", "META_EDIT", "TITLE_GATE", "UNIQUENESS", "QA_FANOUT"]

    latest = lambda kind: artifacts.latest(conn, job, kind).payload  # noqa: E731
    facts = {c["claim_id"]: c for c in latest("FactInventory")["claims"]}
    assert facts["C001"]["action"] == "XAC_MINH" and facts["C001"]["status"] == "DA_DUYET"
    assert facts["C002"]["origin"] == "kho_tac_gia" and facts["C003"]["origin"] == "nghien_cuu_b4"
    assert latest("NewFactCheck")["verified_ig_ids"] == ["IG02"]
    assert "Tóm lại" not in latest("ArticleDraft")["body_markdown"]  # P7b đã áp
    assert latest("TitleChoice")["title"] == TITLE

    # Gate khác họ với model tạo artifact (ADR-5); TRIAGE_GATE dùng model thứ hai khác họ
    providers = dict(fake.calls)
    gate_calls = [p for pid, p in fake.calls if pid == "G"]
    # KW gate: KeywordMap do ChatGPT tạo → gate đổi họ, ưu tiên Gemini (web, miễn phí) hơn Claude API
    assert gate_calls[:1] == ["gemini"]
    assert [p for pid, p in fake.calls if pid == "B0-H"] == ["gemini", "openai"]
    assert providers["P6-A"] == "anthropic"  # nhánh A: viết từng section từ đoạn gốc
    decisions = conn.execute("SELECT gate, actor, action FROM gate_decisions WHERE job_id = %s ORDER BY created_at", (job,)).fetchall()
    assert [(d["gate"], d["action"]) for d in decisions] == [
        ("TRIAGE", "approve"), ("KW", "approve"), ("FACT", "approve"), ("OUTLINE", "approve"), ("TITLE", "approve")]


def test_rerun_uses_cache(conn, setup):
    config, fake, artifacts, job, worker = setup("auto")
    worker.run_until_idle()
    n_calls = len(fake.calls)
    human_resolve(conn, job, "ed1", "rerun", "thử chạy lại bước QA", config)
    conn.execute("UPDATE jobs SET state = 'WRITE' WHERE id = %s", (job,))  # tua lại để kiểm tra cache
    worker.run_until_idle()
    assert [c for c in fake.calls[n_calls:] if c[0] in ("P6-S", "P6-A")] == []


def test_shadow_mode_waits_for_human(conn, setup):
    config, fake, artifacts, job, worker = setup("shadow")
    worker.run_until_idle()
    st = state(conn, job)
    assert st["state"] == "NEEDS_HUMAN" and st["resume_state"] == "TRIAGE_GATE"
    assert st["needs_human_reason"].startswith("SHADOW_GATE:triage")
    human_resolve(conn, job, "ed1", "approve", "đồng ý phân loại", config)
    worker.run_until_idle()
    assert state(conn, job)["resume_state"] == "KW_GATE"
    d = conn.execute("SELECT actor, mode FROM gate_decisions WHERE job_id = %s AND gate = 'KW'", (job,)).fetchall()
    assert {"actor": "ai", "mode": "shadow"} in d


class FakeSerp:
    def __init__(self):
        self.queries = []

    def fetch(self, keyword):
        from pipeline.inputs import SerpIn
        self.queries.append(keyword)
        return SerpIn(organic=[{"rank": 1, "url": "https://a.vn/vay", "title": "Vay online", "snippet": "x"}],
                      paa=["Vay online có an toàn không?"], provider="serper")


def test_serp_fetched_when_not_uploaded(conn, setup):
    fake_serp = FakeSerp()
    config, fake, artifacts, job, worker = setup("auto", with_serp=False, serp_provider=fake_serp)
    worker.run_until_idle()
    assert fake_serp.queries == ["Vay tiền online"]  # keyword primary
    assert artifacts.latest(conn, job, "SerpSnapshot").payload["provider"] == "serper"
    assert state(conn, job)["resume_state"] == "QA_FANOUT"


def test_serp_missing_without_provider(conn, setup):
    config, fake, artifacts, job, worker = setup("auto", with_serp=False, serp_provider=None)
    worker.run_until_idle()
    st = state(conn, job)
    assert st["resume_state"] == "SERP_INPUT" and st["needs_human_reason"].startswith("SERP_MISSING")


def test_ai_escalate_proceeds_with_warning(conn, setup, monkeypatch):
    """on_ai_escalate = proceed: AI gate escalate (luật cứng đã pass) không làm dừng job, chỉ ghi Warnings."""
    real = gate_review

    def escalate_outline(req):
        out = real(req)
        if out["gate"] == "OUTLINE":
            out.update(decision="escalate", escalate_reason="PAA chưa trả lời hết")
        return out

    import tests.test_m0_e2e as mod
    monkeypatch.setattr(mod, "gate_review", escalate_outline)
    config, fake, artifacts, job, worker = setup("auto")
    worker.run_until_idle()
    assert state(conn, job)["resume_state"] == "QA_FANOUT"  # đi hết M0, không dừng ở OUTLINE_GATE
    warnings = artifacts.latest(conn, job, "Warnings").payload["items"]
    assert any(w["gate"] == "outline" and "PAA" in w["reason"] for w in warnings)
    d = conn.execute("SELECT action FROM gate_decisions WHERE job_id = %s AND gate = 'OUTLINE'", (job,)).fetchone()
    assert d["action"] == "escalate"  # quyết định thật của AI vẫn được ghi để audit


def test_rewind_reuses_cache_for_earlier_steps(conn, setup):
    from pipeline.executor import rewind_job
    from pipeline.states import State
    config, fake, artifacts, job, worker = setup("auto")
    worker.run_until_idle()
    n = len(fake.calls)
    rewind_job(conn, job, State.NEW_FACT_CHECK, "ed1", "thử prompt mới")
    worker.run_until_idle()
    again = [pid for pid, _ in fake.calls[n:]]
    assert "P1" not in again and "P3" not in again  # không chạy lại bước trước
    assert state(conn, job)["resume_state"] == "QA_FANOUT"


def test_partial_claim_kept_as_corrected_text(conn, setup, monkeypatch):
    """P10-S xác nhận một phần (CAN_CAP_NHAT): giữ câu đã sửa theo nguồn, IG vẫn tính là có dữ liệu."""
    import tests.test_m0_e2e as mod
    real = mod.fake_outputs

    def partial(req):
        out = real(req)
        if req.prompt_id == "P10-S":
            for it in out["items"]:
                it.update(verdict="CAN_CAP_NHAT", current_value="Lãi suất trung bình khoảng 5,1%/năm")
        return out

    monkeypatch.setattr(mod, "fake_outputs", partial)
    config, fake, artifacts, job, worker = setup("auto")
    worker.run_until_idle()
    nfc = artifacts.latest(conn, job, "NewFactCheck").payload
    assert nfc["verified_ig_ids"] == ["IG02"] and nfc["items"][0]["used_text"].startswith("Lãi suất trung bình")
    claim = next(c for c in artifacts.latest(conn, job, "FactInventory").payload["claims"] if c["origin"] == "nghien_cuu_b4")
    assert claim["verbatim"] == "Lãi suất trung bình khoảng 5,1%/năm"



def test_full_pipeline_to_done(conn, setup):
    """M0 + M1: QA phát hiện đoạn mở yếu → P9 sửa → chấm lại → fact-check → lắp ráp → duyệt cuối → xuất bài."""
    config, fake, artifacts, job, worker = setup("auto", full=True)
    worker.run_until_idle()
    st = state(conn, job)
    assert st["state"] == "DONE", st
    path = [r["to_state"] for r in conn.execute(
        "SELECT to_state FROM job_transitions WHERE job_id = %s ORDER BY id", (job,)).fetchall()]
    assert path[path.index("QA_FANOUT"):] == ["QA_FANOUT", "RUBRIC", "FIX", "QA_RERUN_FAILED", "RUBRIC", "QA_GATE",
                                              "FACTCHECK", "FACT_SIGNOFF", "ASSEMBLE", "FINAL_GATE", "PUBLISH",
                                              "GSC_28D", "DONE"]
    latest = lambda kind: artifacts.latest(conn, job, kind).payload  # noqa: E731
    assert latest("FixLog")["rounds"] == {"P8c.1": 1}
    assert latest("RubricResult")["pass"] is True
    pkg = latest("FinalPackage")
    assert FIXED_INTRO in pkg["article_markdown"] and "<!-- S" not in pkg["article_markdown"]
    assert pkg["title"] == TITLE and pkg["open_placeholders"] == 0 and pkg["jsonld"][0]["headline"] == TITLE
    # Judge khác họ với writer (Claude); P8c chạy 2 lần (trước và sau khi sửa)
    judges = [p for pid, p in fake.calls if pid.startswith("P8")]
    assert "anthropic" not in judges and [pid for pid, _ in fake.calls].count("P8c") == 2
    assert conn.execute("SELECT cms_status FROM publications WHERE job_id = %s", (job,)).fetchone()["cms_status"] == "exported"


def test_multiple_sources_stored_and_joined_in_order(conn, config, tmp_path):
    from pipeline.artifacts import ArtifactStore, LocalBlobStore
    from pipeline.steps.common import source_text
    artifacts = ArtifactStore(LocalBlobStore(tmp_path))
    srcs = ["[Nguồn 02 | B | https://b.vn]\nbài hai", "[Nguồn 01 | A | https://a.vn]\nbài một"]
    job = create_job_with_input(conn, artifacts, JobInput(topic="t", branch="B", keywords=[{"keyword": "k"}]),
                                author_id="an", created_by="ed1", config=config, source_texts=srcs)
    ctx = type("Ctx", (), {"conn": conn, "job_id": job})()
    joined = source_text(ctx)
    assert joined.index("Nguồn 01") < joined.index("Nguồn 02") and "bài một" in joined and "bài hai" in joined
