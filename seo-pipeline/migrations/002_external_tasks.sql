-- Nhánh external: task cho extension trình duyệt (ChatGPT / Gemini web) lấy prompt và gửi kết quả về.
CREATE TABLE external_tasks (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    idempotency_key text NOT NULL UNIQUE,
    job_id          uuid REFERENCES jobs (id),
    step            text,
    prompt_id       text NOT NULL,
    provider        text NOT NULL CHECK (provider IN ('openai', 'gemini')),
    model_hint      text NOT NULL,
    grounding       boolean NOT NULL DEFAULT false,
    prompt_text     text NOT NULL,          -- system + user + <output_schema>, dán thẳng vào ô chat
    status          text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'claimed', 'done', 'failed', 'cancelled')),
    claimed_by      text,
    lease_until     timestamptz,
    failures        int NOT NULL DEFAULT 0,
    result_text     text,
    result_meta     jsonb,                  -- url nguồn extension đọc được (grounding), model thực tế…
    error           text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    completed_at    timestamptz
);
CREATE INDEX external_tasks_queue_idx ON external_tasks (provider, created_at) WHERE status IN ('pending', 'claimed');
