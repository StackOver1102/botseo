"""Dữ liệu cho tab "Các bước" của UI: mỗi bước làm gì, đã gửi prompt nào, nhận kết quả gì, và CHỈNH Ở ĐÂU
(file prompt, khoá trong config.yaml, hàm code). Dành cho người quản lý theo dõi và điều chỉnh pipeline.
"""
from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

from pipeline.states import State

ROOT = Path(__file__).resolve().parent.parent

# state → (tên dễ hiểu, bước làm gì, prompt thường dùng, khoá config liên quan)
STEP_INFO: dict[str, tuple[str, str, list[str], list[str]]] = {
    "TRIAGE_AI": ("Phân loại đầu việc", "AI gợi ý nhánh (A: làm mới bài của site / B: bài mới từ tư liệu), mức (lite/standard/heavy) và có phải chủ đề nhạy cảm (YMYL) không.",
                  ["B0-H"], ["models.triage"]),
    "TRIAGE_GATE": ("Chốt phân loại", "Model thứ hai (khác hãng) phân loại lại; hai model khớp nhau thì đi tiếp, lệch thì chọn phía an toàn.",
                    ["B0-H"], ["models.triage_second", "gates.mode.triage", "cost.cap_usd_per_job"]),
    "KW_NORMALIZE": ("Chuẩn hoá từ khoá", "Code gộp từ khoá trùng (có dấu / không dấu), bỏ dòng rác. Không gọi AI.", [], []),
    "KW_MAP_AI": ("Phân vai từ khoá", "AI chọn 1 từ khoá chính, xếp từ khoá phụ vào từng mục, đánh dấu từ khoá nên có trang riêng.",
                  ["P1"], ["models.keyword_map", "thresholds.kw_map_rows_per_batch"]),
    "KW_GATE": ("Duyệt bảng từ khoá", "Code kiểm tra (đủ dòng, đúng 1 từ khoá chính) rồi AI khác hãng chấm theo checklist.",
                ["G"], ["models.gate", "gates.checklist.kw", "gates.revise_max", "gates.min_confidence"]),
    "FACT_VOICE_FANOUT": ("Trích dữ kiện + giọng văn", "AI liệt kê mọi số liệu, ngày, tên riêng trong bài gốc / bài nguồn (P2a, rà lại bằng P2b); lập hoặc học hồ sơ giọng văn (P3).",
                          ["P2a", "P2b", "P3"], ["models.fact_extract", "models.fact_recall", "models.voice", "voice_learning"]),
    "FACT_GATE": ("Duyệt bảng dữ kiện", "Code + AI kiểm tra mỗi dữ kiện đã có quyết định giữ / cập nhật / xác minh / bỏ.",
                  ["G"], ["models.gate", "gates.checklist.fact", "retention"]),
    "SERP_INPUT": ("Lấy kết quả Google", "Lấy top 10 Google cho từ khoá chính (Serper) hoặc dùng SERP nhập tay. Không gọi AI.",
                   [], ["adapters.serp"]),
    "GAP_IG_AI": ("Phân tích đối thủ + dàn ý", "Code tìm tài liệu nghiên cứu; AI tìm chỗ đối thủ còn thiếu, đề xuất điểm mới (IG), dữ kiện mới có nguồn và lập dàn ý.",
                  ["P4-G", "P4-D", "P4-DR"], ["models.gap_ig", "models.deep_research", "gap_research", "thresholds.ig_min", "modes"]),
    "KNOWLEDGE_SELECT": ("Chọn dữ liệu từ kho", "AI chọn mục trong kho dữ liệu của tác giả / site cho từng điểm mới; chỉ được trích nguyên văn.",
                         ["P5"], ["models.knowledge_select", "knowledge"]),
    "NEW_FACT_CHECK": ("Kiểm chứng dữ kiện mới", "Code tải trang nguồn của từng dữ kiện mới; AI đối chiếu câu chữ với trang đó trước khi cho dùng.",
                       ["P10-S"], ["models.factcheck_source"]),
    "OUTLINE_GATE": ("Duyệt dàn ý", "Code kiểm tra đủ điểm mới có dữ liệu thật, tham chiếu hợp lệ; AI chấm dàn ý theo checklist.",
                     ["G"], ["models.gate", "gates.checklist.outline", "gates.outline_autofix", "thresholds.ig_min"]),
    "PREFLIGHT": ("Kiểm tra trước khi viết", "Tìm mâu thuẫn giữa dữ kiện, dàn ý và dữ liệu kho trước khi viết bài.",
                  ["PF"], ["models.preflight"]),
    "WRITE": ("Viết bài", "AI viết bài theo dàn ý, chỉ dùng dữ kiện đã duyệt và dữ liệu kho; theo hồ sơ giọng văn.",
              ["P6-S", "P6-H", "P6-A"], ["models.writer", "thresholds.writer_source_facts", "blacklist", "modes"]),
    "META_EDIT": ("Title, meta + sửa câu chữ", "AI đề xuất 5 title, 3 meta, slug, link nội bộ (P7a) và sửa hẹp lỗi chính tả / cụm sáo (P7b).",
                  ["P7a", "P7b"], ["models.meta", "models.fixer", "thresholds.max_edit_ratio", "thresholds.title_chars", "thresholds.meta_chars"]),
    "TITLE_GATE": ("Chọn title", "Code chọn title đạt độ dài, có từ khoá chính, không trùng title sẵn có. Không gọi AI.",
                   [], ["thresholds.title_chars", "thresholds.meta_chars", "thresholds.title_px_warn"]),
    "UNIQUENESS": ("Kiểm tra trùng lặp", "Code so bài với các bài nguồn (cụm 5 âm tiết). Nhánh B: trùng quá ngưỡng thì viết lại. Không gọi AI.",
                   [], ["thresholds.branch_b_containment_max", "thresholds.shingle_n"]),
    "QA_FANOUT": ("Chấm chất lượng", "AI khác hãng với AI viết chấm 4 phần: điểm mới (P8a), giọng văn (P8b), SEO (P8c), độ chính xác (P8d).",
                  ["P8a", "P8b", "P8c", "P8d"], ["models.judge", "models.judge_alt", "modes"]),
    "RUBRIC": ("Tổng hợp điểm", "Code gộp kết quả chấm thành 9 tiêu chí; trượt thì gửi sang bước sửa. Không gọi AI.",
               [], ["thresholds.rubric_pass_ratio", "thresholds.fix_rounds_per_check", "gates.rubric_fail_after_fix", "automation.quality_rewinds_max"]),
    "FIX": ("Sửa lỗi QA", "AI sửa đúng những lỗi bị chấm trượt, trong đúng mục được chỉ ra; code giới hạn tỷ lệ câu bị sửa.",
            ["P9"], ["models.fixer", "thresholds.fix_edit_ratio_max"]),
    "QA_RERUN_FAILED": ("Chấm lại phần vừa sửa", "Chỉ chấm lại các phần có lỗi vừa được sửa.", ["P8a", "P8b", "P8c", "P8d"], ["models.judge"]),
    "QA_GATE": ("Duyệt QA", "Code kiểm tra tỷ lệ câu bị sửa; AI xác nhận các sửa đổi không làm đổi nghĩa.",
                ["G"], ["models.gate_heavy", "gates.checklist.qa", "gates.on_ai_escalate"]),
    "FACTCHECK": ("Kiểm chứng dữ kiện", "Code tìm nguồn độc lập cho từng dữ kiện (Serper), tải trang; AI đối chiếu câu chữ với trang đã tải.",
                  ["P10-S", "P10"], ["factcheck", "models.factcheck_source", "models.factcheck"]),
    "FACT_SIGNOFF": ("Chốt kết quả kiểm chứng", "Code mở lại từng nguồn, quyết định dữ kiện nào giữ, sửa theo nguồn hay xoá. Không gọi AI.",
                     [], ["thresholds.fact_apply_min_tier", "gates.fact_unverified", "thresholds.blocking_claim_types"]),
    "FACT_APPLY": ("Sửa bài theo kiểm chứng", "AI sửa / xoá câu chứa dữ kiện sai; sau đó code tự dọn câu, dòng bảng còn số chưa kiểm chứng.",
                   ["P9"], ["models.fixer", "thresholds.fix_edit_ratio_max"]),
    "FACT_RECHECK": ("Soát số liệu lần cuối", "Code quét lại mọi con số trong bài so với bảng dữ kiện đã duyệt. Không gọi AI.",
                     [], ["thresholds.number_ignore_patterns"]),
    "ASSEMBLE": ("Ghép gói bài", "Code ghép bài, title, meta, JSON-LD, bảng dữ kiện; AI viết nhật ký thay đổi.",
                 ["P11"], ["models.changelog", "default_author"]),
    "FINAL_GATE": ("Duyệt cuối", "Code kiểm tra không còn chỗ trống, JSON-LD hợp lệ; AI chấm checklist cuối.",
                   ["G"], ["models.gate_heavy", "gates.checklist.final", "gates.ymyl_final_human"]),
    "PUBLISH": ("Xuất bài", "Ghi nhận bài đã sẵn sàng đăng (chưa nối CMS: đăng tay từ gói bài). Không gọi AI.",
                [], ["adapters.cms"]),
    "GSC_28D": ("Đo hiệu quả", "So sánh số liệu Search Console 28 ngày trước / sau (hiện nhập tay).", [], ["adapters.gsc"]),
    "REWORK": ("Viết lại bài", "Cắt bài cũ thành từng phần rồi AI viết lại từng phần, giữ nguyên số liệu và heading.",
               ["RW", "RWA"], ["models.rework", "models.rework_brief", "thresholds.rework_chunk_words"]),
    "REWORK_CHECK": ("Kiểm tra bản viết lại", "Code kiểm tra số heading, độ dài, số liệu và độ trùng với bản cũ; lỗi thì viết lại phần lỗi.",
                     [], ["thresholds.rework_length_ratio", "thresholds.rework_containment_max", "gates.revise_max"]),
}


def _lookup(config: dict, dotted: str) -> Any:
    node: Any = config
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def handler_location(state: str) -> str | None:
    """'pipeline/steps/m0.py:403 (gap_ig_ai)' — nơi sửa logic của bước."""
    from pipeline.steps import HANDLERS
    try:
        fn = HANDLERS.get(State(state))
    except ValueError:
        return None
    if fn is None:
        return None
    fn = inspect.unwrap(fn)
    try:
        path = Path(inspect.getsourcefile(fn)).resolve().relative_to(ROOT).as_posix()
        line = inspect.getsourcelines(fn)[1]
    except (OSError, TypeError, ValueError):
        return None
    return f"{path}:{line} ({fn.__name__})"


def prompt_edit_info(prompt_id: str, registry: dict, config: dict) -> dict | None:
    spec = registry.get(prompt_id)
    if spec is None:
        return None
    model = config.get("models", {}).get(spec.model_key) or {}
    return {
        "prompt_id": prompt_id, "file": f"prompts/{prompt_id}.yaml", "version": spec.version,
        "extends": spec.raw.get("extends"),
        "model_key": f"models.{spec.model_key}",
        "model": {k: model.get(k) for k in ("provider", "model", "effort", "thinking_level") if model.get(k) is not None},
        "provider_mode": config.get("providers", {}).get(model.get("provider"), {}).get("mode"),
        "output": spec.output, "validators": spec.validators,
    }


def build_steps(job: dict, transitions: list[dict], runs: list[dict], artifacts: list[dict], pipeline: list[str],
                registry: dict, config: dict) -> list[dict]:
    """Một mục cho mỗi bước của luồng, theo thứ tự pipeline; bước chưa tới vẫn hiện (để biết còn gì phía trước)."""
    current = job["resume_state"] if job["state"] == "NEEDS_HUMAN" else job["state"]
    steps = []
    for state in pipeline:
        if state == "DONE":
            continue
        title, what, default_prompts, config_keys = STEP_INFO.get(state, (state, "", [], []))
        mine = [t for t in transitions if t["from_state"] == state]
        step_runs = [r for r in runs if r["step"] == state]
        used = list(dict.fromkeys(r["prompt_id"] for r in step_runs))
        last = mine[-1] if mine else None
        if state == current and job["state"] == "NEEDS_HUMAN":
            status = "stopped"
        elif state == current and job["state"] not in ("DONE", "CANCELLED"):
            status = "running"
        elif mine:
            status = "done"
        else:
            status = "pending"
        steps.append({
            "state": state, "title": title, "what": what, "status": status, "visits": len(mine),
            "last": last and {"outcome": last["outcome"], "to_state": last["to_state"], "reason": last["reason"],
                              "actor": last["actor"], "at": last["created_at"]},
            "history": [{"outcome": t["outcome"], "to_state": t["to_state"], "reason": t["reason"], "at": t["created_at"]} for t in mine],
            "runs": step_runs,
            "cost_usd": sum(float(r["cost_usd"] or 0) for r in step_runs),
            "artifacts": [a for a in artifacts if a["produced_by"] == state],
            "edit": {
                "code": handler_location(state),
                "prompts": [p for p in (prompt_edit_info(pid, registry, config) for pid in (used or default_prompts)) if p],
                "config": [{"key": k, "value": _lookup(config, k)} for k in config_keys],
            },
        })
    return steps
