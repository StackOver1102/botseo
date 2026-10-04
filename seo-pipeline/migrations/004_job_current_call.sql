-- Lượt gọi model API đang chạy (UI theo dõi): {"prompt_id", "provider", "model", "attempt", "started_at"}.
-- Task qua extension đã có trong external_tasks nên không ghi ở đây.
ALTER TABLE jobs ADD COLUMN current_call jsonb;
