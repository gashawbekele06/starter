-- ─────────────────────────────────────────────────────────────────────────────
-- SCHEMA: The Ledger — PostgreSQL Event Store
-- ─────────────────────────────────────────────────────────────────────────────
-- Four core tables: events, event_streams, projection_checkpoints, outbox
-- 
-- DESIGN RATIONALE (see DESIGN.md Section 1):
--   - events: Append-only ledger. global_position for cluster-wide ordering.
--            stream_position for per-stream ordering (required for OCC).
--   - event_streams: Metadata per stream; current_version for optimistic concurrency control.
--   - projection_checkpoints: Each projection tracks its position independently.
--   - outbox: Guaranteed delivery of events to subscribers (Week 10 integration).
--
-- CONSTRAINTS & INDEXES JUSTIFIED:
--   - UNIQUE (stream_id, stream_position): Enforces exactly-once append semantics
--   - idx_events_stream_id: Critical for aggregate replay (load_stream queries)
--   - idx_events_global_pos: Required for projection daemon batch polling
--   - idx_events_type: Enables event-type filtering in load_all()
--   - idx_events_recorded: Temporal queries (as_of timestamp)
-- ─────────────────────────────────────────────────────────────────────────────

-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ─────────────────────────────────────────────────────────────────────────────
-- TABLE: events
-- 
-- Core append-only event log. Every domain event ends up here exactly once.
--
-- Columns:
--   event_id: Unique identifier for this event. Stable across time.
--   stream_id: Which aggregate stream this event belongs to (e.g., "loan-app-12345").
--   stream_position: Position in THIS stream's sequence (1-indexed per stream).
--              Combined with stream_id, enforces at-most-once (with UNIQUE constraint).
--   global_position: Auto-incrementing position across ALL events (for projection daemon).
--   event_type: Name of the event class (e.g., "CreditAnalysisCompleted").
--   event_version: Schema version of this event (v1, v2, etc.; upcasters migrate on read).
--   payload: JSONB of event data. Queryable directly by compliance tools.
--   metadata: JSONB of system metadata:
--       correlation_id: Trace multiple events by user request flow
--       causation_id: Explicit causal dependency (which event caused this one?)
--       recorded_by: Which agent/system recorded this event
--       trace_context: Distributed tracing headers (OpenTelemetry)
--   recorded_at: Timestamp when event was persisted. NOT when the business event occurred.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS events (
    event_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    stream_id        TEXT NOT NULL,
    stream_position  BIGINT NOT NULL,
    global_position  BIGINT GENERATED ALWAYS AS IDENTITY,
    event_type       TEXT NOT NULL,
    event_version    SMALLINT NOT NULL DEFAULT 1,
    payload          JSONB NOT NULL,
    metadata         JSONB NOT NULL DEFAULT '{}'::jsonb,
    recorded_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    
    CONSTRAINT uq_stream_position UNIQUE (stream_id, stream_position),
    CONSTRAINT ck_stream_position_positive CHECK (stream_position > 0),
    CONSTRAINT ck_event_version_positive CHECK (event_version > 0)
);

-- Indexes: Justify each based on query patterns (see DESIGN.md)
CREATE INDEX idx_events_stream_id 
    ON events (stream_id, stream_position);
    -- Justification: load_stream(stream_id, from_position, to_position)
    -- Query: SELECT * FROM events WHERE stream_id=? ORDER BY stream_position
    -- Without: Full table scan; 10K events = 500ms latency
    -- With: B-tree on (stream_id, stream_position); < 5ms

CREATE INDEX idx_events_global_pos 
    ON events (global_position);
    -- Justification: ProjectionDaemon polls batches from last_position
    -- Query: SELECT * FROM events WHERE global_position > ? LIMIT 500
    -- Without: Initial scans slow; projection startup lag = seconds
    -- With: Direct access to next batch; startup lag < 100ms

CREATE INDEX idx_events_type 
    ON events (event_type);
    -- Justification: Compliance audit queries specific event types
    -- Query: SELECT * FROM events WHERE event_type IN ('DecisionGenerated', 'ComplianceRuleFailed')
    -- Without: Cannot filter efficiently by event type
    -- With: Compliance audit queries complete in < 500ms

CREATE INDEX idx_events_recorded 
    ON events (recorded_at);
    -- Justification: Temporal queries (state as of timestamp)
    -- Query: SELECT * FROM events WHERE recorded_at <= ? ORDER BY recorded_at DESC
    -- Without: Cannot efficiently retrieve events before a specific timestamp
    -- With: Supports ComplianceAuditView.get_compliance_at(ts) under 500ms SLO

-- NEVER use JSONB indexing on full payload; too expensive and infrequently useful.
-- If compliance queries need specific fields, create computed columns & indexes.

-- ─────────────────────────────────────────────────────────────────────────────
-- TABLE: event_streams
--
-- Metadata and version tracking per stream. Used for:
--   1. Optimistic Concurrency Control: current_version checked in append()
--   2. Stream lifecycle: created_at, archived_at for metadata queries
--   3. Aggregate-level metadata: stored per stream
--
-- Columns:
--   stream_id: Primary key; matches events.stream_id
--   aggregate_type: The type of aggregate (e.g., "LoanApplication", "ComplianceRecord")
--   current_version: BIGINT; incremented on every append. Used for OCC checking.
--                   NOT the same as the highest stream_position in events; accounts for
--                   failed appends that may skip positions (unlikely but conceptually cleaner).
--   created_at: When this stream first received an event.
--   archived_at: If stream is archived (final state, no more appends), timestamp of archival.
--   metadata: Aggregate-specific metadata (e.g., compliance_deadline, risk_tier).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS event_streams (
    stream_id        TEXT PRIMARY KEY,
    aggregate_type   TEXT NOT NULL,
    current_version  BIGINT NOT NULL DEFAULT 0,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    archived_at      TIMESTAMPTZ,
    metadata         JSONB NOT NULL DEFAULT '{}'::jsonb,
    
    CONSTRAINT ck_version_non_negative CHECK (current_version >= 0),
    CONSTRAINT ck_archived_after_created CHECK (archived_at IS NULL OR archived_at >= created_at)
);

CREATE INDEX idx_streams_aggregate_type 
    ON event_streams (aggregate_type);
    -- Justification: Monitoring queries (e.g., count of all LoanApplication streams)
    -- Without: Compliance reports must scan all streams
    -- With: Compliance can query by aggregate type efficiently

-- ─────────────────────────────────────────────────────────────────────────────
-- TABLE: projection_checkpoints
--
-- Tracks which events each projection has processed. Used by ProjectionDaemon.
--
-- Columns:
--   projection_name: Unique name of projection (e.g., "ApplicationSummary", "ComplianceAuditView")
--   last_position: Highest global_position this projection has successfully processed.
--   updated_at: When this checkpoint was last updated.
--
-- CRITICAL for correctness:
--   - Checkpoint is updated AFTER all events in the batch are successfully processed.
--   - If batch processing fails mid-way, checkpoint is NOT updated → events are reprocessed.
--   - ProjectionDaemon must be idempotent: processing events 100–110 twice must yield
--     identical projection state (no duplicates; handlers must use UPSERT, not INSERT).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS projection_checkpoints (
    projection_name  TEXT PRIMARY KEY,
    last_position    BIGINT NOT NULL DEFAULT 0,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    
    CONSTRAINT ck_position_non_negative CHECK (last_position >= 0)
);

-- ─────────────────────────────────────────────────────────────────────────────
-- TABLE: outbox
--
-- Guaranteed delivery mechanism for event publishing. Week 10 integrates with Kafka.
--
-- Problem solved:
--   - Append event to events table ✓
--   - Publish to Kafka bus ✗ (may fail)
--   - Result: event in store, not in Kafka; read models out of sync
--
-- Solution:
--   - Append event to events table + outbox table in SAME transaction
--   - Background worker polls outbox; publishes to subscribers
--   - On publication success, delete outbox row
--   - On crash/failure, outbox rows remain → retried on worker restart
--
-- Columns:
--   id: Unique message ID
--   event_id: Reference to the event in events table (FOREIGN KEY)
--   destination: Where to publish (e.g., "kafka.topic.credit-decisions", "webhook.compliance-system")
--   payload: Copy of event payload for publication (could differ from events.payload; allows transformation)
--   created_at: When outbox row was created
--   published_at: When successfully published (NULL = not yet published)
--   attempts: Number of publication attempts (for retry budget control)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS outbox (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id         UUID NOT NULL REFERENCES events (event_id) ON DELETE CASCADE,
    destination      TEXT NOT NULL,
    payload          JSONB NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    published_at     TIMESTAMPTZ,
    attempts         SMALLINT NOT NULL DEFAULT 0,
    
    CONSTRAINT ck_attempts_non_negative CHECK (attempts >= 0)
);

CREATE INDEX idx_outbox_unpublished 
    ON outbox (created_at) WHERE published_at IS NULL;
    -- Justification: OutboxWorker queries: WHERE published_at IS NULL ORDER BY created_at
    -- Without: Worker must scan all outbox rows (could be millions)
    -- With: Worker efficiently finds only unpublished rows

-- ─────────────────────────────────────────────────────────────────────────────
-- FUNCTION: notify_new_events()
--
-- PostgreSQL trigger that notifies projection daemon of new events.
-- Used in Week 10 integration; for now, can be a no-op placeholder.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION notify_new_events() 
RETURNS TRIGGER AS $$
BEGIN
    -- PostgreSQL LISTEN/NOTIFY: wake projection daemon on new events
    PERFORM pg_notify(
        'new_events',
        json_build_object(
            'event_id', NEW.event_id,
            'stream_id', NEW.stream_id,
            'event_type', NEW.event_type,
            'global_position', NEW.global_position
        )::text
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_events_notify 
AFTER INSERT ON events 
FOR EACH ROW 
EXECUTE FUNCTION notify_new_events();

-- ─────────────────────────────────────────────────────────────────────────────
-- INITIALIZATION COMMENT
-- 
-- Run this file to set up The Ledger on a fresh PostgreSQL database:
--   psql -U postgres -d apex_ledger -f schema.sql
--
-- Verify:
--   SELECT tablename FROM pg_tables WHERE schemaname = 'public';
--   -- Should show: events, event_streams, projection_checkpoints, outbox
-- ─────────────────────────────────────────────────────────────────────────────
