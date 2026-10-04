"""Tự lấy tư liệu nhánh B từ top SERP (job.yaml `auto_source: N`).

Spec v1 để biên tập viên tự đưa bài nguồn; đây là tiện ích tùy chọn: chỉ tải vài URL top SERP (không crawl), tôn trọng
robots.txt (qua UrlVerifier), bỏ domain rác (nhà cái, soi kèo, mạng xã hội). Mỗi nguồn là một dòng source_texts riêng
nên fingerprint và containment tính theo từng nguồn; text nguồn vẫn chỉ vào P2a/P2b và bị xoá khi FACT_GATE approve (ADR-6).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit

from .inputs import SerpIn

MainText = Callable[[str], tuple[int | None, str]]


@dataclass(frozen=True)
class Source:
    rank: int
    url: str
    title: str
    text: str

    def as_source_text(self, index: int) -> str:
        # Header có số thứ tự 2 chữ số: source_texts sắp theo text khi created_at trùng (cùng transaction)
        return f"[Nguồn {index:02d} | {self.title} | {self.url}]\n{self.text}"


@dataclass(frozen=True)
class Skipped:
    url: str
    reason: str


def _blocked(url: str, cfg: dict) -> str | None:
    parts = urlsplit(url)
    host = parts.netloc.lower().removeprefix("www.")
    for d in cfg.get("block_domains", []):
        if host == d or host.endswith("." + d):
            return f"domain chặn ({d})"
    target = f"{host}{parts.path}".lower()
    for pat in cfg.get("block_url_patterns", []):
        if re.search(pat, target):
            return f"khớp mẫu chặn ({pat})"
    return None


_STOP = {"ở", "đâu", "là", "gì", "và", "có", "không", "của", "cho", "the", "a", "nào", "như", "thế", "bao", "nhiêu"}


def _coverage(text: str, query: str) -> float:
    """Tỷ lệ từ (bỏ dấu, bỏ từ hỏi) của truy vấn có mặt trong nội dung — loại trang lệch chủ đề (video trận khác…)."""
    from vnseo.vn_text.normalize import strip_diacritics
    words = {w for w in strip_diacritics(query.lower()).split() if w not in {strip_diacritics(x) for x in _STOP}}
    if not words:
        return 1.0
    body = set(re.findall(r"\w+", strip_diacritics(text.lower())))
    return len(words & body) / len(words)


def _spam(text: str, cfg: dict) -> str | None:
    """Trang lậu/nhà cái đổi domain liên tục nên lọc URL không đủ: đếm tên thương hiệu/từ cá cược trong nội dung."""
    low = text.lower()
    hits = sorted({m.group(0) for pat in cfg.get("block_text_patterns", []) for m in re.finditer(pat, low)})
    if len(hits) >= cfg.get("block_text_min_hits", 2):
        return f"nội dung phát lậu/cá cược ({', '.join(hits[:4])})"
    return None


def _cut(text: str, max_chars: int) -> str:
    """Cắt ở ranh giới đoạn/câu gần max_chars để P2a không nhận câu cụt."""
    if len(text) <= max_chars:
        return text
    head = text[:max_chars]
    cut = max(head.rfind("\n"), head.rfind(". "))
    return head[: cut + 1] if cut > max_chars * 0.6 else head


def collect_sources(serp: SerpIn, main_text: MainText, cfg: dict) -> tuple[list[Source], list[Skipped]]:
    """Duyệt SERP theo rank, lấy tối đa cfg.max_sources trang tải được và đủ dài; trang lỗi thì lấy trang kế tiếp."""
    want, min_chars, max_chars = cfg.get("max_sources", 5), cfg.get("min_chars", 1500), cfg.get("max_chars_per_source", 12000)
    sources, skipped = [], []
    seen_hosts = {urlsplit(u).netloc.lower().removeprefix("www.") for u in cfg.get("exclude_hosts", [])}
    for o in sorted(serp.organic, key=lambda o: o.rank):
        if len(sources) >= want:
            break
        host = urlsplit(o.url).netloc.lower().removeprefix("www.")
        if host in seen_hosts:  # một domain một nguồn cho đa dạng góc nhìn
            skipped.append(Skipped(o.url, "trùng domain"))
            continue
        if reason := _blocked(o.url, cfg):
            skipped.append(Skipped(o.url, reason))
            continue
        status, text = main_text(o.url)
        if status != 200:
            skipped.append(Skipped(o.url, f"không tải được (HTTP {status})" if status else "không tải được / robots.txt chặn"))
            continue
        if len(text) < min_chars:
            skipped.append(Skipped(o.url, f"nội dung quá ngắn ({len(text)} ký tự)" if text else "không trích được bài chính"))
            continue
        if (cov := _coverage(text, cfg.get("query", ""))) < cfg.get("min_query_coverage", 0.8):
            skipped.append(Skipped(o.url, f"không liên quan (chứa {cov:.0%} từ của truy vấn)"))
            continue
        if reason := _spam(text, cfg):
            skipped.append(Skipped(o.url, reason))
            continue
        seen_hosts.add(host)
        sources.append(Source(o.rank, o.url, o.title, _cut(text, max_chars)))
    return sources, skipped


class AutoSourceError(Exception):
    pass


def gather_sources(inp, config: dict, n: int, serp_query: str | None = None,
                   log=lambda msg: None) -> list[str]:
    """Lấy SERP theo truy vấn (gán vào inp.serp nếu chưa có, dùng luôn làm SERP của job) rồi tìm tối đa n trang làm
    bài nguồn; top 10 không đủ thì xem trang 2–3 và truy vấn theo topic. Gán inp.branch = "B", inp.source_urls.
    Trả về các source_text (mỗi nguồn một chuỗi, có header)."""
    from .serp import make_serp_provider
    from .urlcheck import UrlVerifier
    if inp.article_md:
        raise AutoSourceError("auto_source chỉ dùng cho nhánh B (job đã có bài gốc)")
    cfg = {**config["adapters"].get("auto_source", {}), "max_sources": int(n)}
    provider = make_serp_provider(config)
    query = serp_query or inp.topic
    if inp.serp is None:
        if provider is None:
            raise AutoSourceError("auto_source cần SERP: nhập serp.csv hoặc bật adapters.serp")
        inp.serp = provider.fetch(query)
    main_text = UrlVerifier().main_text
    sources, skipped = collect_sources(inp.serp, main_text, {**cfg, "query": query})
    rounds = [(query, 2), (query, 3)] + ([(inp.topic, 1), (inp.topic, 2)] if inp.topic != query else [])
    for q, page in rounds:
        if len(sources) >= int(n) or provider is None:
            break
        more = provider.fetch(q, page=page)
        seen = {x.url for x in sources} | {sk.url for sk in skipped}
        more.organic = [o for o in more.organic if o.url not in seen]
        extra, extra_skipped = collect_sources(more, main_text, {**cfg, "query": q, "max_sources": int(n) - len(sources),
                                                                 "exclude_hosts": [x.url for x in sources]})
        sources, skipped = sources + extra, skipped + extra_skipped
    for sk in skipped:
        log(f"nguồn: bỏ {sk.url[:90]} — {sk.reason}")
    for src in sources:
        log(f"nguồn: #{src.rank} {src.url[:90]} ({len(src.text)} ký tự)")
    if not sources:
        raise AutoSourceError("không lấy được trang nào làm tư liệu")
    inp.branch = "B"
    inp.source_urls = [src.url for src in sources]
    return [src.as_source_text(i) for i, src in enumerate(sources, 1)]
