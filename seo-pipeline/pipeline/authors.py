"""Danh sách tác giả trong DB (migration 007): tên/URL cho JSON-LD, hồ sơ giọng văn dùng lại, khoá của kho dữ liệu.

Tên tác giả và mẫu giọng văn phải là của người/tổ chức thật — code không tự sinh tác giả hay bài mẫu.
"""
from __future__ import annotations

import re
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from vnseo.vn_text.normalize import strip_diacritics

_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,60}$")


class AuthorError(ValueError):
    pass


def slugify(name: str) -> str:
    """'Nguyễn Văn An' → 'nguyen-van-an' (dùng làm authors.id khi không tự đặt)."""
    slug = re.sub(r"[^a-z0-9]+", "-", strip_diacritics(name)).strip("-")[:60]
    if not _ID.match(slug):
        raise AuthorError(f"không tạo được mã tác giả từ tên '{name}'")
    return slug


def list_authors(conn: psycopg.Connection, include_inactive: bool = False) -> list[dict[str, Any]]:
    """Kèm số mục kho còn dùng được và có hồ sơ giọng văn hay chưa — UI hiển thị để biết tác giả nào đủ dữ liệu."""
    return conn.execute(
        """SELECT a.id, a.name, a.url, a.kind, a.site_id, a.active, a.voice_source, a.voice_origin,
                  a.voice_profile IS NOT NULL AS has_voice, jsonb_array_length(a.voice_learned_from) AS voice_learned_jobs,
                  (SELECT count(*) FROM author_knowledge k
                   WHERE k.author_id = a.id AND k.status = 'active' AND k.valid_until >= current_date) AS knowledge_entries
           FROM authors a WHERE a.active OR %s ORDER BY a.name""", (include_inactive,)).fetchall()


def get_author(conn: psycopg.Connection, author_id: str) -> dict[str, Any] | None:
    return conn.execute("SELECT * FROM authors WHERE id = %s", (author_id,)).fetchone()


def upsert_author(conn: psycopg.Connection, name: str, url: str | None = None, author_id: str | None = None,
                  kind: str = "person", site_id: str | None = None) -> str:
    """Thêm tác giả, hoặc cập nhật tên/URL/loại nếu mã đã có. Hồ sơ giọng văn không bị đụng tới.
    kind: person | organization (ban biên tập, thương hiệu) — quyết định @type của author trong JSON-LD."""
    if kind not in ("person", "organization"):
        raise AuthorError("loại tác giả phải là person hoặc organization")
    name = name.strip()
    if not name:
        raise AuthorError("tên tác giả không được trống")
    url = (url or "").strip() or None
    if url and not url.startswith(("http://", "https://")):
        raise AuthorError("URL trang tác giả phải bắt đầu bằng http:// hoặc https://")
    author_id = (author_id or "").strip() or slugify(name)
    if not _ID.match(author_id):
        raise AuthorError("mã tác giả chỉ gồm chữ thường không dấu, số và dấu gạch nối (vd. nguyen-an)")
    conn.execute(
        """INSERT INTO authors (id, name, url, kind, site_id) VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, url = EXCLUDED.url, kind = EXCLUDED.kind, active = true,
               site_id = coalesce(EXCLUDED.site_id, authors.site_id), updated_at = now()""",
        (author_id, name, url, kind, (site_id or "").strip() or None))
    return author_id


def jsonld_type(author_row: dict[str, Any] | None) -> str:
    return "Organization" if author_row and author_row.get("kind") == "organization" else "Person"


def set_voice_profile(conn: psycopg.Connection, author_id: str, profile: dict, source: str) -> None:
    """Hồ sơ dựng từ bài của chính tác giả: từ đây việc tự học từ SERP dừng hẳn cho tác giả này."""
    cur = conn.execute("UPDATE authors SET voice_profile = %s, voice_source = %s, voice_origin = 'author', updated_at = now() "
                       "WHERE id = %s", (Jsonb(profile), source, author_id))
    if cur.rowcount != 1:
        raise AuthorError(f"không có tác giả '{author_id}'")


def style_only(profile: dict) -> dict:
    """Hồ sơ học từ bài của site khác: chỉ giữ mô tả phong cách và checklist. Bỏ mọi câu trích nguyên văn
    (evidence_quotes, exemplar) — chúng là câu chữ của người khác, đưa cho writer làm mẫu thì bài dễ lặp lại y nguyên."""
    return {
        "dimensions": [{**d, "evidence_quotes": []} for d in profile.get("dimensions", [])],
        "positive_exemplars": [],
        "negative_exemplars_qa_only": [],
        "checklist_qa": profile.get("checklist_qa", []),
    }


def record_learned_voice(conn: psycopg.Connection, author_id: str, profile: dict, job_id: Any, source: str) -> int:
    """Lưu hồ sơ vừa học từ nguồn SERP của một job. Trả số job đã học. Không ghi đè hồ sơ voice_origin = 'author'."""
    row = conn.execute(
        """UPDATE authors SET voice_profile = %s, voice_origin = 'serp', voice_source = %s,
                  voice_learned_from = voice_learned_from || %s, updated_at = now()
           WHERE id = %s AND voice_origin IS DISTINCT FROM 'author' AND NOT voice_learned_from @> %s
           RETURNING jsonb_array_length(voice_learned_from) AS n""",
        (Jsonb(style_only(profile)), source, Jsonb([str(job_id)]), author_id, Jsonb([str(job_id)]))).fetchone()
    if row is None:  # đã học job này rồi (bước chạy lại) hoặc tác giả đã có hồ sơ riêng
        row = conn.execute("SELECT jsonb_array_length(voice_learned_from) AS n FROM authors WHERE id = %s", (author_id,)).fetchone()
    return row["n"] if row else 0
