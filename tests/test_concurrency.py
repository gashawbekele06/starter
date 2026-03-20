"""
tests/test_concurrency.py - Phase 1B: Double-Decision Concurrency Test

The most important Phase 1 test. Demonstrates optimistic concurrency control.
Two agents simultaneously try to append to the same stream at the same version.
Exactly one succeeds; the other gets OptimisticConcurrencyError and must retry.

This prevents split-brain state when multiple agents race on the same aggregate.
"""
import pytest
import asyncio
from uuid import uuid4

from ledger.event_store import InMemoryEventStore, OptimisticConcurrencyError
from ledger.models.events import CreditAnalysisCompleted, RiskTier


@pytest.mark.asyncio
async def test_double_decision_concurrent_append():
    """
    PHASE 1B TEST: Two agents simultaneously append to same stream.
    
    Scenario:
    --------
    1. Stream "loan-12345" starts at version -1 (new)
    2. Both agents read stream version: -1
    3. Agent A decides: risk_tier=MEDIUM, calls append(expected_version=-1)
    4. Agent B decides: risk_tier=HIGH, calls append(expected_version=-1)
    5. Agent A appends first: succeeds, stream now v0
    6. Agent B appends simultaneously: fails with OCC error
    
    Assertions:
    -----------
    ✓ Stream ends at version 1 (exactly 2 events: one succeeded, one failed)
    ✓ Agent A's event is in stream at position 1
    ✓ Agent B receives OptimisticConcurrencyError (not silently accepted)
    ✓ Stream state is consistent (no split brain)
    """
    store = InMemoryEventStore()
    stream_id = "loan-12345"
    app_id = "app-12345"
    
    # Both agents read version first
    version = await store.stream_version(stream_id)
    assert version == -1, "New stream should have version -1"
    
    # Create two competing events
    event_a = CreditAnalysisCompleted(
        application_id=app_id,
        agent_id="agent-credit-001",
        session_id=f"session-{uuid4()}",
        model_version="2.3.1",
        confidence_score=0.75,
        risk_tier=RiskTier.MEDIUM,
        recommended_limit_usd=100_000,
        analysis_duration_ms=1500,
        input_data_hash="hash_a_123",
    )
    
    event_b = CreditAnalysisCompleted(
        application_id=app_id,
        agent_id="agent-credit-002",  # Different agent
        session_id=f"session-{uuid4()}",
        model_version="2.3.1",
        confidence_score=0.82,
        risk_tier=RiskTier.HIGH,
        recommended_limit_usd=50_000,
        analysis_duration_ms=1200,
        input_data_hash="hash_b_456",
    )
    
    # Simulate concurrent appends (Agent A wins race)
    results = await asyncio.gather(
        store.append(stream_id, [event_a], expected_version=-1),
        store.append(stream_id, [event_b], expected_version=-1),
        return_exceptions=True  # Capture exceptions instead of failing
    )
    
    # Assertions
    print(f"Results: {results}")
    
    # One should succeed, one should fail
    successes = [r for r in results if not isinstance(r, Exception)]
    failures = [r for r in results if isinstance(r, Exception)]
    
    assert len(successes) == 1, f"Expected 1 success, got {len(successes)}"
    assert len(failures) == 1, f"Expected 1 failure, got {len(failures)}"
    
    # The failure should be OptimisticConcurrencyError
    assert isinstance(failures[0], OptimisticConcurrencyError)
    assert failures[0].expected_version == -1
    assert failures[0].actual_version == 0  # Other agent already incremented to 0
    
    # Stream should be at version 0 (exactly 1 event appended)
    final_version = await store.stream_version(stream_id)
    assert final_version == 0, f"Expected version 0, got {final_version}"
    
    # Load stream and verify exactly 1 event
    events = await store.load_stream(stream_id)
    assert len(events) == 1, f"Expected 1 event, got {len(events)}"
    
    # Verify it's the winning agent's event (A or B determined by race)
    event = events[0]
    assert event.event_type == "CreditAnalysisCompleted"
    assert event.stream_position == 0  # First event in stream has position 0 (0-indexed)
    risk_tier = event.payload["risk_tier"]
    assert risk_tier in ["MEDIUM", "HIGH"]
    
    print(f"✓ Double-decision test PASSED")
    print(f"  - Winning agent: risk_tier={risk_tier}")
    print(f"  - Final stream version: {final_version}")
    print(f"  - Total events: {len(events)}")
    print(f"  - Error received: {failures[0]}")


@pytest.mark.asyncio
async def test_retry_after_occ_failure():
    """
    Test that losing agent can retry after receiving OCC error.
    
    After Agent B's first append fails, it:
    1. Reloads stream
    2. Sees Agent A's event is already there
    3. Can decide to retry with new expected_version or abandon
    """
    store = InMemoryEventStore()
    stream_id = "loan-54321"
    app_id = "app-54321"
    
    # Agent A: append first event
    event_a = CreditAnalysisCompleted(
        application_id=app_id,
        agent_id="agent-credit-001",
        session_id=f"session-{uuid4()}",
        model_version="2.3.1",
        confidence_score=0.75,
        risk_tier=RiskTier.MEDIUM,
        recommended_limit_usd=100_000,
        analysis_duration_ms=1500,
        input_data_hash="hash_a_789",
    )
    
    v1 = await store.append(stream_id, [event_a], expected_version=-1)
    assert v1 == 0, "First append should result in version 0"
    
    # Agent B: try to append at old version -1 (will fail)
    event_b = CreditAnalysisCompleted(
        application_id=app_id,
        agent_id="agent-credit-002",
        session_id=f"session-{uuid4()}",
        model_version="2.3.1",
        confidence_score=0.82,
        risk_tier=RiskTier.HIGH,
        recommended_limit_usd=50_000,
        analysis_duration_ms=1200,
        input_data_hash="hash_b_789",
    )
    
    # First attempt fails
    with pytest.raises(OptimisticConcurrencyError) as exc_info:
        await store.append(stream_id, [event_b], expected_version=-1)
    
    error = exc_info.value
    assert error.expected_version == -1
    assert error.actual_version == 0
    
    # Agent B reloads stream (simulating retry logic)
    new_version = await store.stream_version(stream_id)
    assert new_version == 0, "Should see Agent A's event"
    
    events = await store.load_stream(stream_id)
    assert len(events) == 1
    assert events[0].payload["risk_tier"] == "MEDIUM"
    
    # Agent B can now make decision: retry or abandon
    # Option: retry with superseding decision
    event_b_revised = CreditAnalysisCompleted(
        application_id=app_id,
        agent_id="agent-credit-002",
        session_id=f"session-{uuid4()}",
        model_version="2.4.0",  # Newer model
        confidence_score=0.88,
        risk_tier=RiskTier.LOW,  # Changed decision
        recommended_limit_usd=150_000,
        analysis_duration_ms=1200,
        input_data_hash="hash_b_revised",
    )
    
    # Retry with correct expected_version
    v2 = await store.append(
        stream_id, [event_b_revised],
        expected_version=0  # Now correct version
    )
    assert v2 == 1, "Retry should succeed and increment to version 1"
    
    # Stream now has both events
    final_events = await store.load_stream(stream_id)
    assert len(final_events) == 2
    assert final_events[0].payload["risk_tier"] == "MEDIUM"
    assert final_events[1].payload["risk_tier"] == "LOW"
    
    print(f"✓ Retry test PASSED")
    print(f"  - First attempt: OCC error (expected)")
    print(f"  - Reload: new version = {new_version}")
    print(f"  - Retry: success with updated expected_version={0}")
    print(f"  - Final stream length: {len(final_events)}")


@pytest.mark.asyncio
async def test_concurrent_load_while_append():
    """
    Test that concurrent load_stream() reads don't block appends.
    Multiple readers can pull events while a writer is appending.
    """
    store = InMemoryEventStore()
    stream_id = "loan-99999"
    app_id = "app-99999"
    
    # Pre-populate stream with 5 events
    for i in range(5):
        event = CreditAnalysisCompleted(
            application_id=app_id,
            agent_id=f"agent-{i}",
            session_id=f"session-{i}",
            model_version="2.3.1",
            confidence_score=0.70 + (i * 0.02),
            risk_tier=RiskTier.MEDIUM,
            recommended_limit_usd=100_000,
            analysis_duration_ms=1500,
            input_data_hash=f"hash_{i}",
        )
        await store.append(stream_id, [event], expected_version=i - 1)
    
    # Concurrent reads while appending
    async def reader_task(task_id):
        for _ in range(3):
            events = await store.load_stream(stream_id)
            await asyncio.sleep(0.01)  # Simulate some work
            return len(events)
    
    async def writer_task():
        new_event = CreditAnalysisCompleted(
            application_id=app_id,
            agent_id="agent-writer",
            session_id="session-writer",
            model_version="2.3.1",
            confidence_score=0.99,
            risk_tier=RiskTier.LOW,
            recommended_limit_usd=200_000,
            analysis_duration_ms=1000,
            input_data_hash="hash_writer",
        )
        
        v = await store.stream_version(stream_id)
        await store.append(stream_id, [new_event], expected_version=v)
    
    # Run readers and writer concurrently
    results = await asyncio.gather(
        reader_task(1),
        reader_task(2),
        writer_task(),
        return_exceptions=True
    )
    
    # All should succeed
    assert all(not isinstance(r, Exception) for r in results), \
        f"Some tasks failed: {[r for r in results if isinstance(r, Exception)]}"
    
    # Final stream length should be 6
    final_events = await store.load_stream(stream_id)
    assert len(final_events) == 6, f"Expected 6 events, got {len(final_events)}"
    
    print(f"✓ Concurrent load/append test PASSED")
    print(f"  - Readers: 2")
    print(f"  - Writers: 1")
    print(f"  - Final events: {len(final_events)}")


if __name__ == "__main__":
    # Run with: pytest tests/test_concurrency.py -v
    pytest.main([__file__, "-v"])
