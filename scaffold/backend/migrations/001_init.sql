-- =============================================================================
-- 001_init — normalised core (3NF) + a deliberately denormalised read model.
--
-- Design note for the review session:
--   The write path is normalised so correctness is enforced by the schema
--   (FKs, unique constraints, no update anomalies). The dashboard's read path
--   asks aggregate questions that would otherwise scan the whole jobs table on
--   every page load, so it is served from a materialised view instead. That
--   view carries a UNIQUE index specifically so it can be refreshed
--   CONCURRENTLY without blocking readers. The cost is bounded staleness,
--   which the API surfaces as `as_of` rather than hiding.
-- =============================================================================

-- ---------------------------------------------------------------- job_types --
CREATE TABLE IF NOT EXISTS job_types (
    id           SERIAL PRIMARY KEY,
    name         TEXT        NOT NULL UNIQUE,
    description  TEXT        NOT NULL DEFAULT '',
    max_attempts INT         NOT NULL DEFAULT 4 CHECK (max_attempts BETWEEN 1 AND 20),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- --------------------------------------------------------------------- jobs --
CREATE TABLE IF NOT EXISTS jobs (
    id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    job_type_id      INT         NOT NULL REFERENCES job_types (id) ON DELETE RESTRICT,
    status           TEXT        NOT NULL CHECK (status IN
                       ('queued','running','succeeded','failed','dead_letter','cancelled')),
    priority         SMALLINT    NOT NULL DEFAULT 0,
    payload          JSONB       NOT NULL DEFAULT '{}'::jsonb,

    -- Client-supplied dedupe key. UNIQUE is what makes submission retry-safe:
    -- a duplicate POST collides here instead of creating duplicate work.
    idempotency_key  TEXT        UNIQUE,

    attempts         INT         NOT NULL DEFAULT 0,
    max_attempts     INT         NOT NULL,
    run_after        TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Lease fields. A claim is only valid until lease_expires_at; the reaper
    -- reclaims anything a dead worker left behind.
    locked_by        TEXT,
    lease_expires_at TIMESTAMPTZ,

    last_error       TEXT,
    result           JSONB,

    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at       TIMESTAMPTZ,
    finished_at      TIMESTAMPTZ
);

-- The claim query's supporting index. Partial, because only queued rows are
-- ever candidates — this keeps the index small no matter how much history
-- accumulates in the table.
CREATE INDEX IF NOT EXISTS ix_jobs_claim
    ON jobs (priority DESC, created_at)
    WHERE status = 'queued';

-- The reaper's index: only running rows can have an expired lease.
CREATE INDEX IF NOT EXISTS ix_jobs_lease
    ON jobs (lease_expires_at)
    WHERE status = 'running';

CREATE INDEX IF NOT EXISTS ix_jobs_status_created ON jobs (status, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_jobs_created        ON jobs (created_at DESC);

-- ------------------------------------------------------------- job_attempts --
-- One row per execution attempt. Keeping attempts separate from jobs is what
-- makes "why did this fail three times?" answerable after the fact.
CREATE TABLE IF NOT EXISTS job_attempts (
    id           BIGSERIAL   PRIMARY KEY,
    job_id       UUID        NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    attempt_no   INT         NOT NULL,
    worker_id    TEXT        NOT NULL,
    status       TEXT        NOT NULL CHECK (status IN ('running','succeeded','failed')),
    error        TEXT,
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ,
    duration_ms  INT,
    UNIQUE (job_id, attempt_no)
);

CREATE INDEX IF NOT EXISTS ix_job_attempts_job ON job_attempts (job_id, attempt_no);

-- --------------------------------------------------------------- job_events --
-- Append-only log. `seq` is monotonic and is what SSE clients resume from via
-- Last-Event-ID, so a dropped connection never silently loses an update.
CREATE TABLE IF NOT EXISTS job_events (
    seq        BIGSERIAL   PRIMARY KEY,
    job_id     UUID        NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    event_type TEXT        NOT NULL,
    data       JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_job_events_job ON job_events (job_id, seq);

-- ------------------------------------------------------------------ workers --
CREATE TABLE IF NOT EXISTS workers (
    id                TEXT        PRIMARY KEY,
    hostname          TEXT        NOT NULL,
    started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ------------------------------------------------------- NOTIFY on new event --
-- Postgres itself publishes the event, so any API replica holding a LISTEN can
-- fan out to its SSE clients. The payload is intentionally tiny (pg_notify caps
-- at 8000 bytes) — listeners read the full row back by seq.
CREATE OR REPLACE FUNCTION notify_job_event() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify(
        'job_events',
        json_build_object('seq', NEW.seq, 'job_id', NEW.job_id, 'event_type', NEW.event_type)::text
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_notify_job_event ON job_events;
CREATE TRIGGER trg_notify_job_event
    AFTER INSERT ON job_events
    FOR EACH ROW EXECUTE FUNCTION notify_job_event();

-- ============================ DENORMALISED READ MODEL =========================
-- Dashboard aggregates. Refreshed on a cadence, never on the write path.
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_job_stats AS
SELECT
    jt.id                                   AS job_type_id,
    jt.name                                 AS job_type,
    j.status                                AS status,
    date_trunc('hour', j.created_at)        AS bucket,
    count(DISTINCT j.id)::bigint            AS job_count,
    coalesce(round(avg(a.duration_ms)), 0)::bigint AS avg_duration_ms,
    coalesce(
        percentile_disc(0.95) WITHIN GROUP (ORDER BY a.duration_ms), 0
    )::bigint                               AS p95_duration_ms,
    coalesce(sum(j.attempts), 0)::bigint    AS total_attempts
FROM jobs j
JOIN job_types jt ON jt.id = j.job_type_id
LEFT JOIN job_attempts a
       ON a.job_id = j.id AND a.finished_at IS NOT NULL
GROUP BY jt.id, jt.name, j.status, date_trunc('hour', j.created_at);

-- REFRESH ... CONCURRENTLY is only legal when a unique index exists on the view.
-- Without this, every refresh takes an exclusive lock and the dashboard stalls.
CREATE UNIQUE INDEX IF NOT EXISTS ux_mv_job_stats
    ON mv_job_stats (job_type_id, status, bucket);

-- ------------------------------------------------------------------- seeding --
INSERT INTO job_types (name, description, max_attempts) VALUES
    ('image.transform', 'Transform an image via the external media API', 4),
    ('report.generate', 'Generate an aggregate report',                  3),
    ('webhook.deliver', 'Deliver a webhook to a customer endpoint',      5)
ON CONFLICT (name) DO NOTHING;
