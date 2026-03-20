# The Ledger — Production-Grade Event Sourcing for Multi-Agent AI

## Project Status — INTERIM SUBMISSION (March 22, 03:00 UTC)

**Phase 0-1 Implementation Complete** ✓

| Phase   | Status         | Deliverables                                 |
| ------- | -------------- | -------------------------------------------- |
| **0**   | ✅ COMPLETE    | DOMAIN_NOTES.md (6 sections + reasoning)     |
| **1A**  | ✅ COMPLETE    | PostgreSQL schema.sql (4 tables + indexes)   |
| **1B**  | ✅ COMPLETE    | EventStore (OCC + concurrency tests passing) |
| **1C**  | ✅ COMPLETE    | DESIGN.md (6 architectural sections)         |
| **2**   | 🚧 IN PROGRESS | LoanApplication & AgentSession aggregates    |
| **3-5** | ⏳ ROADMAP     | Projections, Upcasting, MCP Server           |
| **6**   | ⏳ BONUS       | What-If & Regulatory Package                 |

## Quick Start (No Database Required for Phase 1)

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Run Phase 1 tests (in-memory store, no Postgres needed)
pytest tests/test_concurrency.py -v

# Expected output:
#   test_double_decision_concurrent_append PASSED ✓
#   test_retry_after_occ_failure PASSED ✓
#   test_concurrent_load_while_append PASSED ✓
```

## What Is Implemented (Interim Submission)

### ✅ Phase 0: Conceptual Foundation (DOMAIN_NOTES.md)

Complete analysis of:

1. EDA vs. ES architectural distinction
2. Aggregate boundary justification + coupling problems
3. Concurrency under load (2 agents, same stream)
4. Projection lag consequences & SLO management
5. Upcasting inference strategy (model_version, regulatory_basis)
6. Distributed projection daemon (PostgreSQL advisory locks)

**File**: [DOMAIN_NOTES.md](DOMAIN_NOTES.md)

### ✅ Phase 1A: PostgreSQL Schema (schema.sql)

Core append-only ledger with:

- `events` table: Stream-ordered, globally-ordered, indexed for replay
- `event_streams` table: Per-stream metadata + OCC version tracking
- `projection_checkpoints` table: Async projection state
- `outbox` table: Guaranteed event delivery (Week 10)
- Indexes optimized for: aggregate replay, batch polling, temporal queries
- Triggers: LISTEN/NOTIFY for real-time projection updates

**File**: [schema.sql](schema.sql)

### ✅ Phase 1B: EventStore Core (ledger/event_store.py)

Complete implementation with:

```python
class EventStore:
    async def append(stream_id, events, expected_version) -> int
    # Optimistic concurrency control; atomic outbox write
    # Returns new stream version

    async def load_stream(stream_id) -> list[StoredEvent]
    # Aggregate replay; efficient (indexed)

    async def load_all(from_global_position) -> AsyncGenerator[StoredEvent]
    # Projection daemon polling; streaming (memory-efficient)
```

**Also**: `InMemoryEventStore` for testing (no database required)

**File**: [ledger/event_store.py](ledger/event_store.py)

### ✅ Phase 1B: Concurrency Tests (tests/test_concurrency.py)

**Double-Decision Test** (The Critical Test):

```python
@test_double_decision_concurrent_append
# Scenario: Two agents simultaneously append to same stream at version -1
#
# Expected:
#   Agent A: append succeeds       → stream version 0
#   Agent B: OptimisticConcurrencyError(expected=-1, actual=0)
#   Count: exactly 1 event in stream (NOT 2)
#
# Result: ✅ PASSING
```

**Retry Test**: Losing agent reloads and retries successfully

**Concurrent Load/Append Test**: Multiple readers don't block writers

**Result**: ✅ 3/3 PASSING

**File**: [tests/test_concurrency.py](tests/test_concurrency.py)

### ✅ Architecture / Design Documentation

**DOMAIN_NOTES.md** (Conceptual)

- Why separate aggregates (ComplianceRecord vs. LoanApplication)
- How OCC works under concurrent load
- Upcasting strategy & inference decisions
- Projection lag SLO management

**DESIGN.md** (Implementational)

- Aggregate boundary tradeoffs
- Projection architecture + snapshots
- Concurrency analysis + retry budget
- Upcasting error rates
- EventStoreDB comparison
- Reflection: what would be done differently

**Files**: [DOMAIN_NOTES.md](DOMAIN_NOTES.md), [DESIGN.md](DESIGN.md)

## What Needs to be Done (Roadmap to Final)

### Phase 2: Domain Logic (ETA: March 23)

- [ ] LoanApplicationAggregate with state machine
- [ ] AgentSessionAggregate with context enforcement (Gas Town pattern)
- [ ] ComplianceRecordAggregate
- [ ] Command handlers (submit_application, record_credit_analysis, etc.)
- [ ] Business rule enforcement (state transitions, confidence floor, etc.)

### Phase 3: Projections & Daemon (ETA: March 24)

- [ ] ProjectionDaemon (async processing loop)
- [ ] ApplicationSummary projection
- [ ] AgentPerformanceLedger projection
- [ ] ComplianceAuditView projection + temporal queries
- [ ] SLO tests under 50-concurrent-load

### Phase 4: Schema Evolution & Integrity (ETA: March 24)

- [ ] UpcasterRegistry for v1→v2 migrations
- [ ] Immutability test (upcasting doesn't touch stored events)
- [ ] Cryptographic audit chain (SHA256 hash chain)
- [ ] Gas Town agent recovery (reconstruct_agent_context)

### Phase 5: MCP Server (ETA: March 25)

- [ ] 8 MCP tools (command side) + structured error types
- [ ] 6 MCP resources (query side, projection-backed)
- [ ] Full lifecycle integration test via MCP only
- [ ] Tool descriptions with LLM consumption preconditions

### Phase 6: Bonus (ETA: March 26)

- [ ] What-If counterfactual analysis
- [ ] Regulatory package generation
- [ ] Narrative generation

## Testing

```bash
# Phase 1 tests (no database required)
pytest tests/test_concurrency.py -v

# Expected: 3/3 PASSING

# All tests (when complete)
pytest tests/ -v

# Specific test
pytest tests/test_concurrency.py::test_double_decision_concurrent_append -v
```

## Git Status

```bash
# View uncommitted changes
git status

# Build history from latest
git log --oneline -10

# Current branch
git branch
```

## Architecture Diagrams

### Event Store Topology

```
┌─────────────────────────────────────────────────────────────┐
│                   PostgreSQL Database                       │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  events                 ← Stream-ordered, globally-ordered │
│    stream_id, stream_position (primary key pair)          │
│    global_position (auto-increment)                        │
│    event_type, payload (JSONB), metadata, recorded_at     │
│                                                             │
│  event_streams          ← Per-stream metadata              │
│    stream_id (PK)                                          │
│    current_version      ← Used for OCC checking           │
│    aggregate_type, created_at, archived_at                 │
│                                                             │
│  projection_checkpoints ← Daemon progress tracking         │
│    projection_name (PK)                                    │
│    last_position        ← Where daemon left off           │
│                                                             │
│  outbox                 ← Guaranteed delivery (Week 10)    │
│    event_id (FK to events)                                 │
│    destination, payload, published_at                      │
│                                                             │
└─────────────────────────────────────────────────────────────┘

      ↓ Python EventStore class

    append(stream_id, events, expected_version)
      → checks current_version vs expected_version
      → if mismatch → OptimisticConcurrencyError
      → else → INSERT events + outbox (atomic transaction)
      → UPDATE stream version

    load_stream(stream_id)
      → SELECT * WHERE stream_id ORDER BY stream_position
      → apply upcasters on read (immutable DB)
      → return [StoredEvent, ...]
```

### Optimistic Concurrency Control (OCC)

```
Timeline: Two agents append to same stream

T0: Initial state
    stream_id = "loan-12345"
    event_streams.current_version = -1 (new stream)

T1-A: Agent A reads version
      version := SELECT current_version FROM event_streams WHERE stream_id = "loan-12345"
      version = -1
      ✓ saves expected_version = -1

T1-B: Agent B reads version
      version = -1  (same; T1-A hasn't committed yet)
      ✓ saves expected_version = -1

T2-A: Agent A appends
      BEGIN TRANSACTION
        SELECT current_version FROM event_streams WHERE stream_id = "loan-12345" FOR UPDATE
        → acquires lock, sees version = -1
        → matches expected_version = -1 ✓
        INSERT INTO events (stream_id=1, payload={...A...})
        INSERT INTO outbox (...)
        UPDATE event_streams SET current_version = 0
      COMMIT
      ✅ SUCCESS: new version = 0

T2-B: Agent B appends (after T2-A releases lock)
      BEGIN TRANSACTION
        SELECT current_version FROM event_streams WHERE stream_id = "loan-12345" FOR UPDATE
        → blocks until T2-A commits, then acquires lock
        → sees version = 0 (updated by T2-A)
        → expected_version = -1 ✗ MISMATCH
        RAISE OptimisticConcurrencyError(expected=-1, actual=0)
      ROLLBACK
      ❌ FAILURE: must retry

Result:
  - Stream has exactly 1 event (Agent A's)
  - Agent B receives error + can retry with updated version
  - No lost writes; no silent overwrites
  - Split-brain state prevented
```

## PostgreSQL Setup (for Phase 2+)

```bash
# When ready for Phase 2, start PostgreSQL
docker run -d \
  -e POSTGRES_PASSWORD=apex \
  -e POSTGRES_DB=apex_ledger \
  -p 5432:5432 \
  postgres:16

# Initialize schema
psql -U postgres -d apex_ledger -f schema.sql

# Verify
psql -U postgres -d apex_ledger -c "\dt"  # List tables
```

## Event Schema

Core events implemented in `ledger/models/events.py`:

- **LoanApplication stream**: ApplicationSubmitted, CreditAnalysisRequested, DecisionGenerated, HumanReviewCompleted, ApplicationApproved, ApplicationDeclined
- **AgentSession stream**: AgentContextLoaded, CreditAnalysisCompleted (v1/v2), FraudScreeningCompleted
- **ComplianceRecord stream**: ComplianceCheckRequested, ComplianceRulePassed, ComplianceRuleFailed
- **AuditLedger stream**: AuditIntegrityCheckRun

All events are Pydantic models with strict schema validation.

## References

- **DOMAIN_NOTES.md**: Architectural reasoning (EDA vs. ES, aggregates, concurrency, upcasting)
- **DESIGN.md**: Implementational decisions (projections, retry strategies, performance SLOs)
- **schema.sql**: Complete PostgreSQL schema with justifications
- **ledger/event_store.py**: EventStore implementation (OCC + async streaming)
- **ledger/models/events.py**: Complete event schema (Pydantic models)
- **tests/test_concurrency.py**: Phase 1B test suite (double-decision, retry, concurrent load)
  pytest tests/test_projections.py -v # Phase 4
  pytest tests/test_mcp.py -v # Phase 5

# tested

```

```
