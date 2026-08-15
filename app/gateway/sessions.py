"""Upstream MCP sessions, pooled (arch §30, §45).

Every tool call needs a live MCP session to an upstream. Opening one per call
would mean a fresh `initialize` handshake each time for remote servers, and a
fresh *process or container* each time for local ones — unusable. So sessions
are pooled and shared.

**What a session is keyed by is a security boundary, not an optimisation.** The
key is `(integration, credential identity)`, where the identity is a
non-reversible fingerprint of the resolved credentials mixed with the principal
(see `SecretManager._identity`). Two callers share a session only when they
present the same credentials as the same principal. Without that, one user's
authenticated session would serve another user's request — the exact failure
arch §20 exists to prevent.

**Why an owner task.** The MCP `Client` is an async context manager that runs a
background reader; it must be entered and exited in one task, but its request
methods are safe to call from others. So each session runs a small owner
coroutine that holds the `async with` open and parks, while callers issue
requests concurrently against it. The alternative — entering the context inside
whichever request arrived first — ties the session's lifetime to that one
request's cancellation.

**Failure is contained.** A session that fails to start or dies is removed and
surfaced as `IntegrationUnavailable` for that integration alone. Arch §45 is
explicit that one broken upstream must not take the hub down, so nothing here
propagates an upstream failure past the integration it belongs to.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import mcp.types as types

from app.core.clock import Clock, SystemClock
from app.core.errors import IntegrationUnavailable, UpstreamError, describe_exception
from app.core.logging import get_logger

if TYPE_CHECKING:
    from mcp.client._transport import Transport

__all__ = ["SessionKey", "SessionPool", "UpstreamSession"]

log = get_logger(__name__)

TransportFactory = Callable[[], Awaitable["Transport"]]
"""Builds a fresh transport. Called once per session start, never reused."""


@dataclass(frozen=True, slots=True)
class SessionKey:
    """Identifies a poolable session.

    `credential_identity` is a fingerprint, never credential material, so keys
    are safe to log and to use as dictionary keys.
    """

    integration_id: str
    credential_identity: str

    def __str__(self) -> str:
        return f"{self.integration_id}#{self.credential_identity}"


@dataclass(slots=True)
class _SessionStats:
    """Bookkeeping used by the reaper and by `mcp-hub status`."""

    created_at: float = 0.0
    last_used_at: float = 0.0
    calls: int = 0
    errors: int = 0


class UpstreamSession:
    """One live MCP session, shared by concurrent callers."""

    def __init__(
        self,
        key: SessionKey,
        factory: TransportFactory,
        *,
        request_timeout_seconds: float,
        connect_timeout_seconds: float,
        clock: Clock | None = None,
    ) -> None:
        self.key = key
        self._factory = factory
        self._request_timeout = request_timeout_seconds
        self._connect_timeout = connect_timeout_seconds
        self._clock = clock or SystemClock()

        self._client: Any | None = None
        self._owner: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._shutdown = asyncio.Event()
        self._error: BaseException | None = None
        self._refcount = 0
        self.stats = _SessionStats()

    # ------------------------------------------------------------- lifecycle

    @property
    def is_live(self) -> bool:
        """Whether the session is usable right now."""
        return self._client is not None and self._owner is not None and not self._owner.done()

    @property
    def in_use(self) -> bool:
        """Whether any caller currently holds this session."""
        return self._refcount > 0

    async def start(self) -> None:
        """Establish the session, or raise the reason it could not be.

        Idempotent: concurrent callers all wait on the same startup.

        Raises:
            IntegrationUnavailable: The upstream could not be reached, refused
                initialisation, or exceeded the connect budget.
        """
        if self.is_live:
            return
        if self._owner is None or self._owner.done():
            self._ready = asyncio.Event()
            self._shutdown = asyncio.Event()
            self._error = None
            self._owner = asyncio.create_task(self._run(), name=f"upstream-{self.key}")

        try:
            await asyncio.wait_for(self._ready.wait(), timeout=self._connect_timeout)
        except TimeoutError:
            await self.close()
            raise IntegrationUnavailable(
                f"Timed out after {self._connect_timeout:g}s establishing an MCP session with "
                f"{self.key.integration_id!r}.",
                integration=self.key.integration_id,
            ) from None

        if self._error is not None:
            error = self._error
            await self.close()
            raise IntegrationUnavailable(
                f"Could not establish an MCP session with {self.key.integration_id!r}: {error}",
                integration=self.key.integration_id,
            ) from error
        if self._client is None:  # pragma: no cover - ready implies one or the other
            raise IntegrationUnavailable(
                f"Session for {self.key.integration_id!r} became ready without a client.",
                integration=self.key.integration_id,
            )
        self.stats.created_at = self._clock.monotonic()
        self.stats.last_used_at = self.stats.created_at

    async def _run(self) -> None:
        """Own the client context for the session's whole life.

        Parks on `_shutdown` rather than returning, because leaving the `async
        with` is what closes the session.
        """
        from mcp.client.client import Client

        try:
            transport = await self._factory()
            async with Client(transport, read_timeout_seconds=self._request_timeout) as client:
                self._client = client
                self._ready.set()
                log.info(
                    "session.established",
                    integration=self.key.integration_id,
                    identity=self.key.credential_identity,
                    protocol=client.protocol_version,
                )
                await self._shutdown.wait()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - recorded and re-raised to the starter
            self._error = exc
            log.warning(
                "session.failed",
                integration=self.key.integration_id,
                error=describe_exception(exc),
            )
        finally:
            self._client = None
            self._ready.set()  # unblock anyone waiting on a session that will never come

    async def close(self) -> None:
        """Tear the session down, ending the upstream process or connection."""
        self._shutdown.set()
        owner = self._owner
        self._owner = None
        self._client = None
        if owner is None or owner.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(owner), timeout=10.0)
        except (TimeoutError, asyncio.CancelledError):
            owner.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await owner
        log.debug("session.closed", integration=self.key.integration_id)

    # ----------------------------------------------------------------- usage

    def acquire(self) -> None:
        """Mark the session as in use, so the reaper leaves it alone."""
        self._refcount += 1

    def release(self) -> None:
        """Release a hold and stamp the idle clock."""
        self._refcount = max(0, self._refcount - 1)
        self.stats.last_used_at = self._clock.monotonic()

    def idle_seconds(self) -> float:
        """How long since the last release. Infinite while held."""
        if self.in_use:
            return 0.0
        return self._clock.monotonic() - self.stats.last_used_at

    # ---------------------------------------------------------------- calls

    async def list_tools(self) -> Sequence[types.Tool]:
        """Enumerate the upstream's tools (arch §12).

        Raises:
            IntegrationUnavailable: The session is not usable.
            UpstreamError: The upstream refused or failed the request.
        """
        client = self._require_client()
        try:
            result = await client.list_tools(cache_mode="skip")
        except Exception as exc:
            self.stats.errors += 1
            raise self._wrap(exc, "tools/list") from exc
        return list(result.tools)

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> types.CallToolResult:
        """Invoke an upstream tool.

        Args:
            name: The *upstream* tool name, already stripped of its namespace.
            arguments: Arguments as the agent supplied them.
            timeout_seconds: Per-call budget, defaulting to the session's.

        Returns:
            The upstream's result, unmodified. A tool that failed reports it via
            `is_error`, which is a normal result and not an exception.

        Raises:
            IntegrationUnavailable: The session is not usable.
            UpstreamError: Transport or protocol failure, or the call timed out.
        """
        client = self._require_client()
        self.stats.calls += 1
        try:
            result = await client.call_tool(
                name,
                arguments,
                read_timeout_seconds=timeout_seconds or self._request_timeout,
            )
            assert isinstance(result, types.CallToolResult)
            return result
        except Exception as exc:
            self.stats.errors += 1
            raise self._wrap(exc, f"tools/call {name}") from exc

    async def ping(self) -> bool:
        """Cheap liveness probe used before reusing a pooled session."""
        client = self._client
        if client is None:
            return False
        try:
            await asyncio.wait_for(client.send_ping(), timeout=5.0)
        except Exception:  # noqa: BLE001 - any failure means "not usable"
            return False
        return True

    def _require_client(self) -> Any:
        if self._client is None or not self.is_live:
            raise IntegrationUnavailable(
                f"The MCP session for {self.key.integration_id!r} is not established.",
                integration=self.key.integration_id,
            )
        return self._client

    def _wrap(self, exc: Exception, operation: str) -> Exception:
        """Turn an SDK exception into a hub error the gateway can classify."""
        if isinstance(exc, TimeoutError | asyncio.TimeoutError):
            return UpstreamError(
                f"{self.key.integration_id!r} did not respond to {operation} within its timeout.",
                integration=self.key.integration_id,
                operation=operation,
            )
        return UpstreamError(
            f"{self.key.integration_id!r} failed {operation}: {describe_exception(exc)}",
            integration=self.key.integration_id,
            operation=operation,
        )


@dataclass(slots=True)
class _Lease:
    """A borrowed session, returned by the pool's context manager."""

    session: UpstreamSession
    pool: SessionPool = field(repr=False)


class SessionPool:
    """Keeps upstream sessions alive and hands them out safely."""

    def __init__(
        self,
        *,
        max_sessions: int = 64,
        idle_timeout_seconds: float = 300.0,
        clock: Clock | None = None,
    ) -> None:
        self._sessions: dict[SessionKey, UpstreamSession] = {}
        self._locks: dict[SessionKey, asyncio.Lock] = {}
        self._max_sessions = max_sessions
        self._idle_timeout = idle_timeout_seconds
        self._clock = clock or SystemClock()
        self._guard = asyncio.Lock()
        self._reaper: asyncio.Task[None] | None = None

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        """Begin reaping idle sessions."""
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(self._reap_forever(), name="session-reaper")

    async def stop(self) -> None:
        """Close every session and stop reaping."""
        if self._reaper is not None:
            self._reaper.cancel()
            with suppress(asyncio.CancelledError):
                await self._reaper
            self._reaper = None
        async with self._guard:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._locks.clear()
        for session in sessions:
            await session.close()
        log.info("session_pool.stopped", closed=len(sessions))

    # ---------------------------------------------------------------- access

    async def acquire(
        self,
        key: SessionKey,
        factory: TransportFactory,
        *,
        request_timeout_seconds: float,
        connect_timeout_seconds: float,
    ) -> UpstreamSession:
        """Return a live session for `key`, creating one if needed.

        Callers must `release` what they acquire; `session_for` does that
        automatically and should be preferred.

        Raises:
            IntegrationUnavailable: The session could not be established.
        """
        lock = await self._lock_for(key)
        async with lock:
            session = self._sessions.get(key)
            if session is not None and session.is_live:
                session.acquire()
                return session

            if session is not None:
                # Dead but still mapped: drop it before replacing, so a failed
                # upstream cannot accumulate zombie entries.
                await session.close()
                self._sessions.pop(key, None)

            await self._evict_if_full()

            session = UpstreamSession(
                key,
                factory,
                request_timeout_seconds=request_timeout_seconds,
                connect_timeout_seconds=connect_timeout_seconds,
                clock=self._clock,
            )
            try:
                await session.start()
            except BaseException:
                await session.close()
                raise
            self._sessions[key] = session
            session.acquire()
            return session

    def release(self, session: UpstreamSession) -> None:
        """Return a session to the pool."""
        session.release()

    async def invalidate(self, integration_id: str) -> int:
        """Close every session for one integration.

        Called when an integration is disabled, updated, removed, or has its
        credentials rotated — arch §18 requires disconnecting sessions before
        removal, and a promoted new version must not keep serving from the old
        process.
        """
        async with self._guard:
            doomed = [key for key in self._sessions if key.integration_id == integration_id]
            sessions = [self._sessions.pop(key) for key in doomed]
            for key in doomed:
                self._locks.pop(key, None)
        for session in sessions:
            await session.close()
        if sessions:
            log.info("session_pool.invalidated", integration=integration_id, closed=len(sessions))
        return len(sessions)

    def describe(self) -> list[dict[str, Any]]:
        """Pool contents for `mcp-hub status`. Identities are fingerprints."""
        return [
            {
                "integration": key.integration_id,
                "identity": key.credential_identity,
                "live": session.is_live,
                "in_use": session.in_use,
                "calls": session.stats.calls,
                "errors": session.stats.errors,
                "idle_seconds": round(session.idle_seconds(), 1),
            }
            for key, session in sorted(self._sessions.items(), key=lambda item: str(item[0]))
        ]

    # ---------------------------------------------------------------- internals

    async def _lock_for(self, key: SessionKey) -> asyncio.Lock:
        """One lock per key, so two callers cannot start the same session twice."""
        async with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock

    async def _evict_if_full(self) -> None:
        """Make room by closing the least recently used idle session.

        A pool at capacity with everything in use is left alone: refusing a call
        is better than tearing a session out from under a request in flight.
        """
        async with self._guard:
            if len(self._sessions) < self._max_sessions:
                return
            idle = [(key, session) for key, session in self._sessions.items() if not session.in_use]
            if not idle:
                log.warning("session_pool.full", size=len(self._sessions), limit=self._max_sessions)
                return
            key, session = max(idle, key=lambda item: item[1].idle_seconds())
            self._sessions.pop(key, None)
            self._locks.pop(key, None)
        await session.close()
        log.info("session_pool.evicted", integration=key.integration_id, reason="pool full")

    async def _reap_forever(self) -> None:
        """Close sessions that have been idle past the timeout."""
        interval = max(5.0, min(self._idle_timeout / 4, 60.0))
        while True:
            try:
                await asyncio.sleep(interval)
                await self._reap_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the reaper must never die
                log.error("session_pool.reap_failed", error=str(exc))

    async def _reap_once(self) -> None:
        async with self._guard:
            expired = [
                key
                for key, session in self._sessions.items()
                if not session.in_use and session.idle_seconds() > self._idle_timeout
            ]
            sessions = [self._sessions.pop(key) for key in expired]
            for key in expired:
                self._locks.pop(key, None)
        for key, session in zip(expired, sessions, strict=True):
            await session.close()
            log.debug("session_pool.reaped", integration=key.integration_id)
