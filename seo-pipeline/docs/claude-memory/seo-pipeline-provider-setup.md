---
name: seo-pipeline-provider-setup
description: "SEO pipeline (seo-pipeline/) gọi Claude qua API, còn GPT/Gemini chạy qua extension trình duyệt trên web UI"
metadata:
  node_type: memory
  type: project
  originSessionId: 8db0abe7-6d63-4752-abad-2491dc10f32a
  modified: 2026-09-28T03:59:51.998Z
---

User muốn chỉ gọi Claude bằng API; GPT và Gemini chạy qua extension Chrome tự viết (ChatGPT/Gemini web), nhưng vẫn giữ adapter API cho 2 hãng này như nhánh dự phòng (`providers.<hãng>.mode: api|external` trong config). Ngày 2026-09-28 user xác nhận extension chạy được trên cả ChatGPT và Gemini (selector Gemini đã sửa: nút gửi chỉ hiện khi ô nhập có chữ).

**Why:** tiết kiệm chi phí API của OpenAI/Google, tận dụng gói web.
**How to apply:** khi thêm bước mới dùng GPT/Gemini, luôn hỗ trợ cả mode external (prompt tự chứa `<output_schema>`, validate + repair phía code) lẫn api; selector DOM là phần dễ vỡ nhất.
