"""Distributed locks for lifecycle operations (arch §30).

Arch §30 is specific about the behaviour: two `update jira` commands must not
both run, and the loser must be told *which* job holds the lock rather than
silently queueing or failing.

    $ mcp-hub update jira
    Integration jira is currently locked by update job job_a1b2c3

Redis is the real implementation, and the primitive is `SET key value NX PX ttl`
with a token only the holder knows. Release is a Lua compare-and-delete, so a
process whose lock already expired cannot delete the lock its successor now
holds — the classic bug this pattern exists to avoid.

Every lock has a TTL. A worker that dies mid-update must not leave an
integration permanently unmanageable; the lock lapses and the next operation can
proceed. Long operations extend their lease rather than taking a longer one, so a
crash is noticed in seconds rather than at the end of a worst-case build.

Without Redis the locks are process-local. That is correct for a single-worker
deployment and stated plainly rather than silently pretended — a second replica
would not see them.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from app.core.errors import IntegrationLocked
from app.core.ids import new_token
from app.core.logging import get_logger

if TYPE_CHECKING:
    from redis.asyncio import Redis

__all__ = ["InMemoryLockManager", "LockHandle", "LockManager", "RedisLockManager", "build_lock_manager"]

log = get_logger(__name__)

_PREFIX = "mcp-hub:lock:"

# Compare-and-delete: only the holder whose token matches may release.
_LUA_RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""

# Compare-and-extend, for operations that outlive their initial lease.
_LUA_EXTEND = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""


@dataclass(frozen=True, slots=True)
class LockHandle:
    """Proof of holding a lock."""

    name: str
    token: str
    """Secret proving ownership. Never logged; a leak would allow a stolen release."""

    owner: str
    """Human-readable holder, e.g. a job id. Safe to show in an error."""


@runtime_checkable
class LockManager(Protocol):
    """Acquires and releases named locks."""

    async def acquire(self, name: str, *, owner: str, ttl_seconds: float) -> LockHandle:
        """Take the lock, or say who holds it.

        Raises:
            IntegrationLocked: Someone else holds it.
        """
        ...

    async def release(self, handle: LockHandle) -> bool:
        """Release a lock. Returns whether this holder still owned it."""
        ...

    async def extend(self, handle: LockHandle, *, ttl_seconds: float) -> bool:
        """Renew the lease. Returns whether the lock was still held."""
        ...

    async def owner_of(self, name: str) -> str | None:
        """Who holds the lock, or `None` if it is free."""
        ...


class InMemoryLockManager:
    """Process-local locks. Correct for one worker; invisible to other replicas."""

    def __init__(self) -> None:
        self._held: dict[str, tuple[str, str, float]] = {}  # name -> (token, owner, expires_at)
        self._guard = asyncio.Lock()

    async def acquire(self, name: str, *, owner: str, ttl_seconds: float) -> LockHandle:
        import time

        async with self._guard:
            now = time.monotonic()
            existing = self._held.get(name)
            if existing is not None and existing[2] > now:
                raise IntegrationLocked(
                    f"Integration {name} is currently locked by {existing[1]}",
                    integration=name,
                    holder=existing[1],
                )
            token = new_token(nbytes=16)
            self._held[name] = (token, owner, now + ttl_seconds)
            return LockHandle(name=name, token=token, owner=owner)

    async def release(self, handle: LockHandle) -> bool:
        async with self._guard:
            existing = self._held.get(handle.name)
            if existing is None or existing[0] != handle.token:
                return False
            del self._held[handle.name]
            return True

    async def extend(self, handle: LockHandle, *, ttl_seconds: float) -> bool:
        import time

        async with self._guard:
            existing = self._held.get(handle.name)
            if existing is None or existing[0] != handle.token:
                return False
            self._held[handle.name] = (existing[0], existing[1], time.monotonic() + ttl_seconds)
            return True

    async def owner_of(self, name: str) -> str | None:
        import time

        async with self._guard:
            existing = self._held.get(name)
            if existing is None or existing[2] <= time.monotonic():
                return None
            return existing[1]


class RedisLockManager:
    """Locks shared across every hub replica (arch §28, §30)."""

    def __init__(self, redis: Redis, *, prefix: str = _PREFIX) -> None:
        self._redis = redis
        self._prefix = prefix
        self._release = redis.register_script(_LUA_RELEASE)
        self._extend = redis.register_script(_LUA_EXTEND)

    def _key(self, name: str) -> str:
        return f"{self._prefix}{name}"

    async def acquire(self, name: str, *, owner: str, ttl_seconds: float) -> LockHandle:
        token = new_token(nbytes=16)
        # The stored value carries the owner so a waiter can be told who holds it,
        # while the token stays the part that authorises release.
        value = f"{token}:{owner}"
        acquired = await self._redis.set(self._key(name), value, nx=True, px=int(ttl_seconds * 1000))
        if not acquired:
            holder = await self.owner_of(name)
            raise IntegrationLocked(
                f"Integration {name} is currently locked by {holder or 'another operation'}",
                integration=name,
                holder=holder,
            )
        return LockHandle(name=name, token=value, owner=owner)

    async def release(self, handle: LockHandle) -> bool:
        result = await self._release(keys=[self._key(handle.name)], args=[handle.token])
        return bool(result)

    async def extend(self, handle: LockHandle, *, ttl_seconds: float) -> bool:
        result = await self._extend(keys=[self._key(handle.name)], args=[handle.token, int(ttl_seconds * 1000)])
        return bool(result)

    async def owner_of(self, name: str) -> str | None:
        raw = await self._redis.get(self._key(name))
        if raw is None:
            return None
        value = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        _, _, owner = value.partition(":")
        return owner or "another operation"


@asynccontextmanager
async def integration_lock(
    manager: LockManager,
    integration_id: str,
    *,
    owner: str,
    ttl_seconds: float,
) -> AsyncIterator[LockHandle]:
    """Hold an integration's lifecycle lock for the duration of the block.

    Always released, including on failure or cancellation, so a crashed update
    does not require waiting out the TTL in the common case.

    Raises:
        IntegrationLocked: Another operation holds the lock.
    """
    handle = await manager.acquire(integration_id, owner=owner, ttl_seconds=ttl_seconds)
    log.info("lock.acquired", integration=integration_id, owner=owner, ttl_seconds=ttl_seconds)
    try:
        yield handle
    finally:
        released = await manager.release(handle)
        log.info("lock.released", integration=integration_id, owner=owner, still_held=released)


def build_lock_manager(redis: Redis | None) -> LockManager:
    """Use Redis when configured; otherwise process-local locks."""
    if redis is not None:
        return RedisLockManager(redis)
    log.warning(
        "lock.in_memory",
        reason="No Redis configured; lifecycle locks are process-local and other replicas cannot see them.",
    )
    return InMemoryLockManager()
