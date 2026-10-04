-- Danh sách tác giả: job chọn tác giả từ đây (UI / job.yaml author_id) thay cho default_author trong config.
-- Mỗi tác giả mang tên + URL (JSON-LD author), hồ sơ giọng văn dùng lại cho mọi bài (khỏi chạy P3 mỗi job,
-- nhánh B không cần voice_samples_md) và kho dữ liệu riêng (author_knowledge.author_id = authors.id).
CREATE TABLE authors (
    id            text PRIMARY KEY CHECK (id ~ '^[a-z0-9][a-z0-9-]{1,60}$'),
    name          text NOT NULL CHECK (length(btrim(name)) > 0),
    url           text,
    voice_profile jsonb,          -- VoiceProfile (schema v5) dựng từ bài thật của tác giả; NULL = chưa có
    voice_source  text,           -- các bài mẫu đã dùng để dựng hồ sơ giọng (URL / tên file)
    active        boolean NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
