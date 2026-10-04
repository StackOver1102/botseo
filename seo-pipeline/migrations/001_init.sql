-- Schema v5 (§3). Bổ sung so với spec:
--   jobs.urgent, run_after, attempts, last_error, topic      (queue + batch)
--   source_texts tách khỏi artifacts để xoá được mà không phá tính bất biến của artifacts (ADR-2, ADR-6)
--   knowledge_usage.status (reserved → committed khi publish) để revise/huỷ job không đốt quota tái sử dụng
--   author_knowledge.search_text (không dấu) cho full-text

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE jobs (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    topic              text NOT NULL,
    branch             text CHECK (branch IN ('A', 'B')),
    mode               text CHECK (mode IN ('lite', 'standard', 'heavy')),
    ymyl               boolean,
    author_id          text NOT NULL,
    state              text NOT NULL DEFAULT 'TRIAGE_AI',
    resume_state       text,
    needs_human_reason text,
    revise_count       jsonb NOT NULL DEFAULT '{}',
    fix_round          jsonb NOT NULL DEFAULT '{}',
    cost_usd           numeric(12, 6) NOT NULL DEFAULT 0,
    cost_cap_usd       numeric(12, 6) NOT NULL,
    urgent             boolean NOT NULL DEFAULT false,
    run_after          timestamptz NOT NULL DEFAULT now(),
    lease_until        timestamptz,
    leased_by          text,
    attempts           int NOT NULL DEFAULT 0,
    last_error         text,
    created_by         text NOT NULL,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX jobs_ready_idx ON jobs (run_after)
    WHERE state NOT IN ('DONE', 'CANCELLED', 'NEEDS_HUMAN', 'STALE');
CREATE INDEX jobs_waiting_idx ON jobs (updated_at) WHERE state IN ('NEEDS_HUMAN', 'STALE');

CREATE TABLE job_transitions (
    id         bigserial PRIMARY KEY,
    job_id     uuid NOT NULL REFERENCES jobs (id),
    from_state text NOT NULL,
    outcome    text NOT NULL,
    to_state   text NOT NULL,
    reason     text,
    actor      text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON job_transitions (job_id, id);

CREATE TABLE prompt_runs (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id               uuid NOT NULL REFERENCES jobs (id),
    step                 text NOT NULL,
    prompt_id            text NOT NULL,
    prompt_version       text NOT NULL,
    provider             text NOT NULL,
    model                text NOT NULL,
    params               jsonb NOT NULL,
    idempotency_key      text NOT NULL,
    input_artifact_ids   uuid[] NOT NULL DEFAULT '{}',
    request_redacted     jsonb,
    response_raw_uri     text,
    validation_errors    jsonb,
    repair_attempt       int NOT NULL DEFAULT 0,
    tokens_in            int,
    tokens_cached_read   int,
    tokens_cache_write   int,
    tokens_out           int,
    cost_usd             numeric(12, 6),
    latency_ms           int,
    status               text NOT NULL CHECK (status IN ('running', 'ok', 'invalid', 'refused', 'truncated', 'error')),
    trace_id             text,
    created_at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON prompt_runs (job_id, step);
CREATE UNIQUE INDEX prompt_runs_idem_ok ON prompt_runs (idempotency_key) WHERE status = 'ok';

CREATE TABLE artifacts (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id             uuid NOT NULL REFERENCES jobs (id),
    kind               text NOT NULL,
    schema_version     text NOT NULL,
    version            int NOT NULL,
    sha256             text NOT NULL,
    blob_uri           text NOT NULL,
    produced_by_run_id uuid REFERENCES prompt_runs (id),
    produced_by        text NOT NULL,          -- step hoặc 'human:<user_id>'
    created_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (job_id, kind, version)
);

CREATE TABLE gate_decisions (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id        uuid NOT NULL REFERENCES jobs (id),
    gate          text NOT NULL,
    actor         text NOT NULL CHECK (actor IN ('ai', 'human', 'code')),
    user_id       text,
    prompt_run_id uuid REFERENCES prompt_runs (id),
    mode          text NOT NULL CHECK (mode IN ('auto', 'shadow', 'human')),
    action        text NOT NULL CHECK (action IN ('approve', 'revise', 'escalate', 'reject', 'edit')),
    confidence    numeric,
    reason        text,
    rule_failures jsonb,
    diff          jsonb,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON gate_decisions (gate, mode, created_at);

CREATE TABLE author_knowledge (
    id           text PRIMARY KEY CHECK (id ~ '^K[0-9]{4,}$'),
    author_id    text NOT NULL,
    kind         text NOT NULL CHECK (kind IN ('trai_nghiem', 'so_lieu_noi_bo', 'case_study', 'quan_diem', 'quy_trinh')),
    title        text NOT NULL,
    content      text NOT NULL,
    facts        jsonb NOT NULL DEFAULT '[]',
    topic_tags   text[] NOT NULL,
    evidence_ref text,
    valid_until  date NOT NULL,
    status       text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'expired', 'retired')),
    pii_checked  boolean NOT NULL DEFAULT false,
    usage_count  int NOT NULL DEFAULT 0,
    search_text  text NOT NULL,
    search_tsv   tsvector GENERATED ALWAYS AS (to_tsvector('simple', search_text)) STORED,
    embedding    vector,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE SEQUENCE author_knowledge_seq START 1;
CREATE INDEX ON author_knowledge (author_id, status, valid_until);
CREATE INDEX ON author_knowledge USING gin (search_tsv);
CREATE INDEX ON author_knowledge USING gin (topic_tags);

CREATE TABLE knowledge_usage (
    job_id     uuid NOT NULL REFERENCES jobs (id),
    entry_id   text NOT NULL REFERENCES author_knowledge (id),
    ig_id      text NOT NULL,
    excerpt    text NOT NULL,
    status     text NOT NULL DEFAULT 'reserved' CHECK (status IN ('reserved', 'committed', 'released')),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (job_id, entry_id)
);

CREATE TABLE qa_results (
    job_id        uuid NOT NULL REFERENCES jobs (id),
    pass_id       text NOT NULL,
    artifact_id   uuid NOT NULL REFERENCES artifacts (id),
    blocking_fail boolean NOT NULL,
    round         int NOT NULL,
    PRIMARY KEY (job_id, pass_id, round)
);

-- Nhánh B: text nguồn sống đến khi FACT_GATE approve, sau đó xoá dòng này; chỉ giữ fingerprint
CREATE TABLE source_texts (
    job_id     uuid NOT NULL REFERENCES jobs (id),
    sha256     text NOT NULL,
    text       text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (job_id, sha256)
);

CREATE TABLE source_fingerprints (
    job_id                     uuid NOT NULL REFERENCES jobs (id),
    source_sha256              text NOT NULL,
    shingle_n                  int NOT NULL,
    shingle_hashes             bytea NOT NULL,
    overlap_report_artifact_id uuid REFERENCES artifacts (id),
    PRIMARY KEY (job_id, source_sha256)
);

CREATE TABLE ab_trials (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id         uuid NOT NULL REFERENCES jobs (id),
    model_a        text NOT NULL,
    model_b        text NOT NULL,
    position_order text NOT NULL,
    judge          text,
    winner         text,
    rater_user_id  text,
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE gsc_snapshots (
    job_id      uuid NOT NULL REFERENCES jobs (id),
    url         text NOT NULL,
    "window"    text NOT NULL CHECK ("window" IN ('before', 'after')),
    clicks      int,
    impressions int,
    ctr         numeric,
    position    numeric,
    fetched_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (job_id, url, "window")
);

-- Đếm bài đã publish để áp max_posts_per_week
CREATE TABLE publications (
    job_id       uuid PRIMARY KEY REFERENCES jobs (id),
    url          text,
    cms_status   text NOT NULL,
    published_at timestamptz NOT NULL DEFAULT now()
);
