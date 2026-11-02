"""Events as the server keeps them (docs/telemetry-api-v1.md)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum

__all__ = [
    "FAILURE_KINDS",
    "SOURCES_FOR",
    "Batch",
    "Event",
    "EventType",
    "Failure",
    "Source",
    "StoredEvent",
    "acknowledged",
    "fingerprint",
    "stamp",
    "stored_events",
]


class EventType(StrEnum):
    INSTALLED = "artifact.installed"
    UPDATED = "artifact.updated"
    REMOVED = "artifact.removed"
    USED = "artifact.used"
    #: A use of a skill or MCP no artifact aart-cli installed answers to, by the harness's name.
    UNMANAGED_USED = "unmanaged.used"
    #: A failure aart-cli met: its kind is the artifact_type, where it happened the artifact_id.
    FAILED = "aart_cli.failed"


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
    EventType.UNMANAGED_USED: frozenset({Source.CLAUDE_HOOK, Source.OPENCODE_PLUGIN}),
    EventType.FAILED: frozenset({Source.AART_CLI}),
}

#: What an `aart_cli.failed` event's artifact_type may be.
FAILURE_KINDS = frozenset({"crash", "error", "interrupted"})


def fingerprint(
    kind: str, place: str, exception: str | None, frames: Sequence[str], diagnostics: Sequence[str]
) -> str:
    """What names a defect: the first 16 hex digits of the SHA-256 of the compact JSON array
    `[kind, place, exception or "", frames without their line, diagnostics]`."""

    body = json.dumps(
        [
            kind,
            place,
            exception or "",
            [frame.rsplit(":", 1)[0] for frame in frames],
            list(diagnostics),
        ],
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class Failure:
    """What an `aart_cli.failed` event carries besides its kind and place."""

    fingerprint: str
    exception: str | None
    frames: tuple[str, ...]
    diagnostics: tuple[str, ...]
    exit_code: int | None
    python: str
    platform: str


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
    failure: Failure | None = None

    def __post_init__(self) -> None:
        _utc(self.occurred_at)
        if self.source not in SOURCES_FOR[self.event_type]:
            raise ValueError("this source cannot report this event type")
        if self.event_type is EventType.UNMANAGED_USED and self.artifact_version is not None:
            raise ValueError("an unmanaged use has no version")
        failed = self.event_type is EventType.FAILED
        if failed != (self.failure is not None):
            raise ValueError("a failure is carried by an aart_cli.failed event and no other")
        if failed and (
            self.artifact_version is not None or self.artifact_type not in FAILURE_KINDS
        ):
            raise ValueError("a failure has a kind and no version")


@dataclass(frozen=True, slots=True)
class Batch:
    """One accepted request: whose installation, and what it reported."""

    client_id: str
    client_version: str
    events: tuple[Event, ...]
    #: The installation said it runs as automation, such as a CI job, rather than for a person.
    automated: bool = False


@dataclass(frozen=True, slots=True)
class StoredEvent:
    """An event with what the server knows about its delivery; what a repository keeps."""

    event: Event
    client_id: str
    client_version: str
    received_at: datetime
    #: As the request that delivered it said; an event kept before keeps what its first said.
    automated: bool = False

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
            event.event_id,
            StoredEvent(event, batch.client_id, batch.client_version, received_at, batch.automated),
        )
    return tuple(seen.values())


def acknowledged(batch: Batch) -> tuple[str, ...]:
    """Every id `batch` carried, in order, once: stored now or before, each is safe to forget."""

    return tuple(dict.fromkeys(event.event_id for event in batch.events))


def stamp(moment: datetime) -> str:
    """A moment as the API writes one: UTC, microseconds, `Z`."""

    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
