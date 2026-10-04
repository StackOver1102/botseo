"""Tiện ích dùng chung cho step handler: artifact, biến prompt, datamark, phản hồi revise, AI gate."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import psycopg

from gates.rules import RuleResult
from vnseo.vn_text.normalize import normalize

from ..artifacts import Artifact
from ..executor import GateDecision, StepContext, StepResult
from ..inputs import JobInput
from ..llm.runner import PromptRunner, ProviderFactory, make_provider_factory
from ..prompts import PromptSpec, load_registry, load_schemas, load_shared
from ..serp import SerpProvider, make_serp_provider
from ..states import REVISE_COUNTER, Outcome, State
from ..urlcheck import Fetcher, UrlVerifier, Verifier


@dataclass
class Services:
    registry: dict[str, PromptSpec]
    schemas: dict[str, dict]
    shared: dict[str, str]
    provider_factory: ProviderFactory
    url_verifier: Verifier
    serp_provider: SerpProvider | None = None
    url_fetcher: Fetcher | None = None  # tải text trang nguồn (NEW_FACT_CHECK đưa nguồn vào prompt P10-S)
    url_main_text: Fetcher | None = None  # nội dung chính của trang (tải lại bài nguồn auto_source)

    @classmethod
    def default(cls, conn: psycopg.Connection, config: dict) -> "Services":
        urls = UrlVerifier()  # một instance: cache trang và robots.txt dùng chung cho verify và fetch
        return cls(load_registry(), load_schemas(), load_shared(), make_provider_factory(conn, config),
                   urls.verify, make_serp_provider(config), urls.fetch_text, urls.main_text)


class StepFailed(Exception):
    """Dữ liệu vào không đủ để chạy bước — chuyển cho người với lý do cụ thể."""


def runner(ctx: StepContext) -> PromptRunner:
    s: Services = ctx.services
    return PromptRunner(ctx.conn, ctx.config, s.registry, s.schemas, s.shared, ctx.artifacts.blobs, s.provider_factory)


def latest(ctx: StepContext, kind: str, required: bool = True) -> Artifact | None:
    art = ctx.artifacts.latest(ctx.conn, ctx.job_id, kind)
    if art is None and required:
        raise StepFailed(f"thiếu artifact {kind}")
    return art


def save(ctx: StepContext, kind: str, payload: Any, run_id=None) -> Artifact:
    return ctx.artifacts.save(ctx.conn, ctx.job_id, kind, payload, ctx.state.state.value, run_id=run_id)


def job_input(ctx: StepContext) -> tuple[JobInput, Artifact]:
    art = latest(ctx, "JobInput")
    return JobInput.model_validate(art.payload), art


def job_row(ctx: StepContext) -> dict:
    """Đọc lại dòng job (branch/mode/ymyl có thể vừa được TRIAGE_GATE cập nhật)."""
    return ctx.conn.execute("SELECT * FROM jobs WHERE id = %s", (ctx.job_id,)).fetchone()


def producer_family(ctx: StepContext, art: Artifact) -> set[str]:
    row = ctx.conn.execute(
        "SELECT pr.provider FROM artifacts a JOIN prompt_runs pr ON pr.id = a.produced_by_run_id WHERE a.id = %s",
        (art.id,)).fetchone()
    return {row["provider"]} if row else set()


def source_text(ctx: StepContext) -> str | None:
    """Nhiều bài nguồn (auto_source) được nối lại, mỗi bài mở đầu bằng header [Nguồn NN | title | url]."""
    rows = ctx.conn.execute("SELECT text FROM source_texts WHERE job_id = %s ORDER BY created_at, text",
                            (ctx.job_id,)).fetchall()
    return "\n\n".join(r["text"] for r in rows) if rows else None


# ---------- datamark (ADR-8, spotlighting) ----------

_MARKS = ["ˆ", "¦", "¤", "¸", "˜", "¨"]


def datamark_char(ctx: StepContext) -> str:
    """Cố định theo job (để cache prompt ổn định), khác nhau giữa các job."""
    return _MARKS[int(hashlib.sha256(str(ctx.job_id).encode()).hexdigest(), 16) % len(_MARKS)]


def datamark(text: str, mark: str) -> str:
    return mark.join(text.split())


def undatamark(text: str, mark: str) -> str:
    return text.replace(mark, " ")


def base_vars(ctx: StepContext) -> dict[str, Any]:
    loc = ctx.config["locale"]
    return {
        "TONE_STYLE": "mới (hoà, thuỷ)" if loc["tone_style"] == "new" else "cũ (hòa, thủy)",
        "TONE_EXAMPLE": loc["tone_example"],
        "DATAMARK": datamark_char(ctx),
    }


def to_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


def sha(art: Artifact | None) -> str:
    return art.sha256 if art else "-"


def nfc(s: str) -> str:
    return normalize(s, keep_newlines=True)


# ---------- phản hồi cho vòng revise ----------

def save_feedback(ctx: StepContext, counter_key: str, text: str) -> None:
    rnd = ctx.state.revise_count.get(counter_key, 0) + 1
    save(ctx, f"Feedback:{counter_key}", {"round": rnd, "text": text})


def feedback_for(ctx: StepContext, counter_key: str) -> str | None:
    """Phản hồi chỉ áp dụng cho đúng lượt revise vừa được tính."""
    art = latest(ctx, f"Feedback:{counter_key}", required=False)
    if art and art.payload["round"] == ctx.state.revise_count.get(counter_key, 0):
        return art.payload["text"]
    return None


# ---------- AI gate (ADR-9) ----------

def gate_mode(ctx: StepContext, gate: str) -> str:
    return ctx.config["gates"]["mode"].get(gate, "human")


def _revise_left(ctx: StepContext, counter_key: str | None) -> int:
    if counter_key is None:
        return 0
    return ctx.config["gates"]["revise_max"].get(counter_key, 0) - ctx.state.revise_count.get(counter_key, 0)


def finish_gate(ctx: StepContext, gate: str, action: str, reason: str, decisions: list[GateDecision],
                counter_key: str | None = None, allow_proceed: bool = False) -> StepResult:
    """Áp chế độ gate: auto → theo quyết định; shadow/human → luôn chờ người (quyết định AI chỉ để đo).

    allow_proceed: quyết định escalate là phán đoán của AI (luật cứng đã pass). Nếu config
    gates.on_ai_escalate = proceed thì job đi tiếp, lý do được ghi vào artifact Warnings để xem lại ở bài cuối.
    """
    mode = gate_mode(ctx, gate)
    if mode != "auto":
        return StepResult(Outcome.ESCALATE, f"{mode.upper()}_GATE:{gate} (AI: {action})", decisions=decisions)
    if action == "approve":
        return StepResult(Outcome.APPROVE, decisions=decisions)
    if action == "revise" and counter_key and _revise_left(ctx, counter_key) > 0:
        save_feedback(ctx, counter_key, reason)
        return StepResult(Outcome.REVISE, reason, decisions=decisions)
    if allow_proceed and ctx.config["gates"].get("on_ai_escalate", "human") == "proceed":
        add_warning(ctx, gate, action, reason)
        return StepResult(Outcome.APPROVE, f"đi tiếp dù AI {action}: {reason}", decisions=decisions)
    return StepResult(Outcome.ESCALATE, reason, decisions=decisions)


def add_warning(ctx: StepContext, gate: str, action: str, reason: str) -> None:
    """Cảnh báo tích luỹ theo job; FINAL_GATE/biên tập viên xem lại trước khi publish."""
    art = latest(ctx, "Warnings", required=False)
    items = list(art.payload["items"]) if art else []
    items.append({"gate": gate, "ai_action": action, "reason": reason, "state": ctx.state.state.value})
    save(ctx, "Warnings", {"items": items})


def run_gate(ctx: StepContext, gate: str, artifact_kind: str, artifact_payload: Any, context: dict,
             rule: RuleResult, producer_families: set[str], model_key: str = "gate",
             skip_checks: frozenset[str] = frozenset()) -> StepResult:
    """Luật cứng của code trước; pass thì prompt G (model khác họ) chấm checklist."""
    counter_key = REVISE_COUNTER.get(ctx.state.state)
    mode = gate_mode(ctx, gate)
    if not rule.passed:
        # Luật cứng là code, không phải AI: tự revise/escalate ở mọi chế độ gate
        action = "escalate" if rule.force_escalate or _revise_left(ctx, counter_key) <= 0 else "revise"
        reason = "Luật cứng chưa đạt:\n- " + "\n- ".join(rule.failures)
        d = GateDecision(gate.upper(), "code", mode, action, reason=reason, rule_failures=rule.failures)
        if action == "revise":
            save_feedback(ctx, counter_key, reason)
            return StepResult(Outcome.REVISE, reason, decisions=[d])
        return StepResult(Outcome.ESCALATE, reason, decisions=[d])

    if mode == "human":
        return StepResult(Outcome.ESCALATE, f"HUMAN_GATE:{gate}")

    # skip_checks: check code đã quyết định đi tiếp (ghi Warnings) — không để AI revise lại vì nó
    checklist = [c for c in ctx.config["gates"]["checklist"][gate] if c["check_id"] not in skip_checks]
    revise_max = ctx.config["gates"]["revise_max"].get(counter_key, 0) if counter_key else 0
    artifact_json, context_json = to_json(artifact_payload), to_json(context)
    variables = {
        **base_vars(ctx), "GATE": gate.upper(), "ARTIFACT_KIND": artifact_kind, "ARTIFACT_JSON": artifact_json,
        "GATE_CONTEXT_JSON": context_json, "RULE_RESULTS_JSON": to_json({"passed": True, "data": rule.data}),
        "GATE_CHECKLIST_JSON": to_json(checklist),
        "REVISE_COUNT": ctx.state.revise_count.get(counter_key, 0) if counter_key else 0, "REVISE_MAX": revise_max,
    }
    out = runner(ctx).run(
        job_id=ctx.job_id, step=ctx.state.state.value, prompt_id="G", variables=variables,
        input_hashes=[hashlib.sha256(artifact_json.encode()).hexdigest(), hashlib.sha256(context_json.encode()).hexdigest()],
        avoid_families=producer_families, model_key=model_key,
        ctx={"source_text": artifact_json + "\n" + context_json, "expected_check_ids": [c["check_id"] for c in checklist],
             "min_confidence": ctx.config["gates"]["min_confidence"], "revise_left": _revise_left(ctx, counter_key)},
    )
    review = out.parsed
    save(ctx, f"GateReview:{gate}", review, run_id=out.run_id)
    reason = review["escalate_reason"] or review["revise_instructions"]
    d = GateDecision(gate.upper(), "ai", mode, review["decision"], review["confidence"], reason, prompt_run_id=out.run_id)
    return finish_gate(ctx, gate, review["decision"], reason, [d], counter_key, allow_proceed=True)
