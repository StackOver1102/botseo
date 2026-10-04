"""Chờ sự kiện Postgres LISTEN/NOTIFY (migration 005) thay cho vòng sleep-quét DB.

Kênh: `ext_task` (payload = provider) khi có task external chờ nhận; `job_event` (payload = job_id) khi job/task đổi.
Mỗi Listener giữ một connection riêng (autocommit) — LISTEN gắn với session.
"""
from __future__ import annotations

import time
from typing import Callable, Iterator

import psycopg

from .db import connect


class Listener:
    def __init__(self, *channels: str, url: str | None = None):
        self.conn = connect(url, autocommit=True)
        for ch in channels:
            self.conn.execute(f"LISTEN {ch}")  # tên kênh là hằng trong code, không từ input

    def wait(self, timeout: float, match: Callable[[str, str], bool] = lambda ch, payload: True) -> bool:
        """Chờ tới khi có notify thoả `match` hoặc hết `timeout` giây. True = có sự kiện."""
        deadline = time.monotonic() + timeout
        while (left := deadline - time.monotonic()) > 0:
            for n in self.conn.notifies(timeout=left, stop_after=1):
                if match(n.channel, n.payload):
                    return True
            # notifies() trả về khi hết giờ hoặc sau 1 notify không khớp: vòng lại với thời gian còn lại
        return False

    def events(self, timeout: float) -> Iterator[tuple[str, str]]:
        """Các notify tới trong vòng `timeout` giây kể từ lúc gọi."""
        for n in self.conn.notifies(timeout=timeout):
            yield n.channel, n.payload

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Listener":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def safe_listener(*channels: str) -> Listener | None:
    """Listener hoặc None nếu DB chưa có trigger/không LISTEN được — caller quay về cách quét cũ."""
    try:
        return Listener(*channels)
    except psycopg.Error:
        return None
