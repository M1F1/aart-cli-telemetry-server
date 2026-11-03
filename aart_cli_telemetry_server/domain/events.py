"""Events as the server keeps them (docs/telemetry-api-v1.md)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields
from datetime import UTC, date, datetime
from enum import StrEnum

__all__ = [
    "CONTEXT_FOR",
    "FAILURE_KINDS",
    "HARNESS_OF",
    "SOURCES_FOR",
    "Batch",
    "Cause",
    "Context",
    "Environment",
    "Event",
    "EventType",
    "Failure",
    "Interface",
    "Scope",
    "Source",
    "StoredEvent",
    "Via",
    "acknowledged",
    "context_problem",
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


@dataclass(frozen=True, slots=True)
class Cause:
    """An exception a failure's exception was raised from, or while handling: its type name and
    its frames inside aart-cli, innermost last."""

    exception: str
    frames: tuple[str, ...]


def _lines_dropped(frames: Sequence[str]) -> list[str]:
    return [frame.rsplit(":", 1)[0] for frame in frames]


def fingerprint(
    kind: str,
    place: str,
    exception: str | None,
    frames: Sequence[str],
    diagnostics: Sequence[str],
    *,
    causes: Sequence[Cause] = (),
) -> str:
    """What names a defect: the first 16 hex digits of the SHA-256 of the compact JSON array
    `[kind, place, exception or "", frames without their line, diagnostics]`, followed, when the
    exception has causes, by `[[exception, frames without their line], ...]` in their order."""

    body: list[object] = [
        kind,
        place,
        exception or "",
        _lines_dropped(frames),
        list(diagnostics),
    ]
    if causes:
        body.append([[cause.exception, _lines_dropped(cause.frames)] for cause in causes])
    return hashlib.sha256(json.dumps(body, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]


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
    causes: tuple[Cause, ...] = ()


def _utc(moment: datetime) -> None:
    if moment.tzinfo is None or moment.utcoffset() != UTC.utcoffset(None):
        raise ValueError("a moment is kept in UTC")


class Scope(StrEnum):
    """Whose configuration an installation is in."""

    USER = "user"
    PROJECT = "project"


class Interface(StrEnum):
    """Where a person asked for an install, update or removal."""

    TUI = "tui"
    CLI = "cli"


class Via(StrEnum):
    """How an artifact came to be installed: asked for itself, in a Collection, or in a plugin."""

    DIRECT = "direct"
    COLLECTION = "collection"
    PLUGIN = "plugin"


#: The harness a use's source is, so a use that names its harness names that one.
HARNESS_OF: Mapping[Source, str] = {
    Source.CLAUDE_HOOK: "claude",
    Source.OPENCODE_PLUGIN: "opencode",
}

_LIFECYCLE = frozenset({EventType.INSTALLED, EventType.UPDATED, EventType.REMOVED})
_PLACED = frozenset({EventType.INSTALLED, EventType.UPDATED})
_USES = frozenset({EventType.USED, EventType.UNMANAGED_USED})


@dataclass(frozen=True, slots=True)
class Context:
    """Where and how an event happened (revision 7). Every field is optional; each is held by
    the event types `CONTEXT_FOR` names, and by no other."""

    harness: str | None = None
    scope: Scope | None = None
    registry: str | None = None
    interface: Interface | None = None
    previous_version: str | None = None
    via: Via | None = None
    collection: str | None = None
    collection_version: str | None = None
    package_digest: str | None = None
    duration_ms: int | None = None
    configuration_inputs: int | None = None
    credential_inputs: int | None = None
    tool: str | None = None
    session: str | None = None

    def given(self) -> tuple[str, ...]:
        """The names of the fields that hold something."""

        return tuple(item.name for item in fields(self) if getattr(self, item.name) is not None)


#: Which event types each context field may be given in.
CONTEXT_FOR: Mapping[str, frozenset[EventType]] = {
    "harness": _LIFECYCLE | _USES,
    "scope": _LIFECYCLE | {EventType.USED},
    "registry": _LIFECYCLE | {EventType.USED},
    "interface": _LIFECYCLE,
    "previous_version": frozenset({EventType.UPDATED}),
    "via": _LIFECYCLE,
    "collection": _LIFECYCLE,
    "collection_version": _LIFECYCLE,
    "package_digest": _PLACED,
    "duration_ms": _LIFECYCLE,
    "configuration_inputs": _PLACED,
    "credential_inputs": _PLACED,
    "tool": _USES,
    "session": _USES,
}


def context_problem(
    event_type: EventType, source: Source, artifact_type: str, context: Context
) -> str | None:
    """Why `context` cannot be this event's, in the contract's words; `None` when it can."""

    if any(event_type not in CONTEXT_FOR[name] for name in context.given()):
        return "this event_type does not hold that field"
    if (context.collection is not None or context.collection_version is not None) and (
        context.via is not Via.COLLECTION or context.collection is None
    ):
        return "a collection is named exactly when via is collection"
    if context.tool is not None and artifact_type != "mcp":
        return "a tool is an mcp's"
    harness = HARNESS_OF.get(source)
    if harness is not None and context.harness not in (None, harness):
        return "a use's harness is its source's"
    return None


@dataclass(frozen=True, slots=True)
class Environment:
    """What a request says it was sent from (revision 7): no host, user or path."""

    os: str
    arch: str
    python: str


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
    context: Context = field(default_factory=Context)

    def __post_init__(self) -> None:
        _utc(self.occurred_at)
        self._context_is_its_own()
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

    def _context_is_its_own(self) -> None:
        problem = context_problem(self.event_type, self.source, self.artifact_type, self.context)
        if problem is not None:
            raise ValueError(problem)


@dataclass(frozen=True, slots=True)
class Batch:
    """One accepted request: whose installation, and what it reported."""

    client_id: str
    client_version: str
    events: tuple[Event, ...]
    #: The installation said it runs as automation, such as a CI job, rather than for a person.
    automated: bool = False
    environment: Environment | None = None


@dataclass(frozen=True, slots=True)
class StoredEvent:
    """An event with what the server knows about its delivery; what a repository keeps."""

    event: Event
    client_id: str
    client_version: str
    received_at: datetime
    #: As the request that delivered it said; an event kept before keeps what its first said.
    automated: bool = False
    environment: Environment | None = None

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
            StoredEvent(
                event,
                batch.client_id,
                batch.client_version,
                received_at,
                batch.automated,
                batch.environment,
            ),
        )
    return tuple(seen.values())


def acknowledged(batch: Batch) -> tuple[str, ...]:
    """Every id `batch` carried, in order, once: stored now or before, each is safe to forget."""

    return tuple(dict.fromkeys(event.event_id for event in batch.events))


def stamp(moment: datetime) -> str:
    """A moment as the API writes one: UTC, microseconds, `Z`."""

    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
