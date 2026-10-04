"""Kho dữ liệu tác giả (§5.12): nhập, lọc cứng, xếp hạng lai, giữ chỗ/ghi nhận lượt dùng.

Khác spec: build_evidence không tăng usage_count ngay. Lượt dùng được "giữ chỗ" (reserved) khi P5 chọn,
chỉ commit khi bài publish, và release khi job bị huỷ hoặc P5 chạy lại — để revise không đốt quota.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from vnseo.facts import knowledge_facts_to_claims, uncovered_numbers
from vnseo.pii import redact
from vnseo.vn_text.normalize import normalize, strip_diacritics

_LOOSE_QUOTES = re.compile(r'["\\“”]')


def _loose(s: str) -> str:
    """Cùng cách so khớp với validator excerpt_substring (pipeline.llm.validators._norm): bỏ ngoặc kép/backslash,
    gộp khoảng trắng. Hai nơi lệch nhau thì output P5 qua validator nhưng hỏng ở đây và bước lỗi lặp mãi."""
    return re.sub(r"\s+", " ", _LOOSE_QUOTES.sub("", unicodedata.normalize("NFC", s))).strip()


KINDS = {"trai_nghiem", "so_lieu_noi_bo", "case_study", "quan_diem", "quy_trinh"}
REQUIRED = ("title", "content", "topic_tags", "valid_until", "kind")


class KnowledgeValidationError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass
class EntryInput:
    author_id: str
    kind: str
    title: str
    content: str
    topic_tags: list[str]
    valid_until: date
    facts: list[dict] = field(default_factory=list)
    evidence_ref: str | None = None
    site_id: str | None = None   # mục kho tầng site (pipeline.sites); author_id khi đó là "site:<id>"
    reusable: bool = False       # true: không giới hạn số bài dùng (knowledge.max_reuse_per_entry không áp dụng)


@dataclass
class ImportResult:
    entry_id: str
    pii_redacted: int
    warnings: list[str]


def _search_text(title: str, content: str, tags: Iterable[str]) -> str:
    return strip_diacritics(" ".join([title, content, *tags]))


def _validate(e: EntryInput, today: date) -> list[str]:
    errors = []
    for name in REQUIRED:
        if not getattr(e, name):
            errors.append(f"thiếu {name}")
    if e.kind and e.kind not in KINDS:
        errors.append(f"kind không hợp lệ: {e.kind}")
    if e.valid_until and e.valid_until < today:
        errors.append("valid_until đã qua")
    for i, f in enumerate(e.facts):
        missing = [k for k in ("label", "value", "unit", "as_of") if k not in f]
        if missing:
            errors.append(f"facts[{i}] thiếu {missing}")
    return errors


def import_entry(conn: psycopg.Connection, e: EntryInput, today: date | None = None,
                 strict_numbers: bool = True) -> ImportResult:
    """Chuẩn hoá → redact PII → kiểm tra số trong content đã khai trong facts[] → lưu.

    strict_numbers: số tiền/%/ngày trong content mà facts[] chưa khai sẽ làm number_scan override P8d.1
    khi bài dùng mục này, nên mặc định từ chối nhập để tác giả khai đủ.
    """
    today = today or date.today()
    if errors := _validate(e, today):
        raise KnowledgeValidationError(errors)
    title = normalize(e.title)
    content = normalize(e.content, keep_newlines=True)
    content, findings = redact(content)
    title, t_findings = redact(title)
    tags = sorted({strip_diacritics(t).strip() for t in e.topic_tags if t.strip()})
    facts = [{**f, "value": str(f["value"]), "unit": f.get("unit") or ""} for f in e.facts]

    warnings = []
    if missing := uncovered_numbers(content, facts):
        msg = f"số trong content chưa khai trong facts[]: {missing}"
        if strict_numbers:
            raise KnowledgeValidationError([msg])
        warnings.append(msg)

    with conn.transaction():
        seq = conn.execute("SELECT nextval('author_knowledge_seq') AS n").fetchone()["n"]
        entry_id = f"K{seq:04d}"
        conn.execute(
            """INSERT INTO author_knowledge (id, author_id, kind, title, content, facts, topic_tags, evidence_ref,
                   valid_until, pii_checked, search_text, site_id, reusable)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, true, %s, %s, %s)""",
            (entry_id, e.author_id, e.kind, title, content, Jsonb(facts), tags, e.evidence_ref, e.valid_until,
             _search_text(title, content, tags), e.site_id, e.reusable),
        )
    return ImportResult(entry_id, len(findings) + len(t_findings), warnings)


def expire_entries(conn: psycopg.Connection, today: date | None = None) -> list[str]:
    rows = conn.execute(
        "UPDATE author_knowledge SET status = 'expired', updated_at = now() "
        "WHERE status = 'active' AND valid_until < %s RETURNING id",
        (today or date.today(),),
    ).fetchall()
    return [r["id"] for r in rows]


def expiring_soon(conn: psycopg.Connection, days: int, today: date | None = None) -> list[dict]:
    today = today or date.today()
    return conn.execute(
        "SELECT id, author_id, title, valid_until FROM author_knowledge "
        "WHERE status = 'active' AND valid_until BETWEEN %s AND %s + %s ORDER BY valid_until",
        (today, today, days),
    ).fetchall()


# ---------- truy xuất ----------

@dataclass
class Candidate:
    entry_id: str
    kind: str
    title: str
    content: str
    topic_tags: list[str]
    valid_until: date
    score: float
    parts: dict[str, float]

    def to_prompt(self) -> dict[str, Any]:
        """Dạng đưa vào <ung_vien_kho> của P5."""
        return {"entry_id": self.entry_id, "kind": self.kind, "title": self.title, "content": self.content,
                "topic_tags": self.topic_tags, "valid_until": self.valid_until.isoformat()}


_TOKEN = re.compile(r"[a-z0-9]+")


def _tag_score(tags: list[str], ig_text: str) -> float:
    if not tags:
        return 0.0
    return sum(1 for t in tags if t and t in ig_text) / len(tags)


def retrieve(conn: psycopg.Connection, job_id: UUID, author_id: str, igs: list[dict], weights: dict[str, float],
             k: int = 8, max_reuse: int = 5, today: date | None = None,
             ig_embeddings: dict[str, list[float]] | None = None, site_id: str | None = None) -> dict[str, list[Candidate]]:
    """Lọc cứng (tác giả hoặc site của job, active, còn hạn, chưa dùng quá max_reuse) rồi chấm điểm lai cho từng IG.
    Mục kho tầng site (reusable) không bị giới hạn số bài dùng.

    Lượt đã giữ chỗ bởi job khác cũng tính vào max_reuse. Chưa có embedding thì trọng số vector
    được chia lại cho tag và full-text.
    """
    today = today or date.today()
    out: dict[str, list[Candidate]] = {}
    use_vec = bool(ig_embeddings)
    w = dict(weights)
    if not use_vec:
        rest = w.get("tag", 0) + w.get("fulltext", 0) or 1.0
        w = {"tag": w.get("tag", 0) / rest, "fulltext": w.get("fulltext", 0) / rest, "vector": 0.0}

    for ig in igs:
        ig_text = strip_diacritics(f"{ig.get('description', '')} {ig.get('inputs_needed', '')}")
        query = " | ".join(dict.fromkeys(_TOKEN.findall(ig_text))) or "_"
        vec = ig_embeddings.get(ig["ig_id"]) if use_vec else None
        rows = conn.execute(
            f"""SELECT id, kind, title, content, topic_tags, valid_until,
                       ts_rank(search_tsv, to_tsquery('simple', %(q)s), 32) AS ft
                       {", 1 - (embedding <=> %(vec)s::vector) AS cos" if vec else ""}
                FROM author_knowledge
                WHERE (author_id = %(author)s OR site_id = %(site)s) AND status = 'active' AND pii_checked
                  AND valid_until >= %(today)s
                  AND (reusable OR usage_count + (SELECT count(*) FROM knowledge_usage u
                                                  WHERE u.entry_id = author_knowledge.id AND u.status = 'reserved'
                                                    AND u.job_id <> %(job)s) < %(max_reuse)s)""",
            {"q": query, "vec": str(vec) if vec else None, "author": author_id, "today": today, "max_reuse": max_reuse,
             "job": job_id, "site": site_id},
        ).fetchall()
        cands = []
        for r in rows:
            parts = {"tag": _tag_score(r["topic_tags"], ig_text), "fulltext": float(r["ft"]),
                     "vector": float(r["cos"]) if vec and r.get("cos") is not None else 0.0}
            score = sum(w[key] * parts[key] for key in parts)
            if score > 0:
                cands.append(Candidate(r["id"], r["kind"], r["title"], r["content"], r["topic_tags"],
                                       r["valid_until"], score, parts))
        cands.sort(key=lambda c: c.score, reverse=True)
        out[ig["ig_id"]] = cands[:k]
    return out


# ---------- sau P5 ----------

def build_evidence(conn: psycopg.Connection, job_id: UUID, selection: dict, next_claim_index: int
                   ) -> tuple[dict, list[dict]]:
    """Dựng AuthorEvidence từ DB (không lấy nội dung từ output LLM) + claim kho cho FactInventory.

    Đồng thời giữ chỗ lượt dùng: xoá các reservation cũ của job (P5 chạy lại) rồi ghi mới.
    """
    items, claims = [], []
    entry_ids = [s["entry_id"] for s in selection["selections"]]
    if dup := {e for e in entry_ids if entry_ids.count(e) > 1}:
        raise KnowledgeValidationError([f"một mục dùng cho nhiều IG: {sorted(dup)}"])
    with conn.transaction():
        conn.execute("DELETE FROM knowledge_usage WHERE job_id = %s AND status = 'reserved'", (job_id,))
        for sel in selection["selections"]:
            row = conn.execute("SELECT * FROM author_knowledge WHERE id = %s", (sel["entry_id"],)).fetchone()
            if row is None:
                raise KnowledgeValidationError([f"entry_id không tồn tại: {sel['entry_id']}"])
            if _loose(sel["excerpt"]) not in _loose(row["content"]):
                raise KnowledgeValidationError([f"excerpt không phải chuỗi con của {sel['entry_id']}"])
            items.append({"entry_id": row["id"], "ig_id": sel["ig_id"], "kind": row["kind"],
                          "content": row["content"], "facts": row["facts"]})
            new_claims = knowledge_facts_to_claims(row["id"], row["facts"], next_claim_index + len(claims))
            claims.extend(new_claims)
            conn.execute(
                """INSERT INTO knowledge_usage (job_id, entry_id, ig_id, excerpt) VALUES (%s, %s, %s, %s)
                   ON CONFLICT (job_id, entry_id) DO NOTHING""",
                (job_id, row["id"], sel["ig_id"], sel["excerpt"]),
            )
    return {"items": items}, claims


def commit_usage(conn: psycopg.Connection, job_id: UUID) -> int:
    """Gọi khi PUBLISH thành công: reserved → committed, tăng usage_count."""
    with conn.transaction():
        rows = conn.execute(
            "UPDATE knowledge_usage SET status = 'committed' WHERE job_id = %s AND status = 'reserved' RETURNING entry_id",
            (job_id,),
        ).fetchall()
        ids = [r["entry_id"] for r in rows]
        if ids:
            conn.execute("UPDATE author_knowledge SET usage_count = usage_count + 1 WHERE id = ANY(%s)", (ids,))
    return len(ids)


def release_usage(conn: psycopg.Connection, job_id: UUID) -> int:
    """Gọi khi job bị huỷ."""
    cur = conn.execute("UPDATE knowledge_usage SET status = 'released' WHERE job_id = %s AND status = 'reserved'", (job_id,))
    return cur.rowcount
