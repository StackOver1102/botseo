---
name: seo-pipeline-fully-automated
description: "SEO pipeline phải chạy tự động 100%, không bao giờ dừng chờ người (NEEDS_HUMAN); lỗi thì tự quay về bước gây lỗi chạy lại"
metadata:
  node_type: memory
  type: feedback
  originSessionId: 7edd23c7-2398-4d44-8f1e-4b97b5116477
  modified: 2026-09-29T02:57:42.806Z
---

User (2026-09-29): "dừng thì phải quay lại bước gây lỗi chạy lại r chạy tiếp chứ đừng dừng job của tôi hệ thống tôi sẽ k có người dùng tay đâu auto hết". Trước đó user cũng nói "cần làm liền mạch đừng có dừng lại" và phản đối placeholder [CẦN NGƯỜI].

**Why:** hệ thống vận hành không có biên tập viên; job dừng ở NEEDS_HUMAN = job chết. Nhưng cũng không muốn bài dở lọt ra (bài bóng đá thiếu "90 phút" bị chê).
**How to apply:** mọi nhánh escalate/NEEDS_HUMAN mới phải có đường tự phục hồi: rerun, hoặc rewind về bước gốc gây lỗi kèm feedback, có giới hạn số vòng; hết vòng thì đi tiếp kèm Warnings chứ không dừng. Không thêm bước cần người duyệt. Liên quan [[seo-pipeline-provider-setup]].
