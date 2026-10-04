"""Artifact bất biến có version (ADR-2). Blob lưu theo sha256 (content-addressed)."""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

import psycopg


_PROJECT_ROOT = Path(__file__).resolve().parent.parent


class BlobStore(Protocol):
    def put(self, data: bytes, sha256: str) -> str: ...
    def get(self, uri: str) -> bytes: ...


class LocalBlobStore:
    """Dev/minimum mode. Production thay bằng object storage cùng interface."""

    def __init__(self, root: str | Path | None = None):
        root = Path(root or os.environ.get("SEO_BLOB_DIR", "var/blobs"))
        # Đường dẫn tương đối tính từ thư mục dự án, không theo thư mục đang đứng khi chạy lệnh
        self.root = root if root.is_absolute() else _PROJECT_ROOT / root

    def put(self, data: bytes, sha256: str) -> str:
        path = self.root / sha256[:2] / sha256
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
        return f"file://{path.resolve()}"

    def get(self, uri: str) -> bytes:
        path = Path(uri.removeprefix("file://"))
        try:
            return path.read_bytes()
        except OSError:
            # URI lưu đường dẫn tuyệt đối của máy đã ghi blob. Chuyển máy (pg_dump + copy var/blobs) thì đường dẫn cũ
            # không còn: tên file là sha256 nên tìm lại trong thư mục blob hiện tại.
            name = re.split(r"[\\/]", uri)[-1]
            local = self.root / name[:2] / name
            if local != path and local.exists():
                return local.read_bytes()
            raise


def canonical_json(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True)
class Artifact:
    id: UUID
    job_id: UUID
    kind: str
    version: int
    sha256: str
    schema_version: str
    payload: Any


class ArtifactStore:
    def __init__(self, blobs: BlobStore):
        self.blobs = blobs

    def save(self, conn: psycopg.Connection, job_id: UUID, kind: str, payload: Any, produced_by: str,
             schema_version: str = "5.0.0", run_id: UUID | None = None) -> Artifact:
        """Tạo version mới. Nội dung trùng version mới nhất thì trả lại version cũ (idempotent khi retry)."""
        data = canonical_json(payload)
        digest = hashlib.sha256(data).hexdigest()
        uri = self.blobs.put(data, digest)  # ghi blob trước: blob mồ côi vô hại, dòng DB trỏ blob thiếu thì hỏng
        with conn.transaction():
            latest = self._latest_row(conn, job_id, kind, lock=True)
            if latest and latest["sha256"] == digest:
                return self._to_artifact(latest, payload)
            row = conn.execute(
                """INSERT INTO artifacts (job_id, kind, schema_version, version, sha256, blob_uri, produced_by_run_id, produced_by)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
                (job_id, kind, schema_version, (latest["version"] + 1) if latest else 1, digest, uri, run_id, produced_by),
            ).fetchone()
        return self._to_artifact(row, payload)

    def latest(self, conn: psycopg.Connection, job_id: UUID, kind: str) -> Artifact | None:
        row = self._latest_row(conn, job_id, kind)
        return self._load(row) if row else None

    def get(self, conn: psycopg.Connection, artifact_id: UUID) -> Artifact:
        row = conn.execute("SELECT * FROM artifacts WHERE id = %s", (artifact_id,)).fetchone()
        if row is None:
            raise KeyError(artifact_id)
        return self._load(row)

    def _latest_row(self, conn, job_id, kind, lock: bool = False):
        if lock:
            # Khoá theo (job, kind) để hai worker không cùng tạo một version
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"{job_id}:{kind}",))
        return conn.execute(
            "SELECT * FROM artifacts WHERE job_id = %s AND kind = %s ORDER BY version DESC LIMIT 1", (job_id, kind)
        ).fetchone()

    def _load(self, row) -> Artifact:
        data = self.blobs.get(row["blob_uri"])
        if hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise ValueError(f"blob hỏng: {row['id']}")
        return self._to_artifact(row, json.loads(data))

    @staticmethod
    def _to_artifact(row, payload) -> Artifact:
        return Artifact(row["id"], row["job_id"], row["kind"], row["version"], row["sha256"], row["schema_version"], payload)
