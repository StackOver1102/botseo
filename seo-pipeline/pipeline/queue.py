"""Job queue trên Postgres: lấy job bằng FOR UPDATE SKIP LOCKED + lease (ADR-1).

Worker chết giữa chừng → hết lease_until → worker khác nhận lại job và chạy lại state hiện tại.
Mọi ghi chuyển trạng thái đều kiểm tra leased_by + state cũ, nên worker mất lease không ghi đè được.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

import psycopg

from .states import JobState, State, TERMINAL, WAITING

NOT_RUNNABLE = tuple(s.value for s in TERMINAL | WAITING)


def create_job(conn: psycopg.Connection, *, topic: str, author_id: str, created_by: str, cost_cap_usd: float,
               urgent: bool = False, branch: str | None = None, mode: str | None = None,
               ymyl: bool | None = None, flow: str = "full", site_id: str | None = None) -> UUID:
    """flow = rework: bắt đầu ở REWORK thay vì TRIAGE_AI (xem states.State). site_id: site của bài (pipeline.sites)."""
    row = conn.execute(
        """INSERT INTO jobs (topic, author_id, created_by, cost_cap_usd, urgent, branch, mode, ymyl, flow, state, site_id)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (topic, author_id, created_by, cost_cap_usd, urgent, branch, mode, ymyl, flow,
         "REWORK" if flow == "rework" else "TRIAGE_AI", site_id),
    ).fetchone()
    return row["id"]


def claim(conn: psycopg.Connection, worker_id: str, lease: timedelta) -> dict[str, Any] | None:
    """Nhận một job sẵn sàng; ưu tiên urgent rồi run_after cũ nhất. Tự commit."""
    with conn.transaction():
        return conn.execute(
            """UPDATE jobs SET lease_until = now() + %s, leased_by = %s, attempts = attempts + 1, updated_at = now(),
                   current_call = NULL
               WHERE id = (
                   SELECT id FROM jobs
                   WHERE state <> ALL(%s) AND run_after <= now()
                     AND (lease_until IS NULL OR lease_until < now())
                   ORDER BY urgent DESC, run_after
                   LIMIT 1 FOR UPDATE SKIP LOCKED)
               RETURNING *""",
            (lease, worker_id, list(NOT_RUNNABLE)),
        ).fetchone()


def heartbeat(conn: psycopg.Connection, job_id: UUID, worker_id: str, lease: timedelta) -> bool:
    """Gia hạn lease cho bước chạy lâu (P6, Deep Research). False = đã mất lease, nên dừng."""
    with conn.transaction():
        cur = conn.execute(
            "UPDATE jobs SET lease_until = now() + %s WHERE id = %s AND leased_by = %s AND lease_until > now()",
            (lease, job_id, worker_id),
        )
        return cur.rowcount == 1


def release_for_retry(conn: psycopg.Connection, job_id: UUID, worker_id: str, delay: timedelta, error: str,
                      count_attempt: bool = True) -> bool:
    cur = conn.execute(
        """UPDATE jobs SET lease_until = NULL, leased_by = NULL, run_after = now() + %s, last_error = %s, updated_at = now(),
               attempts = CASE WHEN %s THEN attempts ELSE greatest(attempts - 1, 0) END
           WHERE id = %s AND leased_by = %s""",
        (delay, error, count_attempt, job_id, worker_id),
    )
    return cur.rowcount == 1


def job_state(row: dict[str, Any]) -> JobState:
    return JobState(
        state=State(row["state"]),
        resume_state=State(row["resume_state"]) if row["resume_state"] else None,
        needs_human_reason=row["needs_human_reason"],
        revise_count=dict(row["revise_count"] or {}),
    )


def write_transition(conn: psycopg.Connection, job_id: UUID, before: JobState, after: JobState, outcome: str,
                     actor: str, reason: str | None = None, worker_id: str | None = None,
                     delay: timedelta | None = None) -> bool:
    """Ghi trạng thái mới + log. Chỉ thành công khi job vẫn ở state cũ (và vẫn do worker này giữ lease)."""
    lease_clause = "AND leased_by = %(worker)s" if worker_id else ""
    cur = conn.execute(
        f"""UPDATE jobs SET state = %(state)s, resume_state = %(resume)s, needs_human_reason = %(reason)s,
                revise_count = %(rc)s, lease_until = NULL, leased_by = NULL, attempts = 0, last_error = NULL,
                run_after = now() + %(delay)s, updated_at = now()
            WHERE id = %(id)s AND state = %(from)s {lease_clause}""",
        {
            "state": after.state.value,
            "resume": after.resume_state.value if after.resume_state else None,
            "reason": after.needs_human_reason,
            "rc": psycopg.types.json.Jsonb(after.revise_count),
            "delay": delay or timedelta(0),
            "id": job_id,
            "from": before.state.value,
            "worker": worker_id,
        },
    )
    if cur.rowcount != 1:
        return False
    conn.execute(
        """INSERT INTO job_transitions (job_id, from_state, outcome, to_state, reason, actor)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (job_id, before.state.value, outcome, after.state.value, reason or after.needs_human_reason, actor),
    )
    return True


def add_cost(conn: psycopg.Connection, job_id: UUID, usd: float) -> None:
    conn.execute("UPDATE jobs SET cost_usd = cost_usd + %s WHERE id = %s", (usd, job_id))
