"""Time, injectable.

Every timestamp the hub records or compares goes through a `Clock`. Production
uses `SystemClock`; tests use `ManualClock` and advance it deliberately. That
keeps TTL, backoff, and idle-reaping tests exact instead of sleepy and flaky.

All times are timezone-aware UTC. A naive datetime anywhere in the hub is a bug:
lock expiry and version comparisons across a DST boundary are not worth the
convenience.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

__all__ = ["Clock", "ManualClock", "SystemClock", "utcnow"]


@runtime_checkable
class Clock(Protocol):
    """A source of time and of delay."""

    def now(self) -> datetime:
        """Current wall-clock time, timezone-aware UTC."""
        ...

    def monotonic(self) -> float:
        """Seconds from an arbitrary origin, never moving backwards.

        Used for durations and timeouts, which must survive an NTP correction.
        """
        ...

    async def sleep(self, seconds: float) -> None:
        """Yield for approximately `seconds`."""
        ...


class SystemClock:
    """The real clock."""

    __slots__ = ()

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        import time

        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        if seconds > 0:
            await asyncio.sleep(seconds)


class ManualClock:
    """A clock that only moves when told to.

    `sleep` returns immediately after advancing, so a test can exercise a
    reaper or a backoff loop in real time while the code under test believes
    hours passed.
    """

    __slots__ = ("_monotonic", "_now")

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)
        if self._now.tzinfo is None:
            raise ValueError("ManualClock requires a timezone-aware start time")
        self._monotonic = 0.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)
        await asyncio.sleep(0)  # let other tasks run, as a real sleep would

    def advance(self, seconds: float) -> None:
        """Move both clocks forward by `seconds`."""
        self._now += timedelta(seconds=seconds)
        self._monotonic += seconds


def utcnow() -> datetime:
    """Current UTC time, for the handful of places that cannot take a `Clock`.

    Prefer injecting a `Clock`. This exists for Pydantic/SQLAlchemy default
    factories, which are constructed before any clock is available.
    """
    return datetime.now(UTC)
