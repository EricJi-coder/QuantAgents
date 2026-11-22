"""Time sources. Live runs use the system clock; replays and backtests use a settable one,
so every component that asks "what time is it" sees the same simulated instant."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class SimulatedClock:
    def __init__(self, start: datetime):
        self._now = start if start.tzinfo else start.replace(tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> None:
        self._now = value if value.tzinfo else value.replace(tzinfo=UTC)

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
