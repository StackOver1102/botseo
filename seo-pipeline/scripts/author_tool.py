"""Quản lý tác giả (bảng authors): thêm tác giả, dựng hồ sơ giọng văn từ bài thật, nạp kho dữ liệu.

    python scripts/author_tool.py list
    python scripts/author_tool.py add "Ban biên tập KQBD" --url https://site.vn/gioi-thieu [--id ban-bien-tap]
    python scripts/author_tool.py voice ban-bien-tap https://site.vn/bai-1 https://site.vn/bai-2 bai-3.md
    python scripts/author_tool.py knowledge ban-bien-tap kho.yaml        # định dạng: examples/vay-tien-online/knowledge.yaml

voice: tải 2–5 bài do CHÍNH tác giả viết (URL hoặc file .md/.txt), chạy prompt P3 một lần và lưu hồ sơ vào tác giả.
Mọi job chọn tác giả đó dùng lại hồ sơ này (không chạy P3 mỗi bài, nhánh B không cần voice_samples_md).
Mẫu phải là bài thật của tác giả: hồ sơ gồm các câu trích nguyên văn, code kiểm tra từng câu có trong mẫu.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from knowledge.repository import EntryInput, KnowledgeValidationError, import_entry  # noqa: E402
from pipeline.authors import AuthorError, get_author, list_authors, set_voice_profile, upsert_author  # noqa: E402
from pipeline.config import load_config  # noqa: E402
from pipeline.db import connect, migrate  # noqa: E402
from pipeline.llm.base import LLMRequest  # noqa: E402
from pipeline.llm.runner import choose_model, make_provider_factory  # noqa: E402
from pipeline.llm.validators import validate_output  # noqa: E402
from pipeline.prompts import load_registry, load_schemas, load_shared, render, render_spec  # noqa: E402
from vnseo.style import sentence_stats  # noqa: E402
from vnseo.vn_text.normalize import normalize  # noqa: E402

MAX_SAMPLE_CHARS = 30_000  # tổng mẫu đưa vào P3; bài dài hơn thì cắt bớt từng bài cho đều
MIN_SAMPLE_WORDS = 600


def load_samples(sources: list[str]) -> str:
    from pipeline.urlcheck import UrlVerifier
    verifier, parts = UrlVerifier(), []
    for src in sources:
        if src.startswith(("http://", "https://")):
            status, md = verifier.article_markdown(src)
            if status != 200 or len(md.split()) < 80:
                sys.exit(f"không lấy được bài từ {src} (HTTP {status}, {len(md.split())} chữ) — lưu bài thành file .md rồi đưa đường dẫn file")
        else:
            md = Path(src).read_text(encoding="utf-8")
        md = normalize(md, keep_newlines=True)
        print(f"  mẫu: {src} — {len(md.split())} chữ")
        parts.append(md)
    budget = MAX_SAMPLE_CHARS // len(parts)
    samples = "\n\n---\n\n".join(p[:budget] for p in parts)
    if len(samples.split()) < MIN_SAMPLE_WORDS:
        sys.exit(f"mẫu quá ít ({len(samples.split())} chữ, cần ≥{MIN_SAMPLE_WORDS}): thêm bài của tác giả")
    return samples


def build_voice_profile(config: dict, conn, samples: str) -> dict:
    """Chạy P3 ngoài job (không ghi prompt_runs): gọi model, validate, repair tối đa thresholds.repair_attempts lần."""
    spec, schemas, shared = load_registry()["P3"], load_schemas(), load_shared()
    loc = config["locale"]
    variables = {"TONE_STYLE": "mới (hoà, thuỷ)" if loc["tone_style"] == "new" else "cũ (hòa, thủy)",
                 "TONE_EXAMPLE": loc["tone_example"], "DATAMARK": "¦",
                 "VOICE_SAMPLES_MD": samples,
                 "SENTENCE_STATS_JSON": json.dumps(sentence_stats(samples).__dict__, ensure_ascii=False)}
    system, user = render_spec(spec, variables, shared)
    choice = choose_model(config, spec.model_key)
    provider = make_provider_factory(conn, config)(choice.provider, None, "AUTHOR_VOICE")
    schema = schemas[spec.output]
    key = "author-voice:" + hashlib.sha256(samples.encode()).hexdigest()
    current, repairs = user, config["thresholds"].get("repair_attempts", 2)
    for attempt in range(repairs + 1):
        req = LLMRequest(prompt_id="P3", provider=choice.provider, model=choice.model, system=system, user=current,
                         schema=schema, schema_name=spec.output, max_output_tokens=spec.max_output(choice.provider),
                         effort=choice.effort)
        result = provider.complete(req, f"{key}:{attempt}")
        rep = validate_output(result.text, schema, spec.validators, {"source_text": samples},
                              lenient_json=result.stop_reason == "external")
        print(f"  P3 lượt {attempt + 1} ({choice.model}): {'đạt' if rep.ok else f'{len(rep.errors)} lỗi'}"
              + (f", {result.cost_usd:.4f} USD" if result.cost_usd else ""))
        if rep.ok:
            return rep.parsed
        current = user + "\n\n" + render(shared["repair_v1"], {
            "PREVIOUS_OUTPUT": result.text, "VALIDATOR_ERRORS": "\n".join(rep.errors[:50])}, shared)
    sys.exit("P3 không tạo được hồ sơ hợp lệ: " + "; ".join(rep.errors[:5]))


def import_knowledge(conn, author_id: str, path: str) -> None:
    entries = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or []
    for e in entries:
        exists = conn.execute("SELECT id FROM author_knowledge WHERE author_id = %s AND title = %s AND status = 'active'",
                              (author_id, e["title"])).fetchone()
        if exists:
            print(f"  bỏ qua '{e['title']}' (đã có {exists['id']})")
            continue
        try:
            r = import_entry(conn, EntryInput(
                author_id=author_id, kind=e["kind"], title=e["title"], content=e["content"], topic_tags=e["topic_tags"],
                valid_until=e["valid_until"], facts=e.get("facts") or [], evidence_ref=e.get("evidence_ref")))
            print(f"  nạp {r.entry_id} '{e['title']}'" + (f" (che {r.pii_redacted} thông tin cá nhân)" if r.pii_redacted else ""))
        except KnowledgeValidationError as err:
            sys.exit(f"  LỖI '{e['title']}': {err}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    a = sub.add_parser("add"); a.add_argument("name"); a.add_argument("--url"); a.add_argument("--id")
    a.add_argument("--org", action="store_true", help="tác giả là tổ chức (ban biên tập, thương hiệu), không phải một người")
    v = sub.add_parser("voice"); v.add_argument("author_id"); v.add_argument("sources", nargs="+")
    k = sub.add_parser("knowledge"); k.add_argument("author_id"); k.add_argument("file")
    args = ap.parse_args()

    config = load_config()
    conn = connect(autocommit=True)
    migrate(conn)
    try:
        if args.cmd == "list":
            rows = list_authors(conn, include_inactive=True)
            for r in rows:
                voice = ("CHƯA" if not r["has_voice"] else "bài của tác giả" if r["voice_origin"] == "author"
                         else f"tự học {r['voice_learned_jobs']} job")
                print(f"{r['id']:<28} {r['name']:<32} [{r['kind']}] giọng văn: {voice}  kho: {r['knowledge_entries']} mục"
                      f"  {r['url'] or ''}{'' if r['active'] else '  (đã ẩn)'}")
            if not rows:
                print('chưa có tác giả — thêm bằng: author_tool.py add "Tên tác giả" --url https://…')
        elif args.cmd == "add":
            print("đã lưu tác giả:", upsert_author(conn, args.name, args.url, args.id, "organization" if args.org else "person"))
        else:
            if get_author(conn, args.author_id) is None:
                sys.exit(f"không có tác giả '{args.author_id}' (xem: author_tool.py list)")
            if args.cmd == "voice":
                samples = load_samples(args.sources)
                profile = build_voice_profile(config, conn, samples)
                set_voice_profile(conn, args.author_id, profile, "; ".join(args.sources))
                ok = sum(1 for d in profile["dimensions"] if d["status"] == "DU_MAU")
                print(f"đã lưu hồ sơ giọng văn cho {args.author_id}: {ok}/10 chiều đủ mẫu")
            else:
                import_knowledge(conn, args.author_id, args.file)
    except AuthorError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
