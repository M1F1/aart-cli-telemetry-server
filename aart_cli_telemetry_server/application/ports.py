"""The ports the use cases need. `adapters` implements them; tests swap them."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from returns.io import IOResult

from aart_cli_telemetry_server.domain.events import StoredEvent
from aart_cli_telemetry_server.domain.stats import (
    ArtifactUsage,
    DailyCount,
    SourceUsage,
    Summary,
    Window,
)

__all__ = ["Clock", "EventRepository", "StorageError"]

#: The current moment, in UTC.
Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class StorageError:
    """The database could not do what was asked. The reason is the server's, never a client's."""

    reason: str


class EventRepository(Protocol):
    """Where events are kept, and the dashboard's aggregates over them.

    `add` keeps every event in one transaction, and an id already kept is kept once: that is what
    makes a client's retry after a lost answer harmless. The aggregates agree with
    `domain.stats` on every input.
    """

    def add(self, events: Sequence[StoredEvent]) -> IOResult[None, StorageError]: ...

    def ping(self) -> IOResult[None, StorageError]: ...

    def summary(self, period: Window) -> IOResult[Summary, StorageError]: ...

    def top_artifacts(
        self, period: Window, limit: int
    ) -> IOResult[tuple[ArtifactUsage, ...], StorageError]: ...

    def daily(self, period: Window) -> IOResult[tuple[DailyCount, ...], StorageError]: ...

    def by_source(self, period: Window) -> IOResult[tuple[SourceUsage, ...], StorageError]: ...
