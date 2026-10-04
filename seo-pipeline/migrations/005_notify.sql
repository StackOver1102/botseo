-- Realtime: Postgres báo ngay khi có việc, thay cho quét định kỳ.
--   ext_task  (payload = provider): có task external mới / được mở lại → long-poll của extension trả task ngay
--   job_event (payload = job_id)   : job đổi trạng thái / chờ task / task đổi trạng thái → UI (SSE) và worker thức dậy
CREATE OR REPLACE FUNCTION notify_ext_task() RETURNS trigger AS $$
BEGIN
    IF NEW.status = 'pending' THEN
        PERFORM pg_notify('ext_task', NEW.provider);
    END IF;
    IF NEW.job_id IS NOT NULL AND (TG_OP = 'INSERT' OR NEW.status IS DISTINCT FROM OLD.status) THEN
        PERFORM pg_notify('job_event', NEW.job_id::text);
    END IF;
    RETURN NULL;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER external_tasks_notify AFTER INSERT OR UPDATE OF status ON external_tasks
    FOR EACH ROW EXECUTE FUNCTION notify_ext_task();

CREATE OR REPLACE FUNCTION notify_job_event() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('job_event', NEW.id::text);
    RETURN NULL;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER jobs_notify AFTER UPDATE OF state, last_error, current_call, run_after ON jobs
    FOR EACH ROW EXECUTE FUNCTION notify_job_event();
CREATE TRIGGER jobs_notify_insert AFTER INSERT ON jobs FOR EACH ROW EXECUTE FUNCTION notify_job_event();
