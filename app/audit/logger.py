"""Writing the audit trail (arch §24).

Two destinations, deliberately. Every event goes to the structured log
immediately — that path has no dependencies and survives a database outage — and
is also queued for the `audit_logs` table, which is what the `/api/audit`
endpoint and any retention policy read.

The database write is asynchronous and best-effort. An audit write must never
turn a successful tool call into a failed one, and it must never add its latency
to the caller's request: a background writer drains a bounded queue, and if the
queue fills the event is dropped *after* being logged, with a counter recording
how many were lost. That is the honest failure mode — the alternative is either
blocking user traffic behind an overloaded database or silently pretending
nothing was lost.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, delete, desc, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.models import AuditEvent, AuditStatus
from app.core.clock import Clock, SystemClock, utcnow
from app.core.domain import AuditAction
from app.core.logging import get_logger
from app.database.models import AuditLog

__all__ = ["AuditLogger"]

log = get_logger("audit")

_QUEUE_LIMIT = 4096
_BATCH_SIZE = 64
_FLUSH_INTERVAL_SECONDS = 0.25


class AuditLogger:
    """Records auditable actions to the log and the database."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        clock: Clock | None = None,
        queue_limit: int = _QUEUE_LIMIT,
    ) -> None:
        self._sessions = session_factory
        self._clock = clock or SystemClock()
        self._queue: asyncio.Queue[AuditEvent] = asyncio.Queue(maxsize=queue_limit)
        self._writer: asyncio.Task[None] | None = None
        self._dropped = 0
        self._written = 0

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        """Start the background writer. Idempotent."""
        if self._sessions is None or (self._writer is not None and not self._writer.done()):
            return
        self._writer = asyncio.create_task(self._drain_forever(), name="audit-writer")
        log.debug("audit.writer_started")

    async def stop(self, *, drain_timeout: float = 5.0) -> None:
        """Flush what is queued, then stop the writer.

        Bounded by `drain_timeout` so a wedged database cannot hold shutdown
        open; anything still queued after that is reported as dropped.
        """
        if self._writer is None:
            return
        with suppress(TimeoutError):
            await asyncio.wait_for(self._queue.join(), timeout=drain_timeout)
        self._writer.cancel()
        with suppress(asyncio.CancelledError):
            await self._writer
        self._writer = None
        remaining = self._queue.qsize()
        if remaining:
            self._dropped += remaining
            log.warning("audit.shutdown_dropped", count=remaining)
        log.debug("audit.writer_stopped", written=self._written, dropped=self._dropped)

    @property
    def dropped_events(self) -> int:
        """Events logged but never persisted. Exposed as a metric."""
        return self._dropped

    # ---------------------------------------------------------------- record

    def record(self, event: AuditEvent) -> None:
        """Log an event and queue it for persistence.

        Synchronous and non-blocking by design: call sites are on the request
        path, and an audit record must not be something they await.
        """
        fields = event.to_log_fields()
        if event.status in ("denied", "error"):
            log.warning(f"audit.{event.action.value}", **fields)
        else:
            log.info(f"audit.{event.action.value}", **fields)

        if self._sessions is None:
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._dropped += 1
            log.error(
                "audit.queue_full",
                dropped_total=self._dropped,
                action=event.action.value,
                hint="The audit writer is not keeping up; the event was logged but not persisted.",
            )

    def emit(
        self,
        action: AuditAction,
        status: AuditStatus,
        *,
        raw_arguments: dict[str, Any] | None = None,
        **fields: Any,
    ) -> None:
        """Build and record an event in one call."""
        self.record(AuditEvent.build(action, status, raw_arguments=raw_arguments, **fields))

    # ----------------------------------------------------------------- drain

    async def _drain_forever(self) -> None:
        """Move queued events into the database in batches."""
        assert self._sessions is not None
        while True:
            batch = await self._collect_batch()
            if not batch:
                continue
            try:
                await self._persist(batch)
                self._written += len(batch)
            except Exception as exc:  # noqa: BLE001 - never let the writer die
                self._dropped += len(batch)
                log.error("audit.persist_failed", count=len(batch), error=str(exc))
            finally:
                for _ in batch:
                    self._queue.task_done()

    async def _collect_batch(self) -> list[AuditEvent]:
        """Wait for one event, then take whatever else is already queued.

        Batching keeps a burst of tool calls from becoming a burst of round
        trips, without adding latency when traffic is light.
        """
        first = await self._queue.get()
        batch = [first]
        deadline = asyncio.get_running_loop().time() + _FLUSH_INTERVAL_SECONDS
        while len(batch) < _BATCH_SIZE:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                batch.append(await asyncio.wait_for(self._queue.get(), timeout=remaining))
            except TimeoutError:
                break
        return batch

    async def _persist(self, batch: Sequence[AuditEvent]) -> None:
        assert self._sessions is not None
        async with self._sessions() as session, session.begin():
            session.add_all([AuditLog(**event.to_row()) for event in batch])

    async def flush(self) -> None:
        """Wait until everything queued has been written. For tests."""
        if self._sessions is None:
            return
        await self._queue.join()

    # ----------------------------------------------------------------- query

    async def query(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        user_id: str | None = None,
        integration: str | None = None,
        action: AuditAction | None = None,
        status: AuditStatus | None = None,
        since: datetime | None = None,
    ) -> list[AuditLog]:
        """Read the audit trail, newest first (arch §26 `GET /api/audit`)."""
        if self._sessions is None:
            return []
        statement = select(AuditLog).order_by(desc(AuditLog.timestamp)).limit(min(limit, 1000)).offset(offset)
        if user_id:
            statement = statement.where(AuditLog.user_id == user_id)
        if integration:
            statement = statement.where(AuditLog.integration == integration)
        if action:
            statement = statement.where(AuditLog.action == action.value)
        if status:
            statement = statement.where(AuditLog.status == status)
        if since:
            statement = statement.where(AuditLog.timestamp >= since)
        async with self._sessions() as session:
            return list(await session.scalars(statement))

    async def purge_older_than(self, days: int) -> int:
        """Delete records older than `days`. Returns how many were removed."""
        if self._sessions is None:
            return 0
        cutoff = utcnow() - timedelta(days=days)
        async with self._sessions() as session, session.begin():
            statement = delete(AuditLog).where(AuditLog.timestamp < cutoff)
            result = cast(CursorResult[Any], await session.execute(statement))
            removed = int(result.rowcount or 0)
        if removed:
            log.info("audit.purged", removed=removed, older_than_days=days)
        return removed
