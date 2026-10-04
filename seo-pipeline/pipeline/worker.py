"""Chạy worker: .venv/bin/python -m pipeline.worker [--once]

Vòng lặp: chạy job sẵn sàng; hết job thì ngủ `idle_seconds`; mỗi `sweep_seconds` quét hàng chờ NEEDS_HUMAN.
"""
from __future__ import annotations

import argparse
import logging
import time

from .artifacts import ArtifactStore, LocalBlobStore
from .config import load_config
from .db import connect
from .executor import Worker, sweep_waiting
from .notify import safe_listener
from .steps import HANDLERS, Services

log = logging.getLogger("worker")


def run_forever(idle_seconds: float = 5, sweep_seconds: float = 600) -> None:
    """Vòng worker không dừng (dùng cho worker nhúng trong uvicorn). Lỗi bất ngờ: log, chờ rồi chạy tiếp."""
    config = load_config()
    while True:
        try:
            conn = connect(autocommit=True)
            worker = Worker(conn, config, ArtifactStore(LocalBlobStore(config.get("blob_dir"))), HANDLERS,
                            services=Services.default(conn, config))
            listener = safe_listener("job_event")
            log.info("worker nhúng %s bắt đầu", worker.worker_id)
            last_sweep = 0.0
            while True:
                if worker.run_until_idle():
                    continue
                if time.monotonic() - last_sweep > sweep_seconds:
                    sweep_waiting(conn, config)
                    last_sweep = time.monotonic()
                if listener:
                    listener.wait(idle_seconds)
                else:
                    time.sleep(idle_seconds)
        except Exception:  # noqa: BLE001 — mất kết nối DB…: không để thread chết
            log.exception("worker nhúng lỗi, khởi động lại sau 10s")
            time.sleep(10)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="chạy đến khi hết job rồi thoát")
    ap.add_argument("--idle-seconds", type=float, default=5)
    ap.add_argument("--sweep-seconds", type=float, default=600)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    config = load_config()
    conn = connect(autocommit=True)
    worker = Worker(conn, config, ArtifactStore(LocalBlobStore(config.get("blob_dir"))), HANDLERS,
                    services=Services.default(conn, config))
    log.info("worker %s bắt đầu", worker.worker_id)
    last_sweep = 0.0
    listener = safe_listener("job_event")  # có kết quả task / job mới → thức dậy ngay thay vì chờ hết idle_seconds
    while True:
        steps = worker.run_until_idle()
        if steps:
            log.info("đã chạy %d bước", steps)
        if time.monotonic() - last_sweep > args.sweep_seconds:
            res = sweep_waiting(conn, config)
            if res["stale"] or res["remind"]:
                log.info("NEEDS_HUMAN: %d quá hạn → STALE, %d cần nhắc", len(res["stale"]), len(res["remind"]))
            last_sweep = time.monotonic()
        if args.once:
            return
        if listener:
            listener.wait(args.idle_seconds)
        else:
            time.sleep(args.idle_seconds)


if __name__ == "__main__":
    main()
