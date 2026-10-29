"""Events as the server keeps them (docs/telemetry-api-v1.md)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum

__all__ = [
    "SOURCES_FOR",
    "Batch",
    "Event",
    "EventType",
    "Source",
    "StoredEvent",
    "acknowledged",
    "stamp",
    "stored_events",
]


class EventType(StrEnum):
    INSTALLED = "artifact.installed"
    UPDATED = "artifact.updated"
    REMOVED = "artifact.removed"
    USED = "artifact.used"


class Source(StrEnum):
    AART_CLI = "aart_cli"
    CLAUDE_HOOK = "claude_hook"
    OPENCODE_PLUGIN = "opencode_plugin"


#: Which source may report which event: aart-cli changes what is installed, a harness uses it.
SOURCES_FOR: Mapping[EventType, frozenset[Source]] = {
    EventType.INSTALLED: frozenset({Source.AART_CLI}),
    EventType.UPDATED: frozenset({Source.AART_CLI}),
    EventType.REMOVED: frozenset({Source.AART_CLI}),
    EventType.USED: frozenset({Source.CLAUDE_HOOK, Source.OPENCODE_PLUGIN}),
}


def _utc(moment: datetime) -> None:
    if moment.tzinfo is None or moment.utcoffset() != UTC.utcoffset(None):
        raise ValueError("a moment is kept in UTC")


@dataclass(frozen=True, slots=True)
class Event:
    """One event a client reported."""

    event_id: str
    occurred_at: datetime
    event_type: EventType
    source: Source
    artifact_id: str
    artifact_type: str
    artifact_version: str | None

    def __post_init__(self) -> None:
        _utc(self.occurred_at)
        if self.source not in SOURCES_FOR[self.event_type]:
            raise ValueError("this source cannot report this event type")


@dataclass(frozen=True, slots=True)
class Batch:
    """One accepted request: whose installation, and what it reported."""

    client_id: str
    client_version: str
    events: tuple[Event, ...]


@dataclass(frozen=True, slots=True)
class StoredEvent:
    """An event with what the server knows about its delivery; what a repository keeps."""

    event: Event
    client_id: str
    client_version: str
    received_at: datetime

    def __post_init__(self) -> None:
        _utc(self.received_at)

    @property
    def event_id(self) -> str:
        return self.event.event_id

    @property
    def occurred_day(self) -> date:
        """The UTC calendar day the event happened on: the dashboard's daily bucket."""

        return self.event.occurred_at.date()


def stored_events(batch: Batch, received_at: datetime) -> tuple[StoredEvent, ...]:
    """What `batch` adds, once per id: the first of an id seen twice in one request wins."""

    seen: dict[str, StoredEvent] = {}
    for event in batch.events:
        seen.setdefault(
            event.event_id, StoredEvent(event, batch.client_id, batch.client_version, received_at)
        )
    return tuple(seen.values())


def acknowledged(batch: Batch) -> tuple[str, ...]:
    """Every id `batch` carried, in order, once: stored now or before, each is safe to forget."""

    return tuple(dict.fromkeys(event.event_id for event in batch.events))


def stamp(moment: datetime) -> str:
    """A moment as the API writes one: UTC, microseconds, `Z`."""

    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
