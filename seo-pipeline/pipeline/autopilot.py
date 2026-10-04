"""Autopilot: hệ thống chạy không người — job vào NEEDS_HUMAN thì được xử lý ngay, không nằm chờ.

Worker gọi resolve() ngay sau transition vào NEEDS_HUMAN. Quyết định theo outcome đã đưa job vào đó:
- ERROR (model trả sai sau repair, extension lỗi, lỗi mạng hết retry, lỗi code): chạy lại bước đó, giãn thời gian
  theo automation.rerun_backoff_seconds, không giới hạn số lần. Mỗi lần chạy lại, runner hỏi model lượt mới
  (không dùng lại output sai cũ) — xem PromptRunner._round.
- ESCALATE / REVISE hết lượt / FAIL ở gate có vòng revise: gửi về bước tạo artifact kèm lý do (tối đa
  automation.gate_rewinds_max lần mỗi gate); hết lượt thì duyệt thay và ghi Warnings.
- FAIL ở bước có bước gốc rõ ràng (UNIQUENESS trùng nguồn → viết lại): quay về bước đó kèm feedback.
- PAUSED_BUDGET: dừng thật (trần chi phí job là chốt an toàn duy nhất).
Mọi quyết định ghi vào job_transitions với actor = "autopilot" và lý do bắt đầu bằng "[autopilot:<STATE>]".
"""
from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import Any

import psycopg

from . import queue
from .artifacts import ArtifactStore
from .states import REVISE_COUNTER, TRANSITIONS, JobState, NeedsHumanReason, Outcome, State, next_state

log = logging.getLogger(__name__)
ACTOR = "autopilot"
# Bước FAIL mà nguyên nhân nằm ở một bước trước đó: (state đích, counter feedback, hướng dẫn)
FAIL_REWIND: dict[State, tuple[State, str, str]] = {
    State.UNIQUENESS: (State.WRITE, "write",
                       "Bài trùng quá nhiều với bài nguồn. Viết lại bằng câu chữ của riêng bạn, đổi cấu trúc đoạn, "
                       "chỉ giữ số liệu từ fact đã duyệt."),
}
# State gate/kiểm tra: artifact cần xét đã tồn tại nên được phép "duyệt thay + Warnings" khi hết lượt sửa.
# Các state còn lại là bước tạo artifact (WRITE, SERP_INPUT, ASSEMBLE, PUBLISH…): lỗi ở đó không được duyệt thay,
# đi tiếp sẽ ra job DONE không có bài.
APPROVABLE = {
    State.TRIAGE_GATE, State.KW_GATE, State.FACT_GATE, State.KNOWLEDGE_SELECT, State.OUTLINE_GATE, State.PREFLIGHT,
    State.TITLE_GATE, State.UNIQUENESS, State.RUBRIC, State.QA_GATE, State.FACT_SIGNOFF, State.FACT_RECHECK,
    State.FINAL_GATE, State.REWORK_CHECK,
}


def _count(conn: psycopg.Connection, job_id: uuid.UUID, stopped: State, action: str, consecutive: bool = False) -> int:
    """Số lần autopilot đã làm `action` cho state này. consecutive: chỉ đếm từ lần gần nhất state này chạy xong
    bình thường (dùng cho backoff chạy lại — lỗi rải rác qua nhiều vòng viết bài không được cộng dồn thành 30 phút)."""
    since = "0001-01-01"
    if consecutive:
        ok = conn.execute(
            "SELECT max(created_at) AS t FROM job_transitions WHERE job_id = %s AND from_state = %s "
            "AND outcome NOT IN ('ERROR', 'ESCALATE', 'FAIL')", (job_id, stopped.value)).fetchone()["t"]
        since = ok or since
    return conn.execute(
        "SELECT count(*) AS n FROM job_transitions WHERE job_id = %s AND actor = %s AND reason LIKE %s AND created_at > %s",
        (job_id, ACTOR, f"[autopilot:{stopped.value}] {action}%", since)).fetchone()["n"]


def _warn(conn: psycopg.Connection, artifacts: ArtifactStore, job_id: uuid.UUID, stopped: State, action: str,
          reason: str) -> None:
    art = artifacts.latest(conn, job_id, "Warnings")
    items = list(art.payload["items"]) if art else []
    items.append({"gate": stopped.value.lower(), "ai_action": action, "reason": reason, "state": stopped.value})
    artifacts.save(conn, job_id, "Warnings", {"items": items}, ACTOR)


def _feedback(conn: psycopg.Connection, artifacts: ArtifactStore, job_id: uuid.UUID, key: str, rnd: int, text: str) -> None:
    artifacts.save(conn, job_id, f"Feedback:{key}", {"round": rnd, "text": text}, ACTOR)


def resolve(conn: psycopg.Connection, config: dict[str, Any], artifacts: ArtifactStore, job_id: uuid.UUID,
            outcome: Outcome) -> str | None:
    """Xử lý job đang NEEDS_HUMAN. Trả tên hành động đã làm, None nếu để nguyên (hết ngân sách)."""
    auto = config.get("automation", {})
    with conn.transaction():
        row = conn.execute("SELECT * FROM jobs WHERE id = %s FOR UPDATE", (job_id,)).fetchone()
        if row is None or row["state"] != State.NEEDS_HUMAN.value:
            return None
        before = queue.job_state(row)
        stopped, why = before.resume_state, before.needs_human_reason or ""
        if stopped is None or why.startswith(NeedsHumanReason.PAUSED_BUDGET):
            log.warning("autopilot: job %s dừng (%s)", job_id, why)
            return None

        if why.startswith("YMYL"):  # gates.ymyl_final_human: người duyệt cuối, autopilot không duyệt thay
            log.warning("autopilot: job %s chờ người duyệt YMYL", job_id)
            return None

        revise_max = config["gates"]["revise_max"]
        backoff = auto.get("rerun_backoff_seconds", [60, 300, 900, 1800])
        rerun_max = auto.get("rerun_max", 6)  # None = không giới hạn
        step_failed = why.startswith(NeedsHumanReason.STEP_FAIL)  # handler thiếu/sai dữ liệu vào (StepFailed)
        action, after, delay = None, None, timedelta(0)

        def rerun() -> bool:
            """Chạy lại bước đã dừng; False = đã hết lượt, để job nằm lại NEEDS_HUMAN."""
            nonlocal action, after, delay
            n = _count(conn, job_id, stopped, "rerun", consecutive=True)
            if rerun_max is not None and n >= rerun_max:
                return False
            action, delay = "rerun", timedelta(seconds=backoff[min(n, len(backoff) - 1)])
            after = next_state(before, Outcome.HUMAN_RERUN, revise_max)
            # Task extension đã hết lượt lỗi: cho chạy lại (lỗi thoáng qua như mất mạng, tab treo)
            conn.execute("UPDATE external_tasks SET status = 'pending', failures = 0, error = NULL, claimed_by = NULL, "
                         "lease_until = NULL WHERE job_id = %s AND status = 'failed'", (job_id,))
            return True

        ok = True
        if outcome == Outcome.ERROR or why.startswith(NeedsHumanReason.REFUSED) or step_failed:
            ok = rerun()
        elif outcome == Outcome.FAIL and stopped in FAIL_REWIND and _count(conn, job_id, stopped, "rewind") < auto.get("gate_rewinds_max", 2):
            target, key, text = FAIL_REWIND[stopped]
            rnd = before.revise_count.get(key, 0) + 1
            _feedback(conn, artifacts, job_id, key, rnd, f"{text}\nLý do: {why}")
            action = "rewind"
            after = JobState(target, revise_count={**before.revise_count, key: rnd})
        elif (stopped in REVISE_COUNTER and (stopped, Outcome.REVISE) in TRANSITIONS
              and _count(conn, job_id, stopped, "revise") < auto.get("gate_rewinds_max", 2)):
            key = REVISE_COUNTER[stopped]
            after = next_state(before, Outcome.HUMAN_REVISE, revise_max)
            _feedback(conn, artifacts, job_id, key, after.revise_count[key], why)
            action = "revise"
        elif stopped in APPROVABLE and (TRANSITIONS.get((stopped, Outcome.APPROVE)) or TRANSITIONS.get((stopped, Outcome.DONE))):
            after = next_state(before, Outcome.HUMAN_APPROVE, revise_max)
            _warn(conn, artifacts, job_id, stopped, "autopilot_approve", why)
            action = "approve"
        else:  # bước tạo artifact báo không làm được (thiếu SERP, CMS chưa cài…): chạy lại, hết lượt thì dừng
            ok = rerun()

        if not ok:
            conn.execute("UPDATE jobs SET last_error = %s WHERE id = %s",
                         (f"autopilot dừng: {stopped.value} lỗi {rerun_max} lần liên tiếp — {why[:300]}", job_id))
            log.warning("autopilot: job %s dừng ở %s sau %s lần chạy lại (%s)", job_id, stopped.value, rerun_max, why[:200])
            return None

        note = f"[autopilot:{stopped.value}] {action} → {after.state.value}: {why[:300]}"
        queue.write_transition(conn, job_id, before, after, f"AUTO_{action.upper()}", ACTOR, note, delay=delay)
    log.info("autopilot: job %s %s", job_id, note)
    return action
