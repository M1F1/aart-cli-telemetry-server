"""An `EventRepository` in memory: the definitions in `domain.stats`, applied directly.

For tests, and as the oracle the SQL repository is held to.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from returns.io import IOResult, IOSuccess

from aart_cli_telemetry_server.application.ports import StorageError
from aart_cli_telemetry_server.domain import artifacts, stats
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

    def top_failures(
        self, period: stats.Window, limit: int
    ) -> IOResult[tuple[stats.FailureUsage, ...], StorageError]:
        return IOSuccess(stats.top_failures(self._events.values(), period, limit))

    def daily(self, period: stats.Window) -> IOResult[tuple[stats.DailyCount, ...], StorageError]:
        return IOSuccess(stats.daily(self._events.values(), period))

    def by_source(
        self, period: stats.Window
    ) -> IOResult[tuple[stats.SourceUsage, ...], StorageError]:
        return IOSuccess(stats.by_source(self._events.values(), period))

    def tallies(
        self, selection: artifacts.Selection
    ) -> IOResult[tuple[artifacts.Tally, ...], StorageError]:
        return IOSuccess(artifacts.tallies(self._events.values(), selection))

    def reach(
        self, selection: artifacts.Selection
    ) -> IOResult[tuple[artifacts.Reach, ...], StorageError]:
        return IOSuccess(artifacts.reach(self._events.values(), selection))

    def holdings(
        self, selection: artifacts.Selection
    ) -> IOResult[tuple[artifacts.Holding, ...], StorageError]:
        return IOSuccess(artifacts.holdings(self._events.values(), selection))

    def recent(
        self, selection: artifacts.Selection, limit: int, before: tuple[datetime, str] | None
    ) -> IOResult[tuple[artifacts.EventView, ...], StorageError]:
        return IOSuccess(artifacts.recent(self._events.values(), selection, limit, before))

    def client_versions(
        self, selection: artifacts.Selection
    ) -> IOResult[tuple[artifacts.ClientVersion, ...], StorageError]:
        return IOSuccess(artifacts.client_versions(self._events.values(), selection))

    def breakdown(
        self,
        selection: artifacts.Selection,
        by: tuple[artifacts.Dimension, ...],
        limit: int,
    ) -> IOResult[tuple[artifacts.Group, ...], StorageError]:
        return IOSuccess(artifacts.breakdown(self._events.values(), selection, by, limit))
