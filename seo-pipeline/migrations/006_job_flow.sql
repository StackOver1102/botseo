-- Luồng xử lý của job: full = pipeline SEO đầy đủ (B0→B12); rework = viết lại bài có sẵn (REWORK → REWORK_CHECK → DONE),
-- không keyword map / SERP / title / meta.
ALTER TABLE jobs ADD COLUMN flow text NOT NULL DEFAULT 'full' CHECK (flow IN ('full', 'rework'));
