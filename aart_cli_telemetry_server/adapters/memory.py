"""An `EventRepository` in memory: the definitions in `domain.stats`, applied directly.

For tests, and as the oracle the SQL repository is held to.
"""

from __future__ import annotations

from collections.abc import Sequence

from returns.io import IOResult, IOSuccess

from aart_cli_telemetry_server.application.ports import StorageError
from aart_cli_telemetry_server.domain import stats
from aart_cli_telemetry_server.domain.events import StoredEvent

__all__ = ["InMemoryEventRepository"]


class InMemoryEventRepository:
    def __init__(self) -> None:
        self._events: dict[str, StoredEvent] = {}

    @property
    def events(self) -> tuple[StoredEvent, ...]:
        return tuple(self._events.values())

    def add(self, events: Sequence[StoredEvent]) -> IOResult[None, StorageError]:
        for stored in events:
            self._events.setdefault(stored.event_id, stored)
        return IOSuccess(None)

    def ping(self) -> IOResult[None, StorageError]:
        return IOSuccess(None)

    def summary(self, period: stats.Window) -> IOResult[stats.Summary, StorageError]:
        return IOSuccess(stats.summarise(self._events.values(), period))

    def top_artifacts(
        self, period: stats.Window, limit: int
    ) -> IOResult[tuple[stats.ArtifactUsage, ...], StorageError]:
        return IOSuccess(stats.top_artifacts(self._events.values(), period, limit))

    def top_unmanaged(
        self, period: stats.Window, limit: int
    ) -> IOResult[tuple[stats.UnmanagedUsage, ...], StorageError]:
        return IOSuccess(stats.top_unmanaged(self._events.values(), period, limit))

    def daily(self, period: stats.Window) -> IOResult[tuple[stats.DailyCount, ...], StorageError]:
        return IOSuccess(stats.daily(self._events.values(), period))

    def by_source(
        self, period: stats.Window
    ) -> IOResult[tuple[stats.SourceUsage, ...], StorageError]:
        return IOSuccess(stats.by_source(self._events.values(), period))
