"""Background jobs for long lifecycle operations (arch §29).

Arch §29 forbids running an update inside an HTTP handler, and the reason is
concrete: cloning and building a repository takes minutes, while a proxy will
close the connection long before that. So the API enqueues a job, returns its id
immediately, and the caller polls `GET /api/jobs/{job_id}`.

Jobs are recorded in the database before they run, so a status query works from
any replica and a crash leaves evidence rather than silence. Execution is
in-process: a worker task drains the queue with bounded concurrency. That is the
honest scope — it gives arch §29's API contract and its non-blocking behaviour
without pretending to be a distributed queue. The integration locks (arch §30)
are what actually prevent two replicas from updating the same integration, and
those *are* distributed when Redis is configured.

A job that dies with the process is marked `failed` on the next startup sweep
rather than being left `running` forever.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import utcnow
from app.core.context import RequestContext, bind_request, current_request
from app.core.domain import JobKind, JobStatus
from app.core.errors import HubError, ValidationFailed, describe_exception
from app.core.ids import new_job_id
from app.core.logging import get_logger
from app.database.models import JobRecord

if TYPE_CHECKING:
    from sqlalchemy import CursorResult

__all__ = ["Job", "JobHandler", "JobQueue"]

log = get_logger(__name__)

JobHandler = Callable[["Job"], Awaitable[dict[str, Any]]]
"""Runs one job and returns its JSON-serialisable result."""


@dataclass(slots=True)
class Job:
    """A unit of background work."""

    id: str
    kind: JobKind
    integrations: tuple[str, ...]
    parameters: dict[str, Any]
    requested_by: str | None
    request_id: str | None
    status: JobStatus = JobStatus.QUEUED
    progress: float = 0.0
    message: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None

    def to_payload(self) -> dict[str, Any]:
        """JSON form for `GET /api/jobs/{job_id}`."""
        return {
            "id": self.id,
            "kind": self.kind.value,
            "status": self.status.value,
            "integrations": list(self.integrations),
            "progress": round(self.progress, 3),
            "message": self.message,
            "result": self.result,
            "error": self.error,
        }


class JobQueue:
    """Accepts jobs, persists them, and runs them off the request path."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        concurrency: int = 2,
    ) -> None:
        self._sessions = session_factory
        self._handlers: dict[JobKind, JobHandler] = {}
        self._queue: asyncio.Queue[Job] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        self._concurrency = concurrency
        self._live: dict[str, Job] = {}

    # ------------------------------------------------------------ registration

    def register(self, kind: JobKind, handler: JobHandler) -> None:
        """Bind a handler to a job kind.

        Raises:
            ValidationFailed: That kind already has a handler — a silent
                overwrite would make behaviour depend on import order.
        """
        if kind in self._handlers:
            raise ValidationFailed(f"A handler is already registered for job kind {kind.value!r}.")
        self._handlers[kind] = handler

    # --------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        """Start workers and reconcile jobs orphaned by a previous crash."""
        await self._fail_orphaned()
        if self._workers:
            return
        self._workers = [
            asyncio.create_task(self._work(index), name=f"job-worker-{index}") for index in range(self._concurrency)
        ]
        log.info("jobs.started", workers=self._concurrency)

    async def stop(self, *, timeout: float = 10.0) -> None:  # noqa: ASYNC109
        """Let running jobs finish, then stop the workers."""
        if not self._workers:
            return
        from contextlib import suppress

        with suppress(TimeoutError):
            await asyncio.wait_for(self._queue.join(), timeout=timeout)
        for worker in self._workers:
            worker.cancel()
        for worker in self._workers:
            with suppress(asyncio.CancelledError):
                await worker
        self._workers.clear()
        log.info("jobs.stopped")

    # ------------------------------------------------------------------ submit

    async def submit(
        self,
        kind: JobKind,
        *,
        integrations: tuple[str, ...] = (),
        parameters: dict[str, Any] | None = None,
    ) -> Job:
        """Enqueue a job and return it immediately (arch §29).

        Raises:
            ValidationFailed: No handler is registered for this kind.
        """
        if kind not in self._handlers:
            raise ValidationFailed(f"No handler is registered for job kind {kind.value!r}.")

        context = current_request()
        job = Job(
            id=new_job_id(),
            kind=kind,
            integrations=integrations,
            parameters=parameters or {},
            requested_by=context.principal.subject or None,
            request_id=context.request_id,
        )
        self._live[job.id] = job
        await self._persist_new(job)
        await self._queue.put(job)
        log.info(
            "jobs.submitted",
            job=job.id,
            kind=kind.value,
            integrations=list(integrations),
        )
        return job

    # ------------------------------------------------------------------ status

    async def get(self, job_id: str) -> Job | None:
        """Look up a job, in memory first and then the database."""
        live = self._live.get(job_id)
        if live is not None:
            return live
        if self._sessions is None:
            return None
        async with self._sessions() as session:
            row = await session.get(JobRecord, job_id)
            if row is None:
                return None
            return Job(
                id=row.id,
                kind=JobKind(row.kind),
                integrations=tuple(row.integrations or ()),
                parameters=dict(row.parameters or {}),
                requested_by=row.requested_by,
                request_id=row.request_id,
                status=JobStatus(row.status),
                progress=row.progress,
                message=row.message,
                result=row.result,
                error=row.error,
            )

    async def recent(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """The most recent jobs, newest first."""
        if self._sessions is None:
            return [job.to_payload() for job in list(self._live.values())[-limit:]]
        async with self._sessions() as session:
            rows = await session.scalars(select(JobRecord).order_by(JobRecord.created_at.desc()).limit(limit))
            return [
                {
                    "id": row.id,
                    "kind": row.kind,
                    "status": row.status,
                    "integrations": list(row.integrations or ()),
                    "progress": row.progress,
                    "message": row.message,
                    "created_at": row.created_at.isoformat(),
                    "finished_at": row.finished_at.isoformat() if row.finished_at else None,
                }
                for row in rows
            ]

    # ----------------------------------------------------------------- workers

    async def _work(self, index: int) -> None:
        """Drain the queue until cancelled."""
        while True:
            job = await self._queue.get()
            try:
                await self._run(job)
            except asyncio.CancelledError:
                job.status = JobStatus.CANCELLED
                await self._persist_finished(job)
                raise
            except Exception as exc:  # noqa: BLE001 - a worker must outlive a bad job
                job.status = JobStatus.FAILED
                job.error = describe_exception(exc)
                log.error("jobs.worker_error", job=job.id, worker=index, error=job.error)
                await self._persist_finished(job)
            finally:
                self._queue.task_done()

    async def _run(self, job: Job) -> None:
        """Execute one job under the identity that submitted it."""
        handler = self._handlers[job.kind]
        job.status = JobStatus.RUNNING
        await self._persist_started(job)

        # Re-bind the requester's identity so the job's audit records and any
        # per-user credential resolution attribute to them, not to the worker.
        from app.core.context import ANONYMOUS, Principal

        principal = (
            Principal(subject=job.requested_by, scopes=frozenset({"admin"})) if job.requested_by else ANONYMOUS
        )
        context = RequestContext(
            request_id=job.id,
            principal=principal,
            trace_id=job.request_id,
            source="worker",
        )

        with bind_request(context):
            try:
                job.result = await handler(job)
                job.status = JobStatus.SUCCEEDED
                job.progress = 1.0
                log.info("jobs.succeeded", job=job.id, kind=job.kind.value)
            except HubError as exc:
                job.status = JobStatus.FAILED
                job.error = exc.message
                job.result = exc.to_payload()
                log.warning("jobs.failed", job=job.id, kind=job.kind.value, error=exc.message)
        await self._persist_finished(job)

    # -------------------------------------------------------------- persistence

    async def _persist_new(self, job: Job) -> None:
        if self._sessions is None:
            return
        async with self._sessions() as session, session.begin():
            session.add(
                JobRecord(
                    id=job.id,
                    kind=job.kind,
                    status=job.status,
                    integrations=list(job.integrations),
                    parameters=job.parameters,
                    requested_by=job.requested_by,
                    request_id=job.request_id,
                )
            )

    async def _persist_started(self, job: Job) -> None:
        if self._sessions is None:
            return
        async with self._sessions() as session, session.begin():
            await session.execute(
                update(JobRecord).where(JobRecord.id == job.id).values(status=job.status, started_at=utcnow())
            )

    async def _persist_finished(self, job: Job) -> None:
        if self._sessions is None:
            return
        async with self._sessions() as session, session.begin():
            await session.execute(
                update(JobRecord)
                .where(JobRecord.id == job.id)
                .values(
                    status=job.status,
                    result=job.result,
                    error=job.error,
                    progress=job.progress,
                    message=job.message,
                    finished_at=utcnow(),
                )
            )

    async def _fail_orphaned(self) -> None:
        """Mark jobs a previous process left `running` as failed.

        Without this a crashed update stays `running` forever and an operator has
        no way to tell a wedged job from a lost one.
        """
        if self._sessions is None:
            return
        async with self._sessions() as session, session.begin():
            result = await session.execute(
                update(JobRecord)
                .where(JobRecord.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]))
                .values(
                    status=JobStatus.FAILED,
                    error="The hub restarted while this job was in progress.",
                    finished_at=utcnow(),
                )
            )
        orphaned = int(cast("CursorResult[Any]", result).rowcount or 0)
        if orphaned:
            log.warning("jobs.orphaned_reconciled", count=orphaned)
