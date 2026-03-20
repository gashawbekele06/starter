"""
ledger/event_store.py — PostgreSQL-backed Event Store (The Ledger)
===================================================================

Core append-only event store with:
  ✓ Optimistic Concurrency Control (OCC)
  ✓ Atomic appends with outbox in same transaction
  ✓ Stream versioning for aggregates
  ✓ Efficient replay (load_stream) & streaming (load_all)
  ✓ Upcaster integration for schema evolution
  ✓ Stream archival support
"""
from __future__ import annotations
import json
from datetime import datetime
from typing import AsyncGenerator, Optional
from uuid import UUID, uuid4
import asyncpg
from contextlib import asynccontextmanager

from ledger.models.events import BaseEvent, StoredEvent, StreamMetadata


# ─────────────────────────────────────────────────────────────────────────────
# EXCEPTIONS
# ─────────────────────────────────────────────────────────────────────────────

class OptimisticConcurrencyError(Exception):
    """
    Raised when stream version doesn't match expected_version on append.
    Caller must reload stream and retry with updated expected_version.
    """
    def __init__(
        self,
        stream_id: str,
        expected_version: int,
        actual_version: int,
        suggested_action: str = "reload_stream_and_retry"
    ):
        self.stream_id = stream_id
        self.expected_version = expected_version
        self.actual_version = actual_version
        self.suggested_action = suggested_action
        super().__init__(
            f"OCC on '{stream_id}': expected v{expected_version}, "
            f"actual v{actual_version}. {suggested_action}"
        )


class StreamNotFoundError(Exception):
    """Raised when stream has no events."""
    def __init__(self, stream_id: str):
        self.stream_id = stream_id
        super().__init__(f"Stream '{stream_id}' not found")


# ─────────────────────────────────────────────────────────────────────────────
# POSTGRESQL EVENT STORE
# ─────────────────────────────────────────────────────────────────────────────

class EventStore:
    """
    Append-only PostgreSQL event store. Thread-safe for asyncio.
    All aggregates and projections use this single interface.
    """

    def __init__(self, db_url: str, upcaster_registry=None):
        """
        Args:
            db_url: PostgreSQL URL (postgresql://user:pass@host/db)
            upcaster_registry: UpcasterRegistry for schema evolution (optional)
        """
        self.db_url = db_url
        self.upcasters = upcaster_registry
        self._pool: Optional[asyncpg.Pool] = None

    async def connect(self) -> None:
        """Establish connection pool."""
        self._pool = await asyncpg.create_pool(
            self.db_url,
            min_size=2,
            max_size=10,
            command_timeout=30
        )

    async def close(self) -> None:
        """Close connection pool."""
        if self._pool:
            await self._pool.close()

    @asynccontextmanager
    async def _transaction(self):
        """Context manager for transactions."""
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                yield conn

    async def stream_version(self, stream_id: str) -> int:
        """
        Get current stream version. Returns -1 if stream doesn't exist.
        
        Args:
            stream_id: Target stream

        Returns:
            Current version (-1 if new stream)
        """
        async with self._pool.acquire() as conn:
            version = await conn.fetchval(
                "SELECT current_version FROM event_streams WHERE stream_id = $1",
                stream_id
            )
        return version if version is not None else -1

    async def append(
        self,
        stream_id: str,
        events: list[BaseEvent],
        expected_version: int,
        correlation_id: Optional[str] = None,
        causation_id: Optional[str] = None,
    ) -> int:
        """
        Atomically append events with optimistic concurrency control.

        Contract:
          - If expected_version != actual stream version → OptimisticConcurrencyError
          - All events written atomically in same transaction
          - Outbox rows created for async publication
          - Returns new stream version

        Args:
            stream_id: Target stream (e.g., "loan-app-12345")
            events: List of domain events to append
            expected_version: Expected current version (-1 for new stream)
            correlation_id: Optional trace ID
            causation_id: Optional causal predecessor event ID

        Returns:
            New stream version after successful append

        Raises:
            OptimisticConcurrencyError: If version mismatch
            ValueError: If events list empty
        """
        if not events:
            raise ValueError("Cannot append empty event list")

        async with self._transaction() as conn:
            # 1. Lock stream and get current version
            current_version = await conn.fetchval(
                "SELECT current_version FROM event_streams "
                "WHERE stream_id = $1 FOR UPDATE",
                stream_id
            )

            # 2. Handle new vs existing stream
            if current_version is None:
                if expected_version != -1:
                    raise OptimisticConcurrencyError(
                        stream_id, expected_version, -1,
                        "Stream doesn't exist; expected_version must be -1"
                    )
                # Create stream
                await conn.execute(
                    "INSERT INTO event_streams (stream_id, aggregate_type, current_version, metadata) "
                    "VALUES ($1, $2, $3, $4)",
                    stream_id,
                    stream_id.split("-")[0] if "-" in stream_id else "Unknown",
                    0,
                    json.dumps({"created_at": datetime.utcnow().isoformat()})
                )
                current_version = 0
                next_position = 1
            else:
                # 3. Check OCC
                if current_version != expected_version:
                    raise OptimisticConcurrencyError(
                        stream_id, expected_version, current_version
                    )
                next_position = current_version + 1

            # 4. Prepare and insert events
            now_iso = datetime.utcnow().isoformat() + "Z"
            
            for i, event in enumerate(events):
                event_id = uuid4()
                stream_position = next_position + i
                event_type = event.__class__.__name__
                event_version = getattr(event, "event_version", 1)
                
                # Serialize payload
                if hasattr(event, "model_dump"):
                    payload = event.model_dump(exclude_none=True)
                else:
                    payload = event.__dict__
                
                # Event metadata
                metadata = {
                    "correlation_id": correlation_id or "",
                    "causation_id": causation_id or "",
                }
                
                # Insert event
                await conn.execute(
                    "INSERT INTO events "
                    "(event_id, stream_id, stream_position, event_type, event_version, "
                    "payload, metadata, recorded_at) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                    event_id,
                    stream_id,
                    stream_position,
                    event_type,
                    event_version,
                    json.dumps(payload),
                    json.dumps(metadata),
                    now_iso
                )
                
                # Insert outbox record for async publication
                await conn.execute(
                    "INSERT INTO outbox (event_id, destination, payload) "
                    "VALUES ($1, $2, $3)",
                    event_id,
                    f"event_stream:{event_type}",
                    json.dumps(payload)
                )
            
            # 5. Update stream version
            new_version = next_position + len(events) - 1
            await conn.execute(
                "UPDATE event_streams SET current_version = $1 WHERE stream_id = $2",
                new_version,
                stream_id
            )
            
            return new_version

    async def load_stream(
        self,
        stream_id: str,
        from_position: int = 0,
        to_position: Optional[int] = None,
    ) -> list[StoredEvent]:
        """
        Load events from stream in order, with automatic upcasting.

        Used by aggregates to reconstruct state via replay.

        Args:
            stream_id: Target stream
            from_position: Start from this position (0 = all)
            to_position: Stop at this position (None = all remaining)

        Returns:
            List of StoredEvent objects, upcasted to latest version

        Raises:
            StreamNotFoundError: If stream has no events
        """
        query = """
            SELECT event_id, stream_id, stream_position, global_position,
                   event_type, event_version, payload, metadata, recorded_at
            FROM events
            WHERE stream_id = $1
        """
        params = [stream_id]

        if from_position > 0:
            query += " AND stream_position >= $2"
            params.append(from_position)

        if to_position is not None:
            query += f" AND stream_position <= ${len(params) + 1}"
            params.append(to_position)

        query += " ORDER BY stream_position ASC"

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)

        if not rows:
            raise StreamNotFoundError(stream_id)

        stored_events = []
        for row in rows:
            event = StoredEvent(
                event_id=row["event_id"],
                stream_id=row["stream_id"],
                stream_position=row["stream_position"],
                global_position=row["global_position"],
                event_type=row["event_type"],
                event_version=row["event_version"],
                payload=json.loads(row["payload"]),
                metadata=json.loads(row["metadata"]),
                recorded_at=row["recorded_at"],
            )
            
            if self.upcasters:
                event = self.upcasters.upcast(event)
            
            stored_events.append(event)

        return stored_events

    async def load_all(
        self,
        from_global_position: int = 0,
        event_types: Optional[list[str]] = None,
        batch_size: int = 500,
    ) -> AsyncGenerator[StoredEvent, None]:
        """
        Stream all events in global order.

        Used by ProjectionDaemon for efficient batch processing.

        Args:
            from_global_position: Start from this global position
            event_types: Optional filter by event type
            batch_size: Events per database query

        Yields:
            StoredEvent objects in global order, upcasted
        """
        position = from_global_position

        while True:
            query = """
                SELECT event_id, stream_id, stream_position, global_position,
                       event_type, event_version, payload, metadata, recorded_at
                FROM events
                WHERE global_position > $1
            """
            params = [position]

            if event_types:
                placeholders = ", ".join(f"${i}" for i in range(2, 2 + len(event_types)))
                query += f" AND event_type IN ({placeholders})"
                params.extend(event_types)

            query += f" ORDER BY global_position ASC LIMIT ${len(params) + 1}"
            params.append(batch_size)

            async with self._pool.acquire() as conn:
                rows = await conn.fetch(query, *params)

            if not rows:
                break

            for row in rows:
                event = StoredEvent(
                    event_id=row["event_id"],
                    stream_id=row["stream_id"],
                    stream_position=row["stream_position"],
                    global_position=row["global_position"],
                    event_type=row["event_type"],
                    event_version=row["event_version"],
                    payload=json.loads(row["payload"]),
                    metadata=json.loads(row["metadata"]),
                    recorded_at=row["recorded_at"],
                )
                
                if self.upcasters:
                    event = self.upcasters.upcast(event)
                
                yield event
                position = row["global_position"]

    async def get_event(self, event_id: UUID) -> Optional[StoredEvent]:
        """Load single event by ID."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT event_id, stream_id, stream_position, global_position, "
                "event_type, event_version, payload, metadata, recorded_at "
                "FROM events WHERE event_id = $1",
                event_id
            )

        if not row:
            return None

        event = StoredEvent(
            event_id=row["event_id"],
            stream_id=row["stream_id"],
            stream_position=row["stream_position"],
            global_position=row["global_position"],
            event_type=row["event_type"],
            event_version=row["event_version"],
            payload=json.loads(row["payload"]),
            metadata=json.loads(row["metadata"]),
            recorded_at=row["recorded_at"],
        )
        
        if self.upcasters:
            event = self.upcasters.upcast(event)
        
        return event

    async def archive_stream(self, stream_id: str) -> None:
        """Mark stream as archived (advisory; no appends prevented by store)."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE event_streams SET archived_at = CURRENT_TIMESTAMP "
                "WHERE stream_id = $1",
                stream_id
            )

    async def get_stream_metadata(self, stream_id: str) -> Optional[StreamMetadata]:
        """Get stream metadata."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT stream_id, aggregate_type, current_version, "
                "created_at, archived_at, metadata FROM event_streams "
                "WHERE stream_id = $1",
                stream_id
            )

        if not row:
            return None

        return StreamMetadata(
            stream_id=row["stream_id"],
            aggregate_type=row["aggregate_type"],
            current_version=row["current_version"],
            created_at=row["created_at"],
            archived_at=row["archived_at"],
            metadata=json.loads(row["metadata"]) if row["metadata"] else {}
        )

    async def get_event_count(self, stream_id: str) -> int:
        """Get total events in stream."""
        async with self._pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM events WHERE stream_id = $1",
                stream_id
            )
        return count or 0

    async def truncate_all(self) -> None:
        """Dangerous: Delete all events. Tests only."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "TRUNCATE TABLE events, event_streams, projection_checkpoints, outbox CASCADE"
            )


# ─────────────────────────────────────────────────────────────────────────────
# IN-MEMORY EVENT STORE (for tests)
# ─────────────────────────────────────────────────────────────────────────────

class InMemoryEventStore:
    """
    Non-persistent event store for unit tests.
    Identical interface to EventStore. Drop-in replacement for testing.
    """

    def __init__(self, upcaster_registry=None):
        self.upcasters = upcaster_registry
        self._streams: dict[str, list[dict]] = {}
        self._versions: dict[str, int] = {}
        self._global: list[dict] = []
        self._checkpoints: dict[str, int] = {}

    async def stream_version(self, stream_id: str) -> int:
        return self._versions.get(stream_id, -1)

    async def append(
        self,
        stream_id: str,
        events: list[BaseEvent],
        expected_version: int,
        correlation_id: Optional[str] = None,
        causation_id: Optional[str] = None,
    ) -> int:
        current = self._versions.get(stream_id, -1)
        if current != expected_version:
            raise OptimisticConcurrencyError(stream_id, expected_version, current)

        positions = []
        for i, event in enumerate(events):
            pos = current + 1 + i
            
            if hasattr(event, "model_dump"):
                payload = event.model_dump(exclude_none=True)
            else:
                payload = event.__dict__

            stored = {
                "event_id": str(uuid4()),
                "stream_id": stream_id,
                "stream_position": pos,
                "global_position": len(self._global),
                "event_type": event.__class__.__name__,
                "event_version": getattr(event, "event_version", 1),
                "payload": payload,
                "metadata": {
                    "correlation_id": correlation_id or "",
                    "causation_id": causation_id or "",
                },
                "recorded_at": datetime.utcnow(),
            }
            
            self._streams.setdefault(stream_id, []).append(stored)
            self._global.append(stored)
            positions.append(pos)

        self._versions[stream_id] = current + len(events)
        return current + len(events)

    async def load_stream(
        self,
        stream_id: str,
        from_position: int = 0,
        to_position: Optional[int] = None,
    ) -> list[StoredEvent]:
        events_list = self._streams.get(stream_id, [])
        filtered = [
            e for e in events_list
            if e["stream_position"] >= from_position
            and (to_position is None or e["stream_position"] <= to_position)
        ]

        if not filtered:
            raise StreamNotFoundError(stream_id)

        return [
            StoredEvent(
                event_id=UUID(e["event_id"]),
                stream_id=e["stream_id"],
                stream_position=e["stream_position"],
                global_position=e["global_position"],
                event_type=e["event_type"],
                event_version=e["event_version"],
                payload=e["payload"],
                metadata=e["metadata"],
                recorded_at=e["recorded_at"],
            )
            for e in sorted(filtered, key=lambda x: x["stream_position"])
        ]

    async def load_all(
        self,
        from_global_position: int = 0,
        event_types: Optional[list[str]] = None,
        batch_size: int = 500,
    ) -> AsyncGenerator[StoredEvent, None]:
        for e in self._global:
            if e["global_position"] > from_global_position:
                if event_types is None or e["event_type"] in event_types:
                    yield StoredEvent(
                        event_id=UUID(e["event_id"]),
                        stream_id=e["stream_id"],
                        stream_position=e["stream_position"],
                        global_position=e["global_position"],
                        event_type=e["event_type"],
                        event_version=e["event_version"],
                        payload=e["payload"],
                        metadata=e["metadata"],
                        recorded_at=e["recorded_at"],
                    )

    async def get_event(self, event_id: UUID) -> Optional[StoredEvent]:
        for e in self._global:
            if e["event_id"] == str(event_id):
                return StoredEvent(
                    event_id=UUID(e["event_id"]),
                    stream_id=e["stream_id"],
                    stream_position=e["stream_position"],
                    global_position=e["global_position"],
                    event_type=e["event_type"],
                    event_version=e["event_version"],
                    payload=e["payload"],
                    metadata=e["metadata"],
                    recorded_at=e["recorded_at"],
                )
        return None

    async def truncate_all(self) -> None:
        """Clear all events."""
        self._streams.clear()
        self._versions.clear()
        self._global.clear()
