"""What the dashboard counts, as pure functions over stored events.

These are the definitions. A repository may compute them however its database does best, but it
must agree with these on every input: `tests/test_repository_contract.py` holds each repository to
them over generated events. The in-memory repository simply calls them.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from .events import Cause, EventType, Failure, Source, StoredEvent

__all__ = [
    "MAX_DAYS",
    "MAX_TOP",
    "ArtifactUsage",
    "DailyCount",
    "FailureUsage",
    "SourceUsage",
    "Summary",
    "UnmanagedUsage",
    "Window",
    "by_source",
    "daily",
    "summarise",
    "top_artifacts",
    "top_failures",
    "top_unmanaged",
    "window",
]

MAX_DAYS = 366
MAX_TOP = 200


@dataclass(frozen=True, slots=True)
class Window:
    """Events that happened at or after `since` count."""

    since: datetime

    def holds(self, stored: StoredEvent) -> bool:
        return stored.event.occurred_at >= self.since


def window(days: int, now: datetime) -> Window:
    """The last `days` days before `now`."""

    if not 1 <= days <= MAX_DAYS:
        raise ValueError(f"a period is 1 to {MAX_DAYS} days")
    return Window(now.astimezone(UTC) - timedelta(days=days))


@dataclass(frozen=True, slots=True)
class Summary:
    uses: int
    installs: int
    updates: int
    removals: int
    #: People's installations: those whose events were not sent as automation.
    active_clients: int
    #: People's installations that used an artifact.
    using_clients: int
    used_artifacts: int
    #: Uses of skills and MCPs aart-cli did not install; never part of `uses`.
    unmanaged_uses: int
    #: Failures aart-cli met (`aart_cli.failed`).
    failures: int
    #: Installations whose events were sent as automation, such as CI jobs; one that sent both
    #: kinds is counted in each.
    automated_clients: int


@dataclass(frozen=True, slots=True)
class ArtifactUsage:
    artifact_type: str
    artifact_id: str
    uses: int
    clients: int
    installs: int
    last_used: datetime


@dataclass(frozen=True, slots=True)
class UnmanagedUsage:
    """A skill or MCP aart-cli did not install, by the name the harness used: how often, by how
    many installations, and when last."""

    artifact_type: str
    artifact_id: str
    uses: int
    clients: int
    last_used: datetime


@dataclass(frozen=True, slots=True)
class FailureUsage:
    """One defect, by its fingerprint: what it is, as its latest occurrence says, how often, by
    how many installations, on which versions and platforms, and when first and last."""

    fingerprint: str
    kind: str
    place: str
    exception: str | None
    frames: tuple[str, ...]
    diagnostics: tuple[str, ...]
    occurrences: int
    clients: int
    versions: tuple[str, ...]
    platforms: tuple[str, ...]
    first_seen: datetime
    last_seen: datetime
    causes: tuple[Cause, ...] = ()


@dataclass(frozen=True, slots=True)
class DailyCount:
    day: date
    uses: int
    installs: int
    updates: int
    removals: int
    active_clients: int
    automated_clients: int


@dataclass(frozen=True, slots=True)
class SourceUsage:
    source: Source
    artifact_type: str
    uses: int


def _within(events: Iterable[StoredEvent], period: Window) -> list[StoredEvent]:
    return [stored for stored in events if period.holds(stored)]


def _artifact(stored: StoredEvent) -> tuple[str, str]:
    return stored.event.artifact_type, stored.event.artifact_id


def _people(held: Iterable[StoredEvent]) -> int:
    return len({stored.client_id for stored in held if not stored.automated})


def _automated(held: Iterable[StoredEvent]) -> int:
    return len({stored.client_id for stored in held if stored.automated})


def summarise(events: Iterable[StoredEvent], period: Window) -> Summary:
    held = _within(events, period)
    kinds = Counter(stored.event.event_type for stored in held)
    uses = [stored for stored in held if stored.event.event_type is EventType.USED]
    return Summary(
        uses=kinds[EventType.USED],
        installs=kinds[EventType.INSTALLED],
        updates=kinds[EventType.UPDATED],
        removals=kinds[EventType.REMOVED],
        active_clients=_people(held),
        using_clients=_people(uses),
        used_artifacts=len({_artifact(stored) for stored in uses}),
        unmanaged_uses=kinds[EventType.UNMANAGED_USED],
        failures=kinds[EventType.FAILED],
        automated_clients=_automated(held),
    )


def top_artifacts(
    events: Iterable[StoredEvent], period: Window, limit: int
) -> tuple[ArtifactUsage, ...]:
    """Artifacts by uses, then name, then kind; how many installations used and installed each."""

    if not 1 <= limit <= MAX_TOP:
        raise ValueError(f"a list is 1 to {MAX_TOP} artifacts long")
    held = _within(events, period)
    uses: dict[tuple[str, str], list[StoredEvent]] = {}
    installs: Counter[tuple[str, str]] = Counter()
    for stored in held:
        if stored.event.event_type is EventType.USED:
            uses.setdefault(_artifact(stored), []).append(stored)
        elif stored.event.event_type is EventType.INSTALLED:
            installs[_artifact(stored)] += 1
    ranked = sorted(uses.items(), key=lambda item: (-len(item[1]), item[0][1], item[0][0]))
    return tuple(
        ArtifactUsage(
            artifact_type=kind,
            artifact_id=name,
            uses=len(used),
            clients=len({stored.client_id for stored in used}),
            installs=installs[(kind, name)],
            last_used=max(stored.event.occurred_at for stored in used),
        )
        for (kind, name), used in ranked[:limit]
    )


def top_unmanaged(
    events: Iterable[StoredEvent], period: Window, limit: int
) -> tuple[UnmanagedUsage, ...]:
    """Tools aart-cli did not install by uses, then name, then kind; kept apart from artifacts."""

    if not 1 <= limit <= MAX_TOP:
        raise ValueError(f"a list is 1 to {MAX_TOP} tools long")
    uses: dict[tuple[str, str], list[StoredEvent]] = {}
    for stored in _within(events, period):
        if stored.event.event_type is EventType.UNMANAGED_USED:
            uses.setdefault(_artifact(stored), []).append(stored)
    ranked = sorted(uses.items(), key=lambda item: (-len(item[1]), item[0][1], item[0][0]))
    return tuple(
        UnmanagedUsage(
            artifact_type=kind,
            artifact_id=name,
            uses=len(used),
            clients=len({stored.client_id for stored in used}),
            last_used=max(stored.event.occurred_at for stored in used),
        )
        for (kind, name), used in ranked[:limit]
    )


def top_failures(
    events: Iterable[StoredEvent], period: Window, limit: int
) -> tuple[FailureUsage, ...]:
    """Defects by occurrences, then the latest first, then fingerprint. What a defect is -- its
    frames and its causes' frames with their lines -- is what its latest occurrence (then the
    greatest id) says."""

    if not 1 <= limit <= MAX_TOP:
        raise ValueError(f"a list is 1 to {MAX_TOP} failures long")
    seen: dict[str, list[tuple[StoredEvent, Failure]]] = {}
    for stored in _within(events, period):
        if stored.event.failure is not None:
            seen.setdefault(stored.event.failure.fingerprint, []).append(
                (stored, stored.event.failure)
            )
    latest = {
        name: max(held, key=lambda item: (item[0].event.occurred_at, item[0].event_id))
        for name, held in seen.items()
    }
    ranked = sorted(seen)
    ranked.sort(key=lambda name: (len(seen[name]), latest[name][0].event.occurred_at), reverse=True)
    return tuple(
        FailureUsage(
            fingerprint=name,
            kind=latest[name][0].event.artifact_type,
            place=latest[name][0].event.artifact_id,
            exception=latest[name][1].exception,
            frames=latest[name][1].frames,
            diagnostics=latest[name][1].diagnostics,
            occurrences=len(seen[name]),
            clients=len({stored.client_id for stored, _ in seen[name]}),
            versions=tuple(sorted({stored.client_version for stored, _ in seen[name]})),
            platforms=tuple(sorted({failure.platform for _, failure in seen[name]})),
            first_seen=min(stored.event.occurred_at for stored, _ in seen[name]),
            last_seen=latest[name][0].event.occurred_at,
            causes=latest[name][1].causes,
        )
        for name in ranked[:limit]
    )


def daily(events: Iterable[StoredEvent], period: Window) -> tuple[DailyCount, ...]:
    """Per UTC day that has any event: each kind's count, and the people's and the automated
    installations active that day."""

    days: dict[date, list[StoredEvent]] = {}
    for stored in _within(events, period):
        days.setdefault(stored.occurred_day, []).append(stored)

    def count(held: list[StoredEvent], kind: EventType) -> int:
        return sum(1 for stored in held if stored.event.event_type is kind)

    return tuple(
        DailyCount(
            day=day,
            uses=count(held, EventType.USED),
            installs=count(held, EventType.INSTALLED),
            updates=count(held, EventType.UPDATED),
            removals=count(held, EventType.REMOVED),
            active_clients=_people(held),
            automated_clients=_automated(held),
        )
        for day, held in sorted(days.items())
    )


def by_source(events: Iterable[StoredEvent], period: Window) -> tuple[SourceUsage, ...]:
    """Uses per harness and artifact kind, most first."""

    counted = Counter(
        (stored.event.source, stored.event.artifact_type)
        for stored in _within(events, period)
        if stored.event.event_type is EventType.USED
    )
    ranked = sorted(counted.items(), key=lambda item: (-item[1], item[0][0].value, item[0][1]))
    return tuple(SourceUsage(source, kind, uses) for (source, kind), uses in ranked)
