# DOMAIN_NOTES.md — Architectural Reasoning for The Ledger

## 1. EDA vs. ES: The Architectural Divide

### Current State: Event-Driven Architecture (Callback-Based)

The Week 1 Governance Hooks use LangChain-style traced callbacks: agents emit hook events, listeners react asynchronously, events can be dropped or lost. This is **Event-Driven Architecture**—events are messages between services, not state.

**Example**:

```python
# Week 1: EDA callback
@hook("agent_decision_made")
def on_decision(event):
    log_to_analytics(event)  # Fire and forget
    # If this fails silently, no one knows the decision wasn't recorded
```

### The Ledger: Event Sourcing (Immutable Record)

Event Sourcing inverts the model. Events ARE the database. The state you see is derived by replaying events. Losses are impossible—the append-only log is the source of truth.

**Example**:

```python
# The Ledger: ES
async def handle_decision(cmd: CreditAnalysisCompletedCommand):
    # 1. Load aggregate state by replaying its events
    app = await LoanApplicationAggregate.load(store, cmd.application_id)
    # 2. Validate business rules
    app.assert_credit_analysis_not_already_done()
    # 3. Append new event atomically with optimistic concurrency
    await store.append(
        f"loan-{cmd.application_id}",
        [CreditAnalysisCompleted(...)],
        expected_version=app.version
    )
```

### What Changes with The Ledger

| Aspect             | EDA (Week 1)                                 | Event Sourcing (The Ledger)               |
| ------------------ | -------------------------------------------- | ----------------------------------------- |
| **Event loss**     | Possible—listener crashes drop events        | Impossible—append is ACID                 |
| **Replay**         | Not possible—events are transient            | Always possible—full history available    |
| **State**          | Stored separately from events                | Derived from events (no separate DB)      |
| **Audit trail**    | Incomplete—missing events leave gaps         | Complete and cryptographically verifiable |
| **Temporal query** | Cannot ask "what was state at 3pm Tuesday?"  | Native—replay to any point                |
| **Concurrency**    | Multiple listeners can race; last-write-wins | Optimistic concurrency; first-writer-wins |

### Gained by Switching to Event Sourcing

1. **Auditability**: Every decision traceable to its inputs; no "black hole" events
2. **Reproducibility**: Replay any agent's session with identical context; perfect for debugging
3. **Regulatory compliance**: Immutable audit trail with cryptographic integrity
4. **Temporal correctness**: Compliance officers can ask "what was our compliance status on March 1?" and get exact answer
5. **System resilience**: Gas Town pattern—agents recover from crashes by reading their own event history

---

## 2. The Aggregate Question: Boundary Justification

### Chosen Boundaries

We maintain **four separate aggregates**, each with its own stream:

1. **LoanApplication** (`loan-{application_id}`): Full lifecycle from submission to decision
2. **AgentSession** (`agent-{agent_id}-{session_id}`): One agent's work on one or more applications
3. **ComplianceRecord** (`compliance-{application_id}`): Regulatory checks (kept separate for scaling)
4. **AuditLedger** (`audit-{entity_type}-{entity_id}`): Cross-cutting integrity verification

### Alternative Boundary Considered and Rejected: Monolithic LoanApplication

**Rejected Design**: Single aggregate containing all application events + all compliance checks + all agent decisions.

**Why Rejected**:

```python
# ANTI-PATTERN: All in one stream
loan-12345:
  1. ApplicationSubmitted
  2. DocumentsUploaded
  3. CreditAnalysisRequested
  4. CreditAnalysisCompleted (agent-credit-001)
  5. FraudScreeningRequested
  6. FraudScreeningCompleted (agent-fraud-001)
  7. ComplianceCheckRequested
  8. ComplianceRulePassed (rule-kyc)
  9. ComplianceRuleFailed (rule-sanctions)  # <-- CONFLICT HERE
  10. ComplianceRuleFailed (rule-aml)
  11-20. More compliance checks...
```

**Coupling Problem Under Concurrency**:

- Two compliance officers simultaneously evaluate **different rules** for the same application
- Both try to append: Officer A appends `ComplianceRulePassed(kyc)`, Officer B appends `ComplianceRuleFailed(sanctions)`
- With monolithic aggregate: both events race on the same stream → optimistic concurrency collision
- Officer A reads at v8, Officer B reads at v8, both try to append at expected_version=8
- One must retry, reloading the stream, discovering the other's decision mid-flight
- **Failure mode**: Compliance judgment splits—no single source of truth for "is this application compliant?"

**With Separate ComplianceRecord** (`compliance-12345` stream):

```python
# Each compliance check proceeds independently
compliance-12345:
  1. CheckRequested (rule-kyc)
  2. CheckRequested (rule-sanctions)
  3. CheckRequested (rule-aml)
  4. RulePassed (rule-kyc)    # Officer A appends here; no collision
  5. RuleFailed (rule-sanctions)  # Officer B appends here; orthogonal
  6. RuleFailed (rule-aml)
```

**Benefit**: Compliance checks scale horizontally—each rule evaluation is independent. No two officers collide on the same stream unless they evaluate the **same rule** simultaneously (which is rare and should fail with OCC).

### Aggregate Consistency Boundaries

Each aggregate maintains one **consistency boundary**:

- **LoanApplication**: Cannot transition state without all mandatory events present
- **AgentSession**: Cannot make a decision without `AgentContextLoaded` first (Gas Town)
- **ComplianceRecord**: Cannot clear compliance without all mandatory rules passing
- **AuditLedger**: Append-only; cryptographic chain integrity enforced

Aggregates communicate only through events. An aggregate NEVER directly mutates another's stream.

---

## 3. Concurrency in Practice: The Double-Decision Test

### Scenario: Two Credit Analysts Collide

```
T0: Application 12345 at stream version 3 (events: submitted, docs_uploaded, analysis_requested)

T1-A: Agent auth.credit-001 reads stream:
      version=3, gets events [0, 1, 2]
      Decision: risk_tier=MEDIUM, confidence=0.75

T1-B: Agent auth.credit-002 reads stream at SAME TIME:
      version=3, gets events [0, 1, 2]
      Decision: risk_tier=HIGH, confidence=0.82

T2-A: Agent calls: append_events(
        stream_id="loan-12345",
        events=[CreditAnalysisCompleted(risk_tier=MEDIUM, ...)],
        expected_version=3
      )

T2-B: Agent calls: append_events(
        stream_id="loan-12345",
        events=[CreditAnalysisCompleted(risk_tier=HIGH, ...)],
        expected_version=3
      ) at SAME TIME as T2-A
```

### Exact Sequence in PostgreSQL EventStore

```sql
-- T2-A's Append (wins)
BEGIN TRANSACTION;
  -- Check current version
  SELECT current_version FROM event_streams
  WHERE stream_id='loan-12345'
  FOR UPDATE;  -- Lock for exclusive access
  -- Sees current_version=3, matches expected_version=3 ✓ PASS

  -- Append event
  INSERT INTO events (stream_id, stream_position, event_type, payload, ...)
  VALUES ('loan-12345', 4, 'CreditAnalysisCompleted', {...MEDIUM...}, ...);

  -- Increment stream version
  UPDATE event_streams SET current_version=4
  WHERE stream_id='loan-12345';

  -- Write to outbox (async notification)
  INSERT INTO outbox (event_id, destination, ...)
  VALUES (...);

COMMIT;  -- T2-A succeeds, version now 4

-- T2-B's Append AFTER T2-A commits
BEGIN TRANSACTION;
  SELECT current_version FROM event_streams
  WHERE stream_id='loan-12345'
  FOR UPDATE;  -- Waits for T2-A's lock to release
  -- Now sees current_version=4, but expected_version=3 ✗ MISMATCH

  -- Raise OptimisticConcurrencyError
  ROLLBACK;
```

### What Losing Agent (T2-B) Receives

```python
OptimisticConcurrencyError(
    stream_id="loan-12345",
    expected_version=3,
    actual_version=4,
    message="OCC on 'loan-12345': expected v3, actual v4"
)
```

### What T2-B Must Do Next

```python
# Catch OCC error
except OptimisticConcurrencyError as e:
    # 1. Reload the stream with the winning agent's event
    app = await LoanApplicationAggregate.load(store, "12345")
    # app.version is now 4
    # app.credit_analysis_completed = True with risk_tier=MEDIUM

    # 2. Re-evaluate: is my analysis still relevant?
    # THREE options:
    #   a) ABANDON: Agent A won, my HIGH analysis is moot. Report loss.
    #   b) SUPERSEDE: My analysis is more recent model.
    #      Append HumanReviewOverride or RequestAnalysisRevision.
    #   c) CONDITIONAL: In context (e.g., fraud team), append independent FraudScreeningCompleted
    #      even though credit analysis is complete. No collision on fraud stream.

    if should_supersede():
        # Retry with NEW expected_version=4
        await store.append(
            "loan-12345",
            [CreditAnalysisCompleted(risk_tier=HIGH, ..., replaced_previous=True)],
            expected_version=4  # Now targeting the NEW version
        )
    else:
        return None  # Concurrency loss; let orchestrator decide next steps
```

### Why This Matters in Production

At 1,000 applications/hour with 4 agents each, on a loan that triggers parallel analysis (e.g., expedited review track):

- **Expected daily collisions**: ~20–50 optimistic concurrency errors
- **Without OCC** (naive append): Both analyses apply; state becomes **incoherent**—which decision is authoritative?
- **With OCC**: Exactly one wins; the other **knows** it lost and **can choose** to retry/supersede/abandon
- **Business consequence**: Credit risk models must be deterministic and commutative, OR the system must have explicit resolution logic

---

## 4. Projection Lag and Its Consequences

### The Scenario

ApplicationSummary projection (read model) has **typical lag of 200ms**:

```
T0:00: CreditAnalysisCompleted event appended to store. Current version: 4.
T0:00.050: Projection daemon processes batch, updates ApplicationSummary.
T0:00.200: UI query receives response. Sees state as of 999ms ago.

BUT:
T0:00.150: Loan officer queries "available credit limit" immediately after credit agent finishes.
T0:00.150: Sees ApplicationSummary from T0:00.000 (BEFORE the analysis completed event).
T0:00.150: UI shows old limit. Officer sees stale data.
```

### What System Does

```python
class ApplicationSummaryProjection:
    async def get_current_application(self, app_id: str):
        # This query pulls from Postgres table (inline projection)
        # NOT from events, so it can be stale
        row = await self.db.fetchrow(
            "SELECT * FROM application_summary WHERE application_id = $1",
            app_id
        )
        return ApplicationSummaryModel(**row)

# The application_summary table is updated by the ProjectionDaemon
# which runs asynchronously with ~200ms lag
```

### How We Communicate This to UI

**Strategy 1: Strong Consistency for Critical Queries** (Recommended)

```python
class LoanOfficerPortal:
    async def get_credit_limit(self, app_id: str):
        # For critical queries, bypass projection
        # Load aggregate directly for strong consistency
        app = await LoanApplicationAggregate.load(store, app_id)
        return LoanOfficerView(
            available_limit=app.approved_credit_limit,
            as_of="authoritative_now",  # Signal to UI
            lag="<10ms"  # Direct load, no projection lag
        )
```

**Strategy 2: Projection with Lag Awareness**

```python
class LoanOfficerPortal:
    async def get_credit_limit(self, app_id: str):
        summary = await self.projection.get_current_application(app_id)
        lag_ms = await self.daemon.get_lag()  # Get projection lag metric

        return LoanOfficerView(
            available_limit=summary.approved_limit,
            last_updated_at=summary.last_event_at,
            projection_lag_ms=lag_ms,
            ui_hint="Data may be up to {}ms old".format(lag_ms)
        )
```

**Strategy 3: Eventual Consistency with Polling**

```typescript
// Client-side: after submit, poll until lag closes
async function submitAndWait(cmd) {
  await submitCommand(cmd); // Event appended at T0

  while (true) {
    const state = await getApplicationState();
    if (state.includes(cmd.event_type)) {
      // Projection has caught up
      break;
    }
    await sleep(50); // Poll every 50ms
  }
}
```

### SLO Commitment

- **ApplicationSummary**: p99 lag < 500ms (UI queries acceptable; use lag-aware display)
- **ComplianceAuditView**: p99 lag < 2s (regulatory queries; less sensitive to real-time)
- **Health check endpoint** (exposes lag): p99 < 10ms (let UI know when to trust stale data)

---

## 5. The Upcasting Scenario

### New Event Schema (2026)

**2024 Schema (v1)**:

```python
{
    "application_id": "app-12345",
    "decision": "APPROVE",
    "reason": "Strong financial position"
}
```

**2026 Schema (v2)** — NEW FIELDS REQUIRED:

```python
{
    "application_id": "app-12345",
    "decision": "APPROVE",
    "reason": "Strong financial position",
    "model_version": "2.3.1",       # NEW: which credit model decided this?
    "confidence_score": 0.87,        # NEW: how confident was the model?
    "regulatory_basis": "REGULATION_X_2025"  # NEW: regulatory version at time of decision
}
```

### Upcaster Implementation

```python
from datetime import datetime
from ledger.schema.events import CreditDecisionMade
from ledger.upcasters.registry import UpcasterRegistry

registry = UpcasterRegistry()

@registry.register("CreditDecisionMade", from_version=1)
def upcast_credit_v1_to_v2(payload: dict) -> dict:
    """
    Transform v1 events to v2 schema.
    Inference strategy documented for each new field.
    """
    recorded_at = datetime.fromisoformat(payload.get("recorded_at", "2024-01-01T00:00:00Z"))

    # Field 1: model_version (inferred from timestamp)
    # Historical events before model 2.0 were marked "legacy-pre-2026"
    # Boundary: 2024-Q1 → v1.x, 2024-Q3 → v1.5, 2025-Q1 → v2.0, 2025-Q3 → v2.1, 2026-Q1 → v2.3+
    def infer_model_version(ts: datetime) -> str:
        if ts < datetime(2024, 9, 1):
            return "1.5-legacy"  # Historical v1
        elif ts < datetime(2025, 1, 1):
            return "1.9-transition"
        elif ts < datetime(2025, 9, 1):
            return "2.0-baseline"
        else:
            return "2.1+"  # Up to March 2026

    # Field 2: confidence_score (historical unknown)
    # CRITICAL DECISION: Use null, not inference
    # WHY: confidence_score is calibrated per event. Guessing a number would:
    #   a) Inflate historical confidence (false negative on risk assessment)
    #   b) Pollute regulatory audit trail (auditor sees 0.72, model never output 0.72)
    #   c) Break downstream risk models that depend on accurate historical confidence
    # BETTER: Null signals "unknown — do not use in analysis"
    confidence_score = None  # DO NOT infer; mark as unknown

    # Field 3: regulatory_basis (inferred from effective regulation version at time)
    # Regulation X was updated Q2 2025; before that it was Regulation X v1.0
    regulation_map = {
        "2024-01-01": "REGULATION_X_2024",
        "2024-07-01": "REGULATION_X_2024_REVISED",
        "2025-01-01": "REGULATION_X_2025",
        "2026-01-01": "REGULATION_X_2025_AMENDMENT",
    }
    def infer_regulation_basis(ts: datetime) -> str:
        for effective_date_str in sorted(regulation_map.keys(), reverse=True):
            effective_date = datetime.fromisoformat(effective_date_str)
            if ts >= effective_date:
                return regulation_map[effective_date_str]
        return "REGULATION_X_2024"  # Earliest known version

    return {
        **payload,
        "model_version": infer_model_version(recorded_at),
        "confidence_score": confidence_score,
        "regulatory_basis": infer_regulation_basis(recorded_at),
    }
```

### Inference Strategy: Error Rates and Consequences

| Field              | Inference Type                    | Error Rate                            | Consequence of Error                                                                          | Strategy                                                               |
| ------------------ | --------------------------------- | ------------------------------------- | --------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| `model_version`    | Timestamp-based bucketing         | ~5% (model boundaries uncertain)      | Regulatory audit shows "model v1.5" when v1.9 was used. Reputational but not material.        | Accept; document in audit trail that this is inferred                  |
| `confidence_score` | **NULL (not inferred)**           | N/A                                   | N/A                                                                                           | Null is correct answer; downstream code must handle unknown confidence |
| `regulatory_basis` | Regulation version history lookup | <1% (regulation dates are definitive) | Audit shows old rule version for events near transition date. Could affect compliance review. | Lookup definitive regulation timeline; prefer over guessing            |

### When to Use Null vs. Inference

**Use NULL when**:

- Field genuinely did not exist historically (e.g., confidence_score created in v2 model)
- Guessing would be **worse than honest missing data** (e.g., ML confidence is not even defined for v1)
- Consumer code must handle missing value (projection, compliance check, etc.)

**Use Inference when**:

- Field can be deterministically reconstructed from available data (e.g., model version from date boundaries)
- Error rate is acceptably low (<5%)
- Consequence of error is audit-trail pollution but not business decision change

---

## 6. The Marten Async Daemon Parallel: Distributed Projection Execution

### Marten 7.0 Architecture

Marten (the .NET event store standard) runs projections on **multiple nodes** in a cluster:

```
┌─ Node 1: PostgreSQL               ┌─ Node 2: Async Daemon           ┌─ Node 3: Async Daemon
│ ┌─ events table                   │ Projection checkpoint: KYC-100   │ Projection checkpoint: KYC-101
│ │ global_position: 0–1000         │ Processing: → 102               │ Processing: → 103
│ └                                 │                                  │
└─ Projection checkpoints (shared)   └─────────────────────────────────┘─────────────────────────
  KYC_checks: 100
  Fraud_flags: 99  <-- Daemon 3 is ahead on fraud, behind on KYC
```

### Distributed Projection Challenges

1. **Gap**: Node 2 processes events 100–102, Node 3 processes 99–103
   - Ordering violated; projection state inconsistent across nodes
2. **Lost work**: Node 2 crashes after updating checkpoint but before writing projection data
   - Checkpoint is advanced (100→102) but projection is at 100
   - Recovering node replays duplicates 100–102
3. **Cascading failure**: All 3 nodes decide to process same batch; all write updates; database receives 3x writes

### Solution: Distributed Coordination Primitive

**Database-backed coordination** (PostgreSQL advisory locks):

```python
class DistributedProjectionDaemon:
    async def run_forever(self, poll_interval_ms=100):
        while True:
            # Acquire exclusive lock on this projection across cluster
            async with self.lock_manager.acquire(f"projection:{self.projection.name}"):
                # Only ONE node at a time holds this lock
                await self._process_batch()

            await asyncio.sleep(poll_interval_ms / 1000)

class LockManager:
    async def acquire(self, lock_key: str):
        # PostgreSQL advisory_lock: prevents concurrent execution
        # Built-in PostgreSQL lock; no external coordinator needed
        conn = await self.pool.acquire()
        try:
            # Block until lock acquired
            await conn.execute(f"SELECT pg_advisory_lock(hashtext('{lock_key}'))")
            yield conn
        finally:
            await conn.execute(f"SELECT pg_advisory_unlock(hashtext('{lock_key}'))")
            await self.pool.release(conn)
```

### Failure Mode It Guards Against

```python
# WITHOUT distributed lock:
T0: Node-1 reads checkpoint: "fraud_projection: 99"
T0: Node-2 reads checkpoint: "fraud_projection: 99"  # Same checkpoint!
T1: Node-1 processes events 100–102, appends to fraud_flags_projection
T1: Node-2 processes events 100–102, appends AGAIN to fraud_flags_projection
T2: Database now has DUPLICATES (same calculation 3x)

# WITH PostgreSQL advisory lock:
T0: Node-1 acquires lock on "projection:fraud_flags"
T0: Node-2 tries acquire, BLOCKS
T1: Node-1 processes 100–102, updates checkpoint
T2: Node-1 releases lock
T3: Node-2 acquires lock, reads checkpoint NOW at 102, processes 103 onward
# RESULT: Sequential processing; no duplicates; all nodes in sync
```

### Python Implementation Strategy

```python
# Use PostgreSQL built-in pg_advisory_lock
# One daemon per projection per cluster
# Daemons compete for locks at startup
# Winner keeps running; losers standby for failover

async def ensure_single_daemon_per_projection():
    """Only one daemon processes each projection cluster-wide."""
    async with pool.acquire() as conn:
        # Try to acquire lock; block if someone else has it
        lock_id = crc32(f"projection:application_summary")
        await conn.execute("SELECT pg_advisory_lock($1)", lock_id)
        # Now exclusively run projection daemon
        await ProjectionDaemon(store).run_forever()
        # Release lock on exit (even on crash, PG releases on connection close)
```

### Why This Matters for The Ledger

- **Horizontal scaling**: Add more daemon nodes without risk of duplicate projections
- **Resilience**: Daemon crashes; lock auto-releases; standby Node takes over
- **No external coordinator**: Kafka, Redis, Zookeeper not needed; PG's advisory locks are enough
- **Eventual consistency**: All nodes converge to same state; lag metric stays bounded

---

## Summary: Architecture Decisions

| Decision                                        | Rationale                                                 | Trade-off                                                            |
| ----------------------------------------------- | --------------------------------------------------------- | -------------------------------------------------------------------- |
| **Four aggregates**                             | Independent consistency boundaries; no cascading failures | More streams to manage; must enforce causal links explicitly         |
| **Optimistic concurrency**                      | First-writer-wins fairness; no distributed locks needed   | Losing writer must retry; requires sensible retry logic              |
| **Async projections with lag**                  | Scale reads independently; avoid write-path bottleneck    | Eventual consistency; UI must handle stale data gracefully           |
| **Null over inference for missing fields**      | Honest about unknowns; prevents audit trail pollution     | Downstream code must handle null; requires defensive checks          |
| **Distributed coordination via advisory locks** | No external service; leverages PG; auto-failover          | Requires careful checkpoint management; single leader per projection |

---

## References

- Vernon, "Implementing Domain-Driven Design", Ch 10: Aggregates
- Greg Young, "CQRS and Event Sourcing" (oredev.conf)
- Marten 7.0 documentation: AsyncDaemon & distributed projections
- PostgreSQL advisory locks documentation
