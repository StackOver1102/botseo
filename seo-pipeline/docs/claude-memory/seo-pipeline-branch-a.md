---
name: seo-pipeline-branch-a
description: Nhánh A (bài có sẵn của site) phải chạy luồng full như B nhưng outline + writer thấy bài gốc; rework chỉ là tuỳ chọn
metadata:
  node_type: memory
  type: project
  originSessionId: b9f3f031-9557-4d2e-9017-d292632b57d9
  modified: 2026-10-03T13:57:07.176Z
---

Quyết định của user (2026-10-02/03): nhánh A = "full luồng như B nhưng cải tiến" (mặc định), không phải chỉ rework câu chữ.
Yêu cầu cứng: bài mới **không ngắn hơn bài gốc**, giữ đủ ý/section gốc, keyword + triage **tự động** (không nhập tay),
không dừng chờ người. Rework (chỉ đổi câu chữ, ~0,1 USD) giữ lại như lựa chọn phụ trên UI.

**Why:** luồng full cũ xử lý A như B (P4/P6 không thấy bài gốc) → bài kqbd 14 heading còn 3, 1.173/2.236 chữ; rework thuần
thì không cập nhật số liệu / thêm ý mới như spec "refresh, giữ URL".
**How to apply:** khi sửa nhánh A, giữ nguyên nguyên tắc P4 nhận khung bài gốc (`source_sections`) và P6-A viết từng
section từ đoạn gốc; mọi thay đổi đo trên bài kqbd và so với số liệu trong `seo-pipeline/HANDOFF.md` (mục 5b).
Chi phí là mối lo lớn của user ("1 bài 1 đô là quá nhiều") — luôn báo chi phí đo được. Liên quan [[seo-pipeline-fully-automated]], [[seo-pipeline-provider-setup]].
