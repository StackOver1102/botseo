"""Hồ sơ website (migration 009): thương hiệu, tác giả mặc định, trang nội bộ, kho dữ liệu tầng site.

Kho tầng site lưu ở author_knowledge với author_id = "site:<id>", site_id = <id>, reusable = true.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

import psycopg
from psycopg.types.json import Jsonb

_ID = re.compile(r"^[a-z0-9][a-z0-9.-]{1,60}$")


class SiteError(ValueError):
    pass


def site_owner(site_id: str) -> str:
    """Giá trị author_id của các mục kho tầng site."""
    return f"site:{site_id}"


def site_id_from_url(url: str) -> str:
    """'https://www.KQBD2.com/' → 'kqbd2.com'."""
    try:
        host = urlsplit(url if "://" in url else f"https://{url}").netloc.lower().removeprefix("www.")
    except ValueError:
        host = ""
    host = host.split(":")[0]
    if not _ID.match(host):
        raise SiteError(f"không lấy được mã site từ địa chỉ '{url}'")
    return host


def list_sites(conn: psycopg.Connection, include_inactive: bool = False) -> list[dict[str, Any]]:
    return conn.execute(
        """SELECT s.id, s.name, s.url, s.default_author_id, s.active, jsonb_array_length(s.internal_pages) AS internal_pages,
                  (SELECT count(*) FROM author_knowledge k
                   WHERE k.site_id = s.id AND k.status = 'active' AND k.valid_until >= current_date) AS knowledge_entries,
                  (SELECT count(*) FROM authors a WHERE a.site_id = s.id AND a.active) AS authors
           FROM sites s WHERE s.active OR %s ORDER BY s.name""", (include_inactive,)).fetchall()


def get_site(conn: psycopg.Connection, site_id: str) -> dict[str, Any] | None:
    return conn.execute("SELECT * FROM sites WHERE id = %s", (site_id,)).fetchone()


def upsert_site(conn: psycopg.Connection, name: str, url: str, site_id: str | None = None,
                default_author_id: str | None = None) -> str:
    """Thêm site hoặc cập nhật tên/URL/tác giả mặc định. Trang nội bộ và kho không bị đụng tới."""
    name, url = name.strip(), url.strip()
    if not name:
        raise SiteError("tên site không được trống")
    if not url.startswith(("http://", "https://")):
        raise SiteError("địa chỉ site phải bắt đầu bằng http:// hoặc https://")
    site_id = (site_id or "").strip() or site_id_from_url(url)
    if not _ID.match(site_id):
        raise SiteError("mã site chỉ gồm chữ thường, số, dấu chấm và gạch nối (vd. kqbd2.com)")
    default_author_id = (default_author_id or "").strip() or None
    if default_author_id and conn.execute("SELECT 1 FROM authors WHERE id = %s", (default_author_id,)).fetchone() is None:
        raise SiteError(f"không có tác giả '{default_author_id}'")
    conn.execute(
        """INSERT INTO sites (id, name, url, default_author_id) VALUES (%s, %s, %s, %s)
           ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, url = EXCLUDED.url, active = true, updated_at = now(),
               default_author_id = coalesce(EXCLUDED.default_author_id, sites.default_author_id)""",
        (site_id, name, url, default_author_id))
    return site_id


def set_internal_pages(conn: psycopg.Connection, site_id: str, pages: list[dict[str, str]]) -> int:
    """pages: [{url, title}] — bỏ trùng theo URL, chỉ giữ URL http(s)."""
    seen, clean = set(), []
    for p in pages:
        url, title = (p.get("url") or "").strip(), (p.get("title") or "").strip()
        if not url.startswith(("http://", "https://")) or url in seen or not title:
            continue
        seen.add(url)
        clean.append({"url": url, "title": title})
    cur = conn.execute("UPDATE sites SET internal_pages = %s, updated_at = now() WHERE id = %s", (Jsonb(clean), site_id))
    if cur.rowcount != 1:
        raise SiteError(f"không có site '{site_id}'")
    return len(clean)
