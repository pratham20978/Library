"""Rate limiting (arch §23, §28).

A token bucket per (scope, key). Buckets refill continuously rather than resetting
on a boundary, so a caller cannot save up a whole window's quota and spend it in
one burst against an upstream's own limits.

Two backends behind one interface. `RedisRateLimiter` is the real one — the check
runs as a Lua script so read-modify-write is atomic across every hub replica,
which is the only way a shared limit means anything with more than one worker.
`InMemoryRateLimiter` is the single-process fallback; it is exact within one
process and says so, because a limit that silently becomes per-replica is worse
than no limit at all.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from app.config.models import RateLimitSpec
from app.core.clock import Clock, SystemClock
from app.core.logging import get_logger

if TYPE_CHECKING:
    from redis.asyncio import Redis

__all__ = ["InMemoryRateLimiter", "RateLimitResult", "RateLimiter", "RedisRateLimiter", "build_rate_limiter"]

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    """The outcome of one rate-limit check."""

    allowed: bool
    remaining: float
    """Tokens left after this call. Fractional — buckets refill continuously."""

    retry_after_seconds: float
    """How long until one token is available. Zero when allowed."""

    limit: int
    """Bucket capacity, for the `X-RateLimit-Limit` header."""


@runtime_checkable
class RateLimiter(Protocol):
    """Consumes quota for a key, or refuses."""

    async def check(self, key: str, spec: RateLimitSpec, *, cost: float = 1.0) -> RateLimitResult:
        """Attempt to spend `cost` tokens against `key`.

        Consumes nothing when refusing, so a rejected call does not deepen the
        hole the caller is already in.
        """
        ...

    async def reset(self, key: str) -> None:
        """Drop a bucket, refilling it to capacity."""
        ...


# --------------------------------------------------------------------------- in-memory


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated_at: float


class InMemoryRateLimiter:
    """Process-local token buckets.

    Correct for a single worker and for tests. With several replicas each holds
    its own buckets, so the effective limit is multiplied by the replica count —
    configure Redis in any deployment where the limit matters.
    """

    def __init__(self, *, clock: Clock | None = None, max_buckets: int = 100_000) -> None:
        self._clock = clock or SystemClock()
        self._buckets: dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()
        self._max_buckets = max_buckets

    async def check(self, key: str, spec: RateLimitSpec, *, cost: float = 1.0) -> RateLimitResult:
        now = self._clock.monotonic()
        capacity = float(spec.capacity)
        refill = spec.refill_per_second
        async with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                if len(self._buckets) >= self._max_buckets:
                    self._evict(now, refill)
                bucket = _Bucket(tokens=capacity, updated_at=now)
                self._buckets[key] = bucket
            else:
                elapsed = max(0.0, now - bucket.updated_at)
                bucket.tokens = min(capacity, bucket.tokens + elapsed * refill)
                bucket.updated_at = now

            if bucket.tokens >= cost:
                bucket.tokens -= cost
                return RateLimitResult(
                    allowed=True, remaining=bucket.tokens, retry_after_seconds=0.0, limit=spec.capacity
                )
            deficit = cost - bucket.tokens
            return RateLimitResult(
                allowed=False,
                remaining=bucket.tokens,
                retry_after_seconds=deficit / refill if refill > 0 else float("inf"),
                limit=spec.capacity,
            )

    def _evict(self, now: float, refill: float) -> None:
        """Drop buckets that have refilled to capacity and are therefore inert.

        Bounds memory when keys are unbounded (one bucket per principal per tool).
        Removing a full bucket is lossless: recreating it yields the same state.
        """
        stale = [
            key
            for key, bucket in self._buckets.items()
            if refill > 0 and (now - bucket.updated_at) * refill >= bucket.tokens
        ]
        for key in stale[: max(1, len(self._buckets) // 4)]:
            del self._buckets[key]

    async def reset(self, key: str) -> None:
        async with self._lock:
            self._buckets.pop(key, None)


# --------------------------------------------------------------------------- redis

# Atomic refill-and-consume. Returning the same shape as the in-memory path keeps
# the two backends indistinguishable to callers.
#
# KEYS[1]  bucket key
# ARGV[1]  capacity   ARGV[2] refill/sec   ARGV[3] cost   ARGV[4] now (seconds, float)
# returns  {allowed, remaining_milli, retry_after_milli}
_LUA_BUCKET_SCRIPT = """
local capacity = tonumber(ARGV[1])
local refill   = tonumber(ARGV[2])
local cost     = tonumber(ARGV[3])
local now      = tonumber(ARGV[4])

local state    = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens   = tonumber(state[1])
local ts       = tonumber(state[2])

if tokens == nil then
  tokens = capacity
  ts = now
end

local elapsed = now - ts
if elapsed < 0 then elapsed = 0 end
tokens = math.min(capacity, tokens + elapsed * refill)

local allowed = 0
local retry = 0
if tokens >= cost then
  allowed = 1
  tokens = tokens - cost
else
  if refill > 0 then
    retry = (cost - tokens) / refill
  else
    retry = -1
  end
end

redis.call('HMSET', KEYS[1], 'tokens', tokens, 'ts', now)
-- Expire once a bucket would have refilled completely; it carries no state then.
local ttl = 60
if refill > 0 then ttl = math.ceil(capacity / refill) + 60 end
redis.call('EXPIRE', KEYS[1], ttl)

return {allowed, math.floor(tokens * 1000), math.floor(retry * 1000)}
"""


class RedisRateLimiter:
    """Token buckets shared across every hub replica (arch §28)."""

    def __init__(self, redis: Redis, *, clock: Clock | None = None, prefix: str = "mcp-hub:rl:") -> None:
        self._redis = redis
        self._clock = clock or SystemClock()
        self._prefix = prefix
        self._script = redis.register_script(_LUA_BUCKET_SCRIPT)

    async def check(self, key: str, spec: RateLimitSpec, *, cost: float = 1.0) -> RateLimitResult:
        import time

        try:
            raw = await self._script(
                keys=[f"{self._prefix}{key}"],
                args=[spec.capacity, spec.refill_per_second, cost, time.time()],
            )
        except Exception as exc:  # noqa: BLE001 - availability beats enforcement here
            # Failing closed would take the whole hub down with Redis. Arch §45
            # is explicit that the hub stays operational through dependency
            # failures, so the call is allowed and the gap is recorded loudly.
            log.error("ratelimit.redis_unavailable", error=str(exc), key=key)
            return RateLimitResult(allowed=True, remaining=0.0, retry_after_seconds=0.0, limit=spec.capacity)

        allowed, remaining_milli, retry_milli = (int(value) for value in raw)
        return RateLimitResult(
            allowed=bool(allowed),
            remaining=remaining_milli / 1000.0,
            retry_after_seconds=(retry_milli / 1000.0) if retry_milli >= 0 else float("inf"),
            limit=spec.capacity,
        )

    async def reset(self, key: str) -> None:
        with __import__("contextlib").suppress(Exception):
            await self._redis.delete(f"{self._prefix}{key}")


def build_rate_limiter(redis: Redis | None, *, clock: Clock | None = None) -> RateLimiter:
    """Pick the shared limiter when Redis is configured, else the local one."""
    if redis is not None:
        return RedisRateLimiter(redis, clock=clock)
    log.warning(
        "ratelimit.in_memory",
        reason="No Redis configured; rate limits apply per process, not per deployment.",
    )
    return InMemoryRateLimiter(clock=clock)
