# SEO Pipeline Bridge (extension Chrome, MV3)

Chạy các task `mode: external` của pipeline trên ChatGPT / Gemini web rồi gửi kết quả về.

## Cài đặt
1. Chạy API: `SEO_EXTENSION_TOKEN=<token> .venv/bin/uvicorn app.main:app --port 8000`
2. Mở `chrome://extensions` → bật Developer mode → **Load unpacked** → chọn thư mục `extension/`.
3. Mở trang tuỳ chọn của extension, nhập API (`http://localhost:8000`) và token, bật các hãng cần dùng.
4. Đăng nhập sẵn chatgpt.com / gemini.google.com trên cùng profile Chrome.

## Luồng
`background.js` hỏi `GET /external/tasks/next` mỗi 30s cho từng hãng → mở tab chat mới (chạy nền) →
`content/<site>.js` dán `prompt_text`, bấm gửi, chờ trả lời đứng yên 4s → lấy JSON trong khối code
(không có thì lấy text) và các link nguồn → `POST /result`. Lỗi bất kỳ → `POST /fail` (server cho thử lại tối đa
`max_task_failures` lần). Mỗi hãng chỉ chạy 1 task một lúc.

## Cần chỉnh khi dùng thật
- **Selector** trong `content/chatgpt.js`, `content/gemini.js` phụ thuộc giao diện web và sẽ hỏng khi site đổi DOM.
  Mỗi adapter chỉ gồm 5 hàm: `input`, `sendButton`, `isGenerating`, `lastResponse`, `modelName`.
- **Chọn model / bật tìm kiếm** (task có `grounding: true` là P4-G, P10): thêm `beforeSend(task)` vào adapter để
  chọn model theo `task.model_hint` và bật Search/Deep Research trên UI.
- Prompt rất dài có thể bị ChatGPT chuyển thành file đính kèm; vẫn chạy được nhưng nên kiểm tra.
- Tự động hoá web UI có thể trái điều khoản sử dụng của OpenAI/Google; chuyển `providers.<hãng>.mode: api`
  trong `config/config.yaml` khi cần gọi API chính thức.
