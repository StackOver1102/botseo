-- Key bắt buộc ở cấp gốc của JSON kết quả: extension dùng để nhận ra câu trả lời theo nội dung,
-- không phụ thuộc cấu trúc DOM của ChatGPT/Gemini. Rỗng = task trả Markdown.
ALTER TABLE external_tasks ADD COLUMN expect_keys text[] NOT NULL DEFAULT '{}';
