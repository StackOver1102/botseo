"""Worker: nhận job → chạy handler của state hiện tại → ghi chuyển trạng thái.

Handler là hàm (ctx) -> StepResult, không tự đổi state. Handler được phép gọi LLM lâu,
lưu artifact (idempotent theo nội dung) và cộng chi phí; executor lo phần còn lại.
"""
from __future__ import annotations

import logging
import socket
import threading
import uuid
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any, Callable

import psycopg

from . import queue
from .artifacts import ArtifactStore
from .llm.base import AwaitingExternal, LLMError, Refused, TransientError
from .states import REVISE_COUNTER, JobState, NeedsHumanReason, Outcome, State, next_state

log = logging.getLogger(__name__)


class RetryableError(Exception):
    """Lỗi tạm thời (429/5xx, timeout) sau khi adapter đã tự backoff; executor thử lại cả bước."""


class StepRefused(Exception):
    """Model từ chối (stop_reason refusal). Không repair, chuyển thẳng cho người."""


@dataclass
class GateDecision:
    gate: str
    actor: str  # ai | code
    mode: str  # auto | shadow | human
    action: str  # approve | revise | escalate
    confidence: float | None = None
    reason: str | None = None
    rule_failures: list[str] | None = None
    prompt_run_id: uuid.UUID | None = None


@dataclass
class StepResult:
    outcome: Outcome
    reason: str | None = None
    delay: timedelta | None = None  # hẹn giờ chạy state kế tiếp (DEFER, chờ batch)
    decisions: list[GateDecision] = field(default_factory=list)
    # Tự quay về bước gốc gây lỗi (vd. RUBRIC → FACT_VOICE_FANOUT). bump: các counter feedback tăng 1 vòng —
    # handler đã save_feedback(ctx, key, …) trước đó nên feedback_for() ở bước đích khớp đúng vòng.
    rewind_to: State | None = None
    bump: tuple[str, ...] = ()


@dataclass
class StepContext:
    conn: psycopg.Connection
    job: dict[str, Any]
    state: JobState
    config: dict[str, Any]
    artifacts: ArtifactStore
    worker_id: str
    services: Any = None  # steps.common.Services: registry, schema, provider factory

    @property
    def job_id(self) -> uuid.UUID:
        return self.job["id"]

    def heartbeat(self) -> None:
        lease = timedelta(seconds=self.config.get("worker", {}).get("lease_seconds", 900))
        if not queue.heartbeat(self.conn, self.job_id, self.worker_id, lease):
            raise LeaseLost(self.job_id)


class _LeaseKeeper(threading.Thread):
    """Gia hạn lease định kỳ trong lúc handler chạy (gọi model có thể vài phút). Nhờ đó lease được để ngắn: worker
    chết (restart uvicorn, Ctrl+C) thì job được nhận lại sau ~lease_seconds, không phải chờ 15 phút.
    Dùng connection riêng: connection của worker có thể đang ở giữa một transaction."""

    def __init__(self, conn: psycopg.Connection, job_id: uuid.UUID, worker_id: str, lease: timedelta):
        super().__init__(name=f"lease-{job_id}", daemon=True)
        self.dsn, self.password = conn.info.dsn, conn.info.password
        self.job_id, self.worker_id, self.lease = job_id, worker_id, lease
        self.stop = threading.Event()

    def run(self) -> None:
        try:
            with psycopg.connect(self.dsn, password=self.password, autocommit=True) as c:
                while not self.stop.wait(max(0.5, self.lease.total_seconds() / 4)):
                    if not queue.heartbeat(c, self.job_id, self.worker_id, self.lease):
                        return
        except psycopg.Error:
            log.warning("không gia hạn được lease job %s", self.job_id, exc_info=True)


class LeaseLost(Exception):
    pass


Handler = Callable[[StepContext], StepResult]


class Worker:
    def __init__(self, conn: psycopg.Connection, config: dict[str, Any], artifacts: ArtifactStore,
                 handlers: dict[State, Handler], worker_id: str | None = None, services: Any = None):
        if not conn.autocommit:
            raise ValueError("Worker cần connection autocommit; ghi nhiều câu thì bọc conn.transaction()")
        self.conn = conn
        self.config = config
        self.artifacts = artifacts
        self.handlers = handlers
        self.services = services
        self.worker_id = worker_id or f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"
        wcfg = config.get("worker", {})
        self.lease = timedelta(seconds=wcfg.get("lease_seconds", 900))
        self.max_attempts = wcfg.get("max_attempts", 3)
        self.backoff = [timedelta(seconds=s) for s in wcfg.get("retry_backoff_seconds", [30, 120, 600])]

    @property
    def revise_max(self) -> dict[str, int]:
        return self.config["gates"]["revise_max"]

    def run_once(self) -> bool:
        """Chạy một bước của một job. False = không có job nào sẵn sàng."""
        row = queue.claim(self.conn, self.worker_id, self.lease)
        if row is None:
            return False
        before = queue.job_state(row)
        ctx = StepContext(self.conn, row, before, self.config, self.artifacts, self.worker_id, self.services)

        if row["cost_usd"] >= row["cost_cap_usd"]:
            self._finish(ctx, StepResult(Outcome.ERROR, NeedsHumanReason.PAUSED_BUDGET))
            return True
        monthly_cap = self.config.get("cost", {}).get("monthly_cap_usd")
        if monthly_cap is not None:
            spent = self.conn.execute("SELECT coalesce(sum(cost_usd), 0) AS s FROM prompt_runs "
                                      "WHERE created_at >= date_trunc('month', now())").fetchone()["s"]
            if spent >= monthly_cap:
                self._finish(ctx, StepResult(
                    Outcome.ERROR, f"{NeedsHumanReason.PAUSED_BUDGET}: đã tiêu {float(spent):.2f} USD trong tháng, "
                                   f"trần cost.monthly_cap_usd = {monthly_cap}"))
                return True

        handler = self.handlers.get(before.state)
        if handler is None:
            self._finish(ctx, StepResult(Outcome.ERROR, f"no_handler:{before.state}"))
            return True

        keeper = _LeaseKeeper(self.conn, row["id"], self.worker_id, self.lease)
        keeper.start()
        try:
            result = handler(ctx)
        except LeaseLost:
            log.warning("mất lease job %s, bỏ kết quả", row["id"])
            return True
        except AwaitingExternal as e:
            # Chờ extension: hẹn chạy lại, không tính là một lần thử
            with self.conn.transaction():
                queue.release_for_retry(self.conn, row["id"], self.worker_id, timedelta(seconds=e.poll_seconds),
                                        f"awaiting_external:{e.task_id}", count_attempt=False)
            return True
        except (StepRefused, Refused) as e:
            result = StepResult(Outcome.ERROR, f"{NeedsHumanReason.REFUSED}: {e}")
        except (RetryableError, TransientError) as e:
            if row["attempts"] < self.max_attempts:
                delay = self.backoff[min(row["attempts"] - 1, len(self.backoff) - 1)]
                with self.conn.transaction():
                    queue.release_for_retry(self.conn, row["id"], self.worker_id, delay, str(e))
                return True
            result = StepResult(Outcome.ERROR, f"retry_exhausted: {e}")
        except LLMError as e:  # ProviderError, Truncated, InvalidOutput
            result = StepResult(Outcome.ERROR, f"{type(e).__name__}: {e}")
        except Exception as e:  # lỗi lập trình: không retry mù, chuyển cho người xem
            log.exception("handler %s lỗi", before.state)
            result = StepResult(Outcome.ERROR, f"{type(e).__name__}: {e}")
        finally:
            keeper.stop.set()

        self._finish(ctx, result)
        return True

    def _finish(self, ctx: StepContext, result: StepResult) -> None:
        before = ctx.state
        if result.rewind_to is not None:
            counts = {**before.revise_count, **{k: before.revise_count.get(k, 0) + 1 for k in result.bump}}
            after, outcome = replace(before, state=result.rewind_to, revise_count=counts), "AUTO_REWIND"
        else:
            after, outcome = next_state(before, result.outcome, self.revise_max, result.reason), result.outcome.value
        with self.conn.transaction():
            ok = queue.write_transition(
                self.conn, ctx.job_id, before, after, outcome, f"worker:{self.worker_id}",
                result.reason, worker_id=self.worker_id, delay=result.delay,
            )
            if not ok:
                log.warning("job %s đã đổi state hoặc mất lease; bỏ transition", ctx.job_id)
                return
            for d in result.decisions:
                record_decision(self.conn, ctx.job_id, d)
        if after.state == State.NEEDS_HUMAN and self.config.get("automation", {}).get("enabled"):
            from .autopilot import resolve  # import muộn: autopilot dùng lại hàm của module này
            resolve(self.conn, self.config, self.artifacts, ctx.job_id, result.outcome)

    def run_until_idle(self, max_steps: int = 1000) -> int:
        steps = 0
        while steps < max_steps and self.run_once():
            steps += 1
        return steps


def record_decision(conn: psycopg.Connection, job_id: uuid.UUID, d: GateDecision, user_id: str | None = None) -> None:
    conn.execute(
        """INSERT INTO gate_decisions (job_id, gate, actor, user_id, prompt_run_id, mode, action, confidence, reason, rule_failures)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (job_id, d.gate, d.actor, user_id, d.prompt_run_id, d.mode, d.action, d.confidence, d.reason,
         psycopg.types.json.Jsonb(d.rule_failures) if d.rule_failures is not None else None),
    )


# ---------- hành động của người trên hàng chờ NEEDS_HUMAN (§7) ----------

HUMAN_ACTIONS = {
    "approve": Outcome.HUMAN_APPROVE,  # đi tiếp như gate approve (sau khi đã sửa artifact nếu cần)
    "rerun": Outcome.HUMAN_RERUN,      # chạy lại state đã dừng
    "revise": Outcome.HUMAN_REVISE,    # gate đang dừng → quay về bước tạo artifact, `reason` là hướng dẫn sửa
    "cancel": Outcome.CANCEL,
}


def human_resolve(conn: psycopg.Connection, job_id: uuid.UUID, user_id: str, action: str, reason: str,
                  config: dict[str, Any], artifacts: ArtifactStore | None = None) -> JobState:
    if not reason.strip():
        raise ValueError("mọi hành động của người bắt buộc có lý do")
    if action == "revise" and artifacts is None:
        raise ValueError("revise cần ArtifactStore để lưu hướng dẫn cho bước trước")
    outcome = HUMAN_ACTIONS[action]
    with conn.transaction():
        row = conn.execute("SELECT * FROM jobs WHERE id = %s FOR UPDATE", (job_id,)).fetchone()
        before = queue.job_state(row)
        after = next_state(before, outcome, config["gates"]["revise_max"], reason)
        if not queue.write_transition(conn, job_id, before, after, outcome.value, f"human:{user_id}", reason):
            raise RuntimeError("job đã đổi state")
        if action == "revise":
            # Bước trước đọc hướng dẫn qua steps.common.feedback_for (khớp theo số vòng revise)
            key = REVISE_COUNTER[before.resume_state]
            artifacts.save(conn, job_id, f"Feedback:{key}",
                           {"round": after.revise_count[key], "text": f"Hướng dẫn của biên tập viên: {reason}"},
                           f"human:{user_id}")
        if action == "rerun":
            # Người đã xử lý nguyên nhân (đăng nhập lại, sửa selector…): cho task extension đã hết lượt chạy lại
            conn.execute("UPDATE external_tasks SET status = 'pending', failures = 0, error = NULL, claimed_by = NULL, "
                         "lease_until = NULL WHERE job_id = %s AND status = 'failed'", (job_id,))
        if before.resume_state is not None:
            record_decision(conn, job_id, GateDecision(
                gate=before.resume_state.value, actor="human", mode="human",
                action={"approve": "approve", "rerun": "revise", "revise": "revise", "cancel": "reject"}[action],
                reason=reason,
            ), user_id=user_id)
    return after


def sweep_waiting(conn: psycopg.Connection, config: dict[str, Any]) -> dict[str, list]:
    """Chạy định kỳ: NEEDS_HUMAN quá hạn → STALE; trả danh sách job cần nhắc."""
    g = config["gates"]
    stale_after = timedelta(days=g.get("needs_human_stale_days", 14))
    remind_after = timedelta(hours=g.get("needs_human_remind_hours", 48))
    stale = []
    with conn.transaction():
        rows = conn.execute(
            "SELECT * FROM jobs WHERE state = 'NEEDS_HUMAN' AND updated_at < now() - %s FOR UPDATE SKIP LOCKED",
            (stale_after,),
        ).fetchall()
        for row in rows:
            before = queue.job_state(row)
            after = next_state(before, Outcome.TIMEOUT, g["revise_max"])
            if queue.write_transition(conn, row["id"], before, after, Outcome.TIMEOUT.value, "sweeper"):
                stale.append(row["id"])
    remind = [r["id"] for r in conn.execute(
        "SELECT id FROM jobs WHERE state = 'NEEDS_HUMAN' AND updated_at < now() - %s", (remind_after,)
    ).fetchall()]
    return {"stale": stale, "remind": remind}


def rewind_job(conn: psycopg.Connection, job_id: uuid.UUID, state: State, user_id: str, reason: str) -> None:
    """Đưa job về một state để chạy lại từ đó (thử prompt/luật mới). Artifact cũ giữ nguyên làm lịch sử;
    bước nào đầu vào và prompt không đổi sẽ lấy từ cache của runner, không gọi lại model."""
    with conn.transaction():
        row = conn.execute("SELECT state FROM jobs WHERE id = %s FOR UPDATE", (job_id,)).fetchone()
        if row is None:
            raise ValueError("không có job")
        # Đặt lại bộ đếm revise: phản hồi của các vòng cũ (Feedback:*) không còn khớp vòng nên không bị áp lại
        conn.execute("UPDATE jobs SET state = %s, resume_state = NULL, needs_human_reason = NULL, lease_until = NULL, "
                     "leased_by = NULL, attempts = 0, last_error = NULL, revise_count = '{}', run_after = now(), "
                     "updated_at = now() WHERE id = %s", (state.value, job_id))
        conn.execute("INSERT INTO job_transitions (job_id, from_state, outcome, to_state, reason, actor) "
                     "VALUES (%s, %s, 'REWIND', %s, %s, %s)", (job_id, row["state"], state.value, reason, f"human:{user_id}"))
        conn.execute("UPDATE external_tasks SET status = 'cancelled' WHERE job_id = %s AND status IN ('pending', 'claimed')",
                     (job_id,))


def cancel_job(conn: psycopg.Connection, job_id: uuid.UUID, user_id: str, reason: str) -> None:
    """Huỷ job ở bất kỳ state chưa kết thúc: huỷ task extension còn treo, trả lại lượt dùng mục kho đã giữ chỗ.
    Worker đang chạy dở job này sẽ không ghi được transition (state đã đổi) nên kết quả của nó bị bỏ."""
    if not reason.strip():
        raise ValueError("huỷ job bắt buộc có lý do")
    with conn.transaction():
        row = conn.execute("SELECT state FROM jobs WHERE id = %s FOR UPDATE", (job_id,)).fetchone()
        if row is None or row["state"] in ("DONE", "CANCELLED"):
            return
        conn.execute("UPDATE jobs SET state = 'CANCELLED', lease_until = NULL, leased_by = NULL, updated_at = now() "
                     "WHERE id = %s", (job_id,))
        conn.execute("INSERT INTO job_transitions (job_id, from_state, outcome, to_state, reason, actor) "
                     "VALUES (%s, %s, 'CANCEL', 'CANCELLED', %s, %s)", (job_id, row["state"], reason, f"human:{user_id}"))
        conn.execute("UPDATE external_tasks SET status = 'cancelled' WHERE job_id = %s AND status IN ('pending', 'claimed')",
                     (job_id,))
        conn.execute("UPDATE knowledge_usage SET status = 'released' WHERE job_id = %s AND status = 'reserved'", (job_id,))
