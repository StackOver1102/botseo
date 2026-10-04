"""Kết nối Postgres và chạy migration SQL theo thứ tự tên file."""
from __future__ import annotations

import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
# 127.0.0.1 thay cho localhost: trên Windows localhost thử IPv6 trước và kết nối treo khi Postgres chỉ nghe IPv4
DEFAULT_DSN = "postgresql://seo:seo@127.0.0.1:55432/seo"


def dsn() -> str:
    return os.environ.get("SEO_DATABASE_URL", DEFAULT_DSN)


def connect(url: str | None = None, autocommit: bool = False) -> psycopg.Connection:
    return psycopg.connect(url or dsn(), row_factory=dict_row, autocommit=autocommit)


def migrate(conn: psycopg.Connection) -> list[str]:
    """Áp các file migrations/*.sql chưa chạy. Mỗi file một transaction."""
    with conn.transaction():
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, applied_at timestamptz DEFAULT now())")
    done = {r["name"] for r in conn.execute("SELECT name FROM schema_migrations")}
    applied = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name in done:
            continue
        with conn.transaction():
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
        applied.append(path.name)
    return applied


if __name__ == "__main__":
    with connect(autocommit=True) as c:
        print(migrate(c) or "up to date")
