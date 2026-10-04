"""Tạo task thử cho extension rồi chờ kết quả — không cần chạy pipeline.

    .venv/bin/python scripts/ext_smoke_test.py --provider openai
    .venv/bin/python scripts/ext_smoke_test.py --provider gemini --kind json --timeout 600

--kind text : prompt ngắn, kiểm tra dán/gửi/đọc trả lời.
--kind json : prompt có <output_schema> giống task thật, kiểm tra extension lấy đúng JSON và code validate được.
--kind long : prompt ~30K ký tự, kiểm tra dán nội dung dài.
--kind deep : task P4-DR (Gemini), kiểm tra extension bật Deep Research, bấm Start research và lấy báo cáo (5–40 phút).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import jsonschema  # noqa: E402

from pipeline.db import connect  # noqa: E402
from pipeline.llm.base import LLMRequest  # noqa: E402
from pipeline.llm.external import render_prompt_text, strip_fences  # noqa: E402

SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["city", "population_note", "tips"],
    "properties": {"city": {"type": "string"}, "population_note": {"type": "string"},
                   "tips": {"type": "array", "items": {"type": "string"}}},
}


def build_prompt(kind: str, provider: str) -> str:
    if kind == "deep":
        return ("Nghiên cứu chủ đề \"vay tiền online\" cho người đọc Việt Nam năm 2026: câu hỏi thường gặp, "
                "quy định pháp lý hiện hành về cho vay tiêu dùng trực tuyến, dữ liệu mới nhất có nguồn chính thức. "
                "Liệt kê nguồn kèm URL và ngày. Báo cáo bằng tiếng Việt.")
    if kind == "text":
        return "Trả lời đúng một câu tiếng Việt: thủ đô của Việt Nam là gì?"
    user = "Giới thiệu ngắn về Đà Nẵng cho người đi du lịch lần đầu: tên thành phố, một câu về dân số, 3 mẹo."
    if kind == "long":
        user += "\n\n<du_lieu_tham_khao>\n" + ("Đà Nẵng là thành phố biển miền Trung. " * 800) + "\n</du_lieu_tham_khao>"
    req = LLMRequest(prompt_id="SMOKE", provider=provider, model="web", system="Bạn là trợ lý du lịch.", user=user,
                     schema=SCHEMA)
    return render_prompt_text(req)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=["openai", "gemini"], required=True)
    ap.add_argument("--kind", choices=["text", "json", "long", "deep"], default="json")
    ap.add_argument("--timeout", type=int, default=300)
    args = ap.parse_args()

    prompt = build_prompt(args.kind, args.provider)
    with connect(autocommit=True) as conn:
        task = conn.execute(
            """INSERT INTO external_tasks (idempotency_key, prompt_id, provider, model_hint, prompt_text)
               VALUES (%s, %s, %s, 'web', %s) RETURNING id""",
            (f"smoke:{uuid.uuid4()}", "P4-DR" if args.kind == "deep" else "SMOKE", args.provider, prompt)).fetchone()["id"]
        print(f"Đã tạo task {task} ({args.kind}, {len(prompt)} ký tự). Bấm 'Hỏi task ngay' trong extension hoặc chờ ≤30s…")

        start, last = time.time(), None
        while time.time() - start < args.timeout:
            row = conn.execute("SELECT * FROM external_tasks WHERE id = %s", (task,)).fetchone()
            if row["status"] != last:
                print(f"  [{int(time.time() - start):>3}s] {row['status']}" + (f" bởi {row['claimed_by']}" if row["claimed_by"] else ""))
                last = row["status"]
            if row["status"] in ("done", "failed"):
                break
            time.sleep(2)
        else:
            print("Hết thời gian chờ. Kiểm tra: API có chạy? token đúng? extension đã bật hãng này?")
            conn.execute("UPDATE external_tasks SET status = 'cancelled' WHERE id = %s", (task,))
            sys.exit(1)

    if row["status"] == "failed":
        print(f"Extension báo lỗi: {row['error']}")
        sys.exit(1)
    text = row["result_text"]
    print(f"\nKết quả ({len(text)} ký tự), meta={row['result_meta']}:\n{text[:1500]}")
    if args.kind not in ("text", "deep"):
        try:
            data = json.loads(strip_fences(text))
            jsonschema.validate(data, SCHEMA)
            print("\n✓ JSON hợp lệ theo schema")
        except (json.JSONDecodeError, jsonschema.ValidationError) as e:
            print(f"\n✗ JSON không hợp lệ: {e}\n(pipeline sẽ tạo task repair; nếu lỗi do extension lấy sai phần tử thì sửa extractText/selector)")
            sys.exit(1)


if __name__ == "__main__":
    main()
