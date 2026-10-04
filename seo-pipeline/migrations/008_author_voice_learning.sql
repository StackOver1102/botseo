-- Tác giả là người hay tổ chức (JSON-LD author @type), và hồ sơ giọng văn tự học dần từ bài tìm được khi search.
--   voice_origin = 'author': hồ sơ dựng từ bài của chính tác giả (author_tool.py voice) — không bao giờ bị ghi đè.
--   voice_origin = 'serp'  : hồ sơ học từ các trang top SERP của từng job nhánh B (voice_learning trong config);
--                            chỉ giữ mô tả phong cách, không giữ câu trích nguyên văn của site khác.
--   voice_learned_from     : danh sách job đã học (mỗi job chỉ tính một lần; đủ voice_learning.max_jobs thì dừng học).
ALTER TABLE authors ADD COLUMN kind text NOT NULL DEFAULT 'person' CHECK (kind IN ('person', 'organization'));
ALTER TABLE authors ADD COLUMN voice_origin text CHECK (voice_origin IN ('author', 'serp'));
ALTER TABLE authors ADD COLUMN voice_learned_from jsonb NOT NULL DEFAULT '[]';
UPDATE authors SET voice_origin = 'author' WHERE voice_profile IS NOT NULL;
