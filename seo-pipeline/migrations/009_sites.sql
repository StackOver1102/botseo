-- Nhiều website: mỗi site có hồ sơ riêng (thương hiệu, tác giả mặc định, trang nội bộ để chèn link, kho dữ liệu).
-- Job chọn site → tác giả mặc định, trang nội bộ và kho dữ liệu của site đi theo; dữ liệu site A không lọt sang bài site B.
CREATE TABLE sites (
    id                text PRIMARY KEY CHECK (id ~ '^[a-z0-9][a-z0-9.-]{1,60}$'),
    name              text NOT NULL CHECK (length(btrim(name)) > 0),
    url               text NOT NULL,
    default_author_id text REFERENCES authors (id),
    internal_pages    jsonb NOT NULL DEFAULT '[]',   -- [{url, title}] — JobInput.internal_pages của mọi job thuộc site
    active            boolean NOT NULL DEFAULT true,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE jobs ADD COLUMN site_id text REFERENCES sites (id);
-- Tác giả thuộc một site (NULL = dùng chung mọi site). Giọng văn tự học gắn theo tác giả nên cũng tách theo site.
ALTER TABLE authors ADD COLUMN site_id text REFERENCES sites (id);
-- Kho dữ liệu hai tầng:
--   tầng site  (site_id có giá trị, reusable = true): tính năng thật, quy trình, cam kết của site — nhập một lần, mọi bài
--               của site dùng được, không giới hạn số bài (knowledge.max_reuse_per_entry không áp dụng).
--   tầng tác giả (site_id NULL): trải nghiệm / số liệu riêng của một tác giả, mỗi mục dùng tối đa max_reuse_per_entry bài.
ALTER TABLE author_knowledge ADD COLUMN site_id text REFERENCES sites (id);
ALTER TABLE author_knowledge ADD COLUMN reusable boolean NOT NULL DEFAULT false;
CREATE INDEX ON author_knowledge (site_id, status, valid_until) WHERE site_id IS NOT NULL;
