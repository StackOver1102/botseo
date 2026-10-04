"""Quản lý website (bảng sites): thêm site, trang nội bộ, kho dữ liệu tầng site.

    python scripts/site_tool.py list
    python scripts/site_tool.py add "KQBD2" https://kqbd2.com [--id kqbd2.com] [--author ban-bien-tap]
    python scripts/site_tool.py pages kqbd2.com --sitemap [--limit 40]      # trang nội bộ từ sitemap.xml (để chèn link)
    python scripts/site_tool.py pages kqbd2.com trang.csv                   # hoặc từ CSV: url,title
    python scripts/site_tool.py draft-kho kqbd2.com https://kqbd2.com/gioi-thieu https://kqbd2.com/…
    python scripts/site_tool.py kho kqbd2.com sites/kqbd2.com/kho.draft.yaml

Kho tầng site = thông tin có thật về chính website (tính năng, quy trình cập nhật, cam kết, số liệu site tự công bố).
Mọi bài của site dùng được, không giới hạn số bài. Quy trình hai bước, có người duyệt ở giữa:
  1. draft-kho: tải các trang CỦA CHÍNH SITE, model chỉ được CHÉP NGUYÊN VĂN câu có trong trang (code kiểm tra từng câu,
     câu nào không có trong trang bị loại) → ghi file nháp sites/<site>/kho.draft.yaml.
  2. Bạn mở file nháp, xoá mục không muốn bài nhắc tới, sửa nhãn số liệu; rồi chạy `kho` để nạp.
Số liệu trong kho được coi là dữ kiện đã duyệt của site (không qua fact-check web) — chỉ giữ con số bạn chịu trách nhiệm.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from knowledge.repository import KINDS, EntryInput, KnowledgeValidationError, import_entry  # noqa: E402
from pipeline.config import load_config  # noqa: E402
from pipeline.db import connect, migrate  # noqa: E402
from pipeline.llm.base import LLMRequest  # noqa: E402
from pipeline.llm.runner import choose_model, make_provider_factory  # noqa: E402
from pipeline.llm.validators import _contains, validate_output  # noqa: E402
from pipeline.sites import SiteError, get_site, list_sites, set_internal_pages, site_owner, upsert_site  # noqa: E402
from vnseo.facts import OVERRIDE_KINDS, claim_value_keys, scan_numbers  # noqa: E402

DRAFT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object", "additionalProperties": False,
    "required": ["entries"],
    "properties": {"entries": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["title", "kind", "content", "topic_tags", "source_url"],
        "properties": {
            "title": {"type": "string"}, "kind": {"enum": sorted(KINDS)}, "content": {"type": "string"},
            "topic_tags": {"type": "array", "items": {"type": "string"}}, "source_url": {"type": "string"}}}}},
}
DRAFT_SYSTEM = """Bạn lập kho dữ liệu về một website từ nội dung các trang của chính website đó.
Mỗi mục là một thông tin CÓ THẬT về website: tính năng, phạm vi nội dung, quy trình cập nhật, cam kết, số liệu site tự công bố.
- content: 1–3 câu CHÉP NGUYÊN VĂN, liền nhau, từ đúng một trang (không sửa chữ, không gộp câu ở hai chỗ khác nhau,
  không thêm chữ nào). Code sẽ so khớp từng ký tự với trang; mục không khớp bị loại.
- Bỏ qua menu, quảng cáo, tin tức theo ngày, tỷ số trận đấu, lời kêu gọi chung chung không có thông tin.
- title: nhãn ngắn mô tả mục. kind: quy_trinh (cách site làm việc), so_lieu_noi_bo (con số site tự công bố),
  quan_diem (cam kết, định hướng), case_study, trai_nghiem. topic_tags: 2–5 từ khoá không dấu hoặc có dấu.
- source_url: URL của trang chứa câu đó. Tối đa 12 mục, chọn mục cụ thể và hữu ích nhất cho người viết bài về site."""
_VALUE_UNIT = re.compile(r"^\$?\s*([\d.,/]+)\s*(.*)$")


def check_draft_entries(entries: list[dict], pages: dict[str, str], today: dt.date) -> tuple[list[dict], list[str]]:
    """Giữ mục có content là đoạn nguyên văn của một trang đã tải; tự khai facts cho số %, tiền, ngày trong content
    (import_entry bắt buộc khai — nhãn để người duyệt sửa). Trả (mục hợp lệ dạng kho, lý do loại)."""
    kept, dropped = [], []
    for e in entries:
        content = (e.get("content") or "").strip()
        page = pages.get(e.get("source_url") or "")
        if page is None:  # model ghi sai URL: tìm trang thật sự chứa câu đó
            page = next((t for t in pages.values() if _contains(t, content)), None)
        if not content or page is None or not _contains(page, content):
            dropped.append(f"không có nguyên văn trong trang: {content[:80]!r}")
            continue
        url = next(u for u, t in pages.items() if t is page)
        facts = []
        for h in scan_numbers(content):
            if h.kind not in OVERRIDE_KINDS:
                continue
            m = _VALUE_UNIT.match(h.raw.strip())
            value, unit = (m.group(1), m.group(2).strip()) if m else (h.raw, "")
            if claim_value_keys(f"{value} {unit}") and not any(f["value"] == value and f["unit"] == unit for f in facts):
                facts.append({"label": "(sửa nhãn) " + h.raw, "value": value, "unit": unit, "as_of": today.strftime("%Y-%m")})
        kept.append({"title": e["title"].strip(), "kind": e["kind"], "content": content,
                     "topic_tags": [t.strip() for t in e.get("topic_tags") or [] if t.strip()] or ["website"],
                     "valid_until": (today + dt.timedelta(days=365)).isoformat(), "facts": facts, "evidence_ref": url})
    return kept, dropped


def fetch_site_pages(site: dict, urls: list[str]) -> dict[str, str]:
    from pipeline.urlcheck import UrlVerifier
    host = urlsplit(site["url"]).netloc.lower().removeprefix("www.")
    verifier, pages = UrlVerifier(), {}
    for url in urls:
        if urlsplit(url).netloc.lower().removeprefix("www.") != host:
            sys.exit(f"{url} không thuộc site {host}: kho tầng site chỉ lấy từ trang của chính site")
        status, text = verifier.main_text(url)
        if status != 200 or len(text.split()) < 60:
            status, text = verifier.fetch_text(url)  # trang chủ/landing: không có "bài chính", lấy text cả trang
        if status != 200 or len(text.split()) < 60:
            print(f"  bỏ qua {url} (HTTP {status}, {len(text.split())} chữ)")
            continue
        pages[url] = text[:20000]
        print(f"  đã tải {url} — {len(text.split())} chữ")
    if not pages:
        sys.exit("không tải được trang nào")
    return pages


def draft_knowledge(config: dict, conn, site: dict, pages: dict[str, str]) -> list[dict]:
    choice = choose_model(config, "knowledge_draft" if "knowledge_draft" in config["models"] else "voice")
    provider = make_provider_factory(conn, config)(choice.provider, None, "SITE_KNOWLEDGE")
    user = (f"Website: {site['name']} ({site['url']})\n\n"
            + "\n\n".join(f'<trang url="{u}">\n{t}\n</trang>' for u, t in pages.items())
            + "\n\nLập danh sách entries theo đúng quy tắc.")
    req = LLMRequest(prompt_id="SITE-KHO", provider=choice.provider, model=choice.model, system=DRAFT_SYSTEM, user=user,
                     schema=DRAFT_SCHEMA, schema_name="SiteKnowledgeDraft", max_output_tokens=6000, effort=choice.effort)
    result = provider.complete(req, "site-kho:" + hashlib.sha256(user.encode()).hexdigest())
    rep = validate_output(result.text, DRAFT_SCHEMA, ["jsonschema"], {}, lenient_json=result.stop_reason == "external")
    if not rep.ok:
        sys.exit("model trả kết quả sai định dạng: " + "; ".join(rep.errors[:5]))
    print(f"  model ({choice.model}) đề xuất {len(rep.parsed['entries'])} mục"
          + (f", {result.cost_usd:.4f} USD" if result.cost_usd else ""))
    return rep.parsed["entries"]


def sitemap_pages(site: dict, limit: int) -> list[dict]:
    """URL từ sitemap.xml (kể cả sitemap index, tối đa 5 sitemap con); tiêu đề lấy từ <title> của từng trang."""
    from pipeline.urlcheck import UrlVerifier
    http = UrlVerifier().http
    base = site["url"].rstrip("/")
    queue, urls = [f"{base}/sitemap.xml"], []
    for _ in range(6):
        if not queue or len(urls) >= limit:
            break
        r = http.get(queue.pop(0))
        if r.status_code != 200:
            continue
        locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", r.text)
        if "<sitemapindex" in r.text:
            queue += locs[:5]
        else:
            urls += [u for u in locs if u not in urls]
    pages = []
    for url in urls[:limit]:
        r = http.get(url)
        m = re.search(r"<title[^>]*>(.*?)</title>", r.text, re.S | re.I) if r.status_code == 200 else None
        title = re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
        title = re.split(r"\s+[|–—-]\s+", title)[0].strip()  # bỏ đuôi " | Tên site"
        if title:
            pages.append({"url": url, "title": title[:120]})
            print(f"  {url} — {title[:70]}")
    return pages


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    a = sub.add_parser("add"); a.add_argument("name"); a.add_argument("url"); a.add_argument("--id"); a.add_argument("--author")
    p = sub.add_parser("pages"); p.add_argument("site"); p.add_argument("file", nargs="?")
    p.add_argument("--sitemap", action="store_true"); p.add_argument("--limit", type=int, default=40)
    d = sub.add_parser("draft-kho"); d.add_argument("site"); d.add_argument("urls", nargs="+")
    k = sub.add_parser("kho"); k.add_argument("site"); k.add_argument("file")
    args = ap.parse_args()

    config = load_config()
    conn = connect(autocommit=True)
    migrate(conn)
    try:
        if args.cmd == "list":
            rows = list_sites(conn, include_inactive=True)
            for r in rows:
                print(f"{r['id']:<26} {r['name']:<24} kho: {r['knowledge_entries']} mục  trang nội bộ: {r['internal_pages']}  "
                      f"tác giả mặc định: {r['default_author_id'] or '—'}  {r['url']}")
            if not rows:
                print('chưa có site — thêm bằng: site_tool.py add "Tên site" https://ten-site.vn')
            return
        if args.cmd == "add":
            print("đã lưu site:", upsert_site(conn, args.name, args.url, args.id, args.author))
            return
        site = get_site(conn, args.site)
        if site is None:
            sys.exit(f"không có site '{args.site}' (xem: site_tool.py list)")
        if args.cmd == "pages":
            if args.sitemap:
                pages = sitemap_pages(site, args.limit)
            elif args.file:
                rows = csv.DictReader(io.StringIO(Path(args.file).read_text(encoding="utf-8-sig")))
                pages = [{"url": r.get("url", ""), "title": r.get("title", "")} for r in rows]
            else:
                sys.exit("cần --sitemap hoặc file CSV (cột: url,title)")
            print(f"đã lưu {set_internal_pages(conn, site['id'], pages)} trang nội bộ cho {site['id']}")
        elif args.cmd == "draft-kho":
            pages = fetch_site_pages(site, args.urls)
            entries, dropped = check_draft_entries(draft_knowledge(config, conn, site, pages), pages, dt.date.today())
            for why in dropped:
                print("  loại:", why)
            out = ROOT / "sites" / site["id"] / "kho.draft.yaml"
            out.parent.mkdir(parents=True, exist_ok=True)
            header = (f"# Kho dữ liệu NHÁP của site {site['id']} — tạo {dt.date.today()} từ {len(pages)} trang của site.\n"
                      "# Mọi content là câu nguyên văn trên site (đã được code so khớp). Trước khi nạp:\n"
                      "#   - XOÁ mục bạn không muốn bài viết nhắc tới (nhất là số liệu tự công bố không còn đúng);\n"
                      "#   - sửa label trong facts (đang là \"(sửa nhãn) …\");\n"
                      f"# rồi chạy: python scripts/site_tool.py kho {site['id']} {out.relative_to(ROOT).as_posix()}\n")
            out.write_text(header + yaml.safe_dump(entries, allow_unicode=True, sort_keys=False, width=110), encoding="utf-8")
            print(f"đã ghi {len(entries)} mục nháp vào {out} — mở file, duyệt rồi nạp bằng lệnh `kho`")
        else:  # kho
            entries = yaml.safe_load(Path(args.file).read_text(encoding="utf-8")) or []
            for e in entries:
                exists = conn.execute("SELECT id FROM author_knowledge WHERE site_id = %s AND title = %s AND status = 'active'",
                                      (site["id"], e["title"])).fetchone()
                if exists:
                    print(f"  bỏ qua '{e['title']}' (đã có {exists['id']})")
                    continue
                try:
                    r = import_entry(conn, EntryInput(
                        author_id=site_owner(site["id"]), site_id=site["id"], reusable=True, kind=e["kind"], title=e["title"],
                        content=e["content"], topic_tags=e["topic_tags"], valid_until=dt.date.fromisoformat(str(e["valid_until"])),
                        facts=e.get("facts") or [], evidence_ref=e.get("evidence_ref")))
                    print(f"  nạp {r.entry_id} '{e['title']}'")
                except KnowledgeValidationError as err:
                    sys.exit(f"  LỖI '{e['title']}': {err}")
    except SiteError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
