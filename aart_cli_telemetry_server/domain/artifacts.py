"""What the read API says about artifacts, the events behind them and the aart-cli versions sending.

Pure. A repository answers five narrow questions -- `tallies`, `reach`, `holdings`, `recent` and
`client_versions` -- and must answer each exactly as it is defined here
(`tests/test_repository_contract.py`). Everything shown about an artifact is then composed from
those answers by `artifact_rows`, `ranked` and `artifact_detail`, the same way for every
repository.

A plugin's skill is an artifact of its own, named `<plugin>:<skill>` (aart-cli §214): its
installs, updates, removals and uses are its own, and its plugin's row also shows the uses of all
its members.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum

from .events import (
    HARNESS_OF,
    Context,
    Environment,
    EventType,
    Interface,
    Scope,
    Source,
    StoredEvent,
)

__all__ = [
    "EPOCH",
    "MANAGED",
    "MAX_DIMENSIONS",
    "MAX_EVENTS",
    "MAX_GROUPS",
    "ArtifactDetail",
    "ArtifactRow",
    "ClientVersion",
    "DayRow",
    "Dimension",
    "EventView",
    "Group",
    "Holding",
    "Order",
    "Reach",
    "Selection",
    "SourceRow",
    "Tally",
    "VersionRow",
    "artifact_detail",
    "artifact_rows",
    "breakdown",
    "check_breakdown",
    "client_versions",
    "dimension_value",
    "harness_of",
    "holdings",
    "member_of",
    "ordered_groups",
    "ordered_versions",
    "ranked",
    "reach",
    "recent",
    "tallies",
    "tally_key",
]

#: The events about an artifact aart-cli installed: the ones the read model counts.
MANAGED = frozenset({EventType.INSTALLED, EventType.UPDATED, EventType.REMOVED, EventType.USED})
_LIFECYCLE = frozenset({EventType.INSTALLED, EventType.UPDATED, EventType.REMOVED})
_HELD = frozenset({EventType.INSTALLED, EventType.UPDATED})
#: The most events one page lists.
MAX_EVENTS = 500
#: Before every event: a selection since it holds everything ever kept.
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class Selection:
    """Which events count.

    Those at or after `since` and before `until`, of the `event_type`, `source`, `client_version`,
    automation, harness, scope, Registry and interface asked for. With `artifact_id`, those about
    that artifact, of `artifact_type` when given; with `members` as well, those about its members,
    the skills named `<artifact_id>:<member>`. Without `artifact_id`, those of `artifact_type` when
    given.
    """

    since: datetime
    until: datetime | None = None
    event_type: EventType | None = None
    source: Source | None = None
    client_version: str | None = None
    #: Only automation (`True`), only people (`False`), or both.
    automated: bool | None = None
    artifact_type: str | None = None
    artifact_id: str | None = None
    members: bool = False
    #: The harness, as `harness_of` names it.
    harness: str | None = None
    scope: Scope | None = None
    registry: str | None = None
    interface: Interface | None = None

    def __post_init__(self) -> None:
        for moment in (self.since, self.until):
            if moment is not None and moment.tzinfo is None:
                raise ValueError("a selection's moments are aware")
        if self.members and self.artifact_id is None:
            raise ValueError("members are selected with the artifact they belong to")

    def holds(self, stored: StoredEvent) -> bool:
        event = stored.event
        return (
            event.occurred_at >= self.since
            and (self.until is None or event.occurred_at < self.until)
            and (self.event_type is None or event.event_type is self.event_type)
            and (self.source is None or event.source is self.source)
            and (self.client_version is None or stored.client_version == self.client_version)
            and (self.automated is None or stored.automated is self.automated)
            and (self.harness is None or harness_of(stored) == self.harness)
            and (self.scope is None or event.context.scope is self.scope)
            and (self.registry is None or event.context.registry == self.registry)
            and (self.interface is None or event.context.interface is self.interface)
            and self._about(event.artifact_type, event.artifact_id)
        )

    def _about(self, kind: str, name: str) -> bool:
        if self.artifact_id is None:
            return self.artifact_type is None or kind == self.artifact_type
        own = name == self.artifact_id and (
            self.artifact_type is None or kind == self.artifact_type
        )
        return own or (self.members and _member(kind, name, self.artifact_id))


def harness_of(stored: StoredEvent) -> str | None:
    """The harness an event names, or, for a use that names none, the one its source is."""

    event = stored.event
    return event.context.harness or HARNESS_OF.get(event.source)


def member_of(artifact_type: str, artifact_id: str) -> str | None:
    """The plugin a skill named `<plugin>:<member>` belongs to; `None` for anything else."""

    plugin, separator, member = artifact_id.partition(":")
    return plugin if artifact_type == "skill" and separator and plugin and member else None


def _member(kind: str, name: str, plugin: str) -> bool:
    return kind == "skill" and name.startswith(f"{plugin}:") and name != f"{plugin}:"


# -- what a repository answers ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Tally:
    """How many events of one type were about one version of one artifact, from one source, on
    one UTC day, and when the first and last of them happened."""

    artifact_type: str
    artifact_id: str
    artifact_version: str | None
    event_type: EventType
    source: Source
    day: date
    events: int
    first_seen: datetime
    last_seen: datetime


def tally_key(tally: Tally) -> tuple[object, ...]:
    return (
        tally.artifact_type,
        tally.artifact_id,
        tally.artifact_version is not None,
        tally.artifact_version or "",
        tally.event_type.value,
        tally.source.value,
        tally.day,
    )


def tallies(events: Iterable[StoredEvent], selection: Selection) -> tuple[Tally, ...]:
    """The selected events about installed artifacts, counted per artifact, version, type, source
    and day; in that order, a missing version first."""

    grouped: dict[tuple[str, str, str | None, EventType, Source, date], list[datetime]] = {}
    for stored in events:
        event = stored.event
        if event.event_type in MANAGED and selection.holds(stored):
            key = (
                event.artifact_type,
                event.artifact_id,
                event.artifact_version,
                event.event_type,
                event.source,
                stored.occurred_day,
            )
            grouped.setdefault(key, []).append(event.occurred_at)
    found = (
        Tally(*key, len(moments), min(moments), max(moments)) for key, moments in grouped.items()
    )
    return tuple(sorted(found, key=tally_key))


@dataclass(frozen=True, slots=True)
class Reach:
    """How many installations sent an event of one type about one artifact."""

    artifact_type: str
    artifact_id: str
    event_type: EventType
    clients: int


def reach(events: Iterable[StoredEvent], selection: Selection) -> tuple[Reach, ...]:
    """Per artifact and type of the selected events about installed artifacts, the distinct
    installations that sent one; in artifact, then type order."""

    clients: dict[tuple[str, str, EventType], set[str]] = {}
    for stored in events:
        event = stored.event
        if event.event_type in MANAGED and selection.holds(stored):
            key = (event.artifact_type, event.artifact_id, event.event_type)
            clients.setdefault(key, set()).add(stored.client_id)
    return tuple(
        Reach(kind, name, event_type, len(held))
        for (kind, name, event_type), held in sorted(
            clients.items(), key=lambda item: (item[0][0], item[0][1], item[0][2].value)
        )
    )


@dataclass(frozen=True, slots=True)
class Holding:
    """How many installations hold an artifact: their last install or update of it among the
    selected events came after their last removal of it, or they never removed it."""

    artifact_type: str
    artifact_id: str
    holders: int


def holdings(events: Iterable[StoredEvent], selection: Selection) -> tuple[Holding, ...]:
    """Per artifact any selected install, update or removal is about, its holders; in artifact
    order. A removal at the very moment of an install wins."""

    last: dict[tuple[str, str], dict[str, list[datetime | None]]] = {}
    for stored in events:
        event = stored.event
        if event.event_type not in _LIFECYCLE or not selection.holds(stored):
            continue
        moments = last.setdefault((event.artifact_type, event.artifact_id), {}).setdefault(
            stored.client_id, [None, None]
        )
        slot = 0 if event.event_type in _HELD else 1
        current = moments[slot]
        if current is None or event.occurred_at > current:
            moments[slot] = event.occurred_at
    return tuple(
        Holding(
            kind,
            name,
            sum(
                1
                for held, removed in clients.values()
                if held is not None and (removed is None or held > removed)
            ),
        )
        for (kind, name), clients in sorted(last.items())
    )


@dataclass(frozen=True, slots=True)
class EventView:
    """One kept event as the API lists it; a failure's detail is the failures list's."""

    event_id: str
    occurred_at: datetime
    event_type: EventType
    source: Source
    artifact_type: str
    artifact_id: str
    artifact_version: str | None
    client_id: str
    client_version: str
    automated: bool
    context: Context = field(default_factory=Context)
    environment: Environment | None = None


def _newest_first(view: EventView) -> tuple[datetime, str]:
    return view.occurred_at, view.event_id


def recent(
    events: Iterable[StoredEvent],
    selection: Selection,
    limit: int,
    before: tuple[datetime, str] | None = None,
) -> tuple[EventView, ...]:
    """The newest `limit` selected events, newest first, then the greatest id; only those
    older than `before` -- a moment and an id -- when it is given."""

    if not 1 <= limit <= MAX_EVENTS:
        raise ValueError(f"a page is 1 to {MAX_EVENTS} events long")
    views = (
        EventView(
            stored.event_id,
            stored.event.occurred_at,
            stored.event.event_type,
            stored.event.source,
            stored.event.artifact_type,
            stored.event.artifact_id,
            stored.event.artifact_version,
            stored.client_id,
            stored.client_version,
            stored.automated,
            stored.event.context,
            stored.environment,
        )
        for stored in events
        if selection.holds(stored)
    )
    held = (view for view in views if before is None or _newest_first(view) < before)
    return tuple(sorted(held, key=_newest_first, reverse=True)[:limit])


@dataclass(frozen=True, slots=True)
class ClientVersion:
    """One aart-cli version: how many installations sent events from it, how many of those
    events, and when the last."""

    client_version: str
    clients: int
    events: int
    last_seen: datetime


def client_versions(
    events: Iterable[StoredEvent], selection: Selection
) -> tuple[ClientVersion, ...]:
    """Every version the selected events were sent from: most installations first, then the
    latest seen, then the version."""

    sent: dict[str, list[StoredEvent]] = {}
    for stored in events:
        if selection.holds(stored):
            sent.setdefault(stored.client_version, []).append(stored)
    return ordered_versions(
        ClientVersion(
            version,
            len({stored.client_id for stored in held}),
            len(held),
            max(stored.event.occurred_at for stored in held),
        )
        for version, held in sent.items()
    )


def ordered_versions(versions: Iterable[ClientVersion]) -> tuple[ClientVersion, ...]:
    """Most installations first, then the latest seen, then the version."""

    found = sorted(versions, key=lambda item: item.client_version)
    found.sort(key=lambda item: (item.clients, item.last_seen), reverse=True)
    return tuple(found)


# -- what is shown ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ArtifactRow:
    """One artifact: what happened to it, how many installations it reached, and what it is."""

    artifact_type: str
    artifact_id: str
    #: The plugin this skill is a member of, when it is one.
    plugin: str | None
    #: The version of its latest event that names one.
    latest_version: str | None
    installs: int
    updates: int
    removals: int
    uses: int
    #: For a plugin, uses of its skills, `<artifact_id>:<member>`; never part of `uses`.
    member_uses: int
    #: Installations that hold it now, as their installs, updates and removals say.
    holders: int
    installed_by: int
    used_by: int
    first_seen: datetime | None
    last_seen: datetime | None


def _row(
    kind: str,
    name: str,
    own: list[Tally],
    *,
    clients: dict[EventType, int],
    holders: int,
    member_uses: int,
) -> ArtifactRow:
    counted = Counter[EventType]()
    for tally in own:
        counted[tally.event_type] += tally.events
    versioned = [tally for tally in own if tally.artifact_version is not None]
    latest = (
        max(versioned, key=lambda tally: (tally.last_seen, tally.artifact_version or ""))
        if versioned
        else None
    )
    return ArtifactRow(
        artifact_type=kind,
        artifact_id=name,
        plugin=member_of(kind, name),
        latest_version=None if latest is None else latest.artifact_version,
        installs=counted[EventType.INSTALLED],
        updates=counted[EventType.UPDATED],
        removals=counted[EventType.REMOVED],
        uses=counted[EventType.USED],
        member_uses=member_uses,
        holders=holders,
        installed_by=clients.get(EventType.INSTALLED, 0),
        used_by=clients.get(EventType.USED, 0),
        first_seen=min((tally.first_seen for tally in own), default=None),
        last_seen=max((tally.last_seen for tally in own), default=None),
    )


def artifact_rows(
    counted: Iterable[Tally], reached: Iterable[Reach], held: Iterable[Holding]
) -> tuple[ArtifactRow, ...]:
    """A row for every artifact `counted` names, in artifact order."""

    own: dict[tuple[str, str], list[Tally]] = {}
    for tally in counted:
        own.setdefault((tally.artifact_type, tally.artifact_id), []).append(tally)
    clients: dict[tuple[str, str], dict[EventType, int]] = {}
    for item in reached:
        clients.setdefault((item.artifact_type, item.artifact_id), {})[item.event_type] = (
            item.clients
        )
    holders = {(item.artifact_type, item.artifact_id): item.holders for item in held}
    skill_uses = {
        name: sum(tally.events for tally in tallied if tally.event_type is EventType.USED)
        for (kind, name), tallied in own.items()
        if kind == "skill"
    }
    return tuple(
        _row(
            kind,
            name,
            tallied,
            clients=clients.get((kind, name), {}),
            holders=holders.get((kind, name), 0),
            member_uses=sum(
                uses for other, uses in skill_uses.items() if _member("skill", other, name)
            )
            if kind == "plugin"
            else 0,
        )
        for (kind, name), tallied in sorted(own.items())
    )


class Order(StrEnum):
    """What a list of artifacts is ordered by, most first; ties by name, then kind."""

    INSTALLS = "installs"
    USES = "uses"
    HOLDERS = "holders"
    RECENT = "recent"
    NAME = "name"


def ranked(
    rows: Iterable[ArtifactRow], order: Order, limit: int, search: str | None = None
) -> tuple[ArtifactRow, ...]:
    """The first `limit` of `rows` whose name holds `search`, ignoring case, ordered by `order`."""

    if limit < 1:
        raise ValueError("a list holds at least one artifact")
    needle = (search or "").casefold()
    kept = sorted(
        (row for row in rows if needle in row.artifact_id.casefold()),
        key=lambda row: (row.artifact_id, row.artifact_type),
    )
    if order is Order.INSTALLS:
        kept.sort(key=lambda row: (row.installs, row.uses + row.member_uses), reverse=True)
    elif order is Order.USES:
        kept.sort(key=lambda row: (row.uses + row.member_uses, row.installs), reverse=True)
    elif order is Order.HOLDERS:
        kept.sort(key=lambda row: (row.holders, row.installs), reverse=True)
    elif order is Order.RECENT:
        kept.sort(key=lambda row: row.last_seen or EPOCH, reverse=True)
    return tuple(kept[:limit])


@dataclass(frozen=True, slots=True)
class VersionRow:
    artifact_version: str | None
    installs: int
    updates: int
    removals: int
    uses: int
    last_seen: datetime


@dataclass(frozen=True, slots=True)
class DayRow:
    day: date
    installs: int
    updates: int
    removals: int
    uses: int


@dataclass(frozen=True, slots=True)
class SourceRow:
    """Events about the artifact one source sent: aart-cli changes it, a harness's hook uses it."""

    source: Source
    events: int


@dataclass(frozen=True, slots=True)
class ArtifactDetail:
    artifact: ArtifactRow
    #: Latest seen first.
    versions: tuple[VersionRow, ...]
    days: tuple[DayRow, ...]
    sources: tuple[SourceRow, ...]
    #: For a plugin, a row for each of its skills that something was said about, by name.
    members: tuple[ArtifactRow, ...]


def _counts(own: Iterable[Tally]) -> Counter[EventType]:
    counted = Counter[EventType]()
    for tally in own:
        counted[tally.event_type] += tally.events
    return counted


def artifact_detail(
    counted: Iterable[Tally],
    reached: Iterable[Reach],
    held: Iterable[Holding],
    artifact_type: str,
    artifact_id: str,
) -> ArtifactDetail | None:
    """The artifact and its members from answers selected for it with its members; `None` when
    nothing was said about either."""

    rows = artifact_rows(counted, reached, held)
    members = (
        tuple(row for row in rows if _member(row.artifact_type, row.artifact_id, artifact_id))
        if artifact_type == "plugin"
        else ()
    )
    found = [
        row for row in rows if (row.artifact_type, row.artifact_id) == (artifact_type, artifact_id)
    ]
    if not found and not members:
        return None
    own = [
        tally
        for tally in counted
        if (tally.artifact_type, tally.artifact_id) == (artifact_type, artifact_id)
    ]
    member_uses = sum(row.uses for row in members)
    artifact = (
        found[0]
        if found
        else _row(artifact_type, artifact_id, [], clients={}, holders=0, member_uses=member_uses)
    )
    by_version: dict[str | None, list[Tally]] = {}
    by_day: dict[date, list[Tally]] = {}
    by_source = Counter[Source]()
    for tally in own:
        by_version.setdefault(tally.artifact_version, []).append(tally)
        by_day.setdefault(tally.day, []).append(tally)
        by_source[tally.source] += tally.events
    versions = []
    for version, tallied in by_version.items():
        counts = _counts(tallied)
        versions.append(
            VersionRow(
                version,
                counts[EventType.INSTALLED],
                counts[EventType.UPDATED],
                counts[EventType.REMOVED],
                counts[EventType.USED],
                max(tally.last_seen for tally in tallied),
            )
        )
    versions.sort(key=lambda row: (row.artifact_version is not None, row.artifact_version or ""))
    versions.sort(key=lambda row: row.last_seen, reverse=True)
    days = tuple(
        DayRow(
            day,
            counts[EventType.INSTALLED],
            counts[EventType.UPDATED],
            counts[EventType.REMOVED],
            counts[EventType.USED],
        )
        for day, counts in ((day, _counts(tallied)) for day, tallied in sorted(by_day.items()))
    )
    sources = tuple(
        SourceRow(source, n)
        for source, n in sorted(by_source.items(), key=lambda item: (-item[1], item[0].value))
    )
    return ArtifactDetail(artifact, tuple(versions), days, sources, members)


class Dimension(StrEnum):
    """What a breakdown groups events by."""

    EVENT_TYPE = "event_type"
    SOURCE = "source"
    ARTIFACT_TYPE = "artifact_type"
    ARTIFACT_ID = "artifact_id"
    ARTIFACT_VERSION = "artifact_version"
    CLIENT_VERSION = "client_version"
    AUTOMATED = "automated"
    DAY = "day"
    HARNESS = "harness"
    SCOPE = "scope"
    REGISTRY = "registry"
    INTERFACE = "interface"
    VIA = "via"
    COLLECTION = "collection"
    PREVIOUS_VERSION = "previous_version"
    TOOL = "tool"
    OS = "os"
    ARCH = "arch"
    PYTHON = "python"


#: The most dimensions one breakdown groups by, and the most groups it lists.
MAX_DIMENSIONS = 3
MAX_GROUPS = 1000


def dimension_value(stored: StoredEvent, dimension: Dimension) -> str | None:
    """What `stored` is in `dimension`, as a string; `None` when it does not say."""

    event, context, environment = stored.event, stored.event.context, stored.environment
    match dimension:
        case Dimension.EVENT_TYPE:
            return event.event_type.value
        case Dimension.SOURCE:
            return event.source.value
        case Dimension.ARTIFACT_TYPE:
            return event.artifact_type
        case Dimension.ARTIFACT_ID:
            return event.artifact_id
        case Dimension.ARTIFACT_VERSION:
            return event.artifact_version
        case Dimension.CLIENT_VERSION:
            return stored.client_version
        case Dimension.AUTOMATED:
            return "true" if stored.automated else "false"
        case Dimension.DAY:
            return stored.occurred_day.isoformat()
        case Dimension.HARNESS:
            return harness_of(stored)
        case Dimension.OS | Dimension.ARCH | Dimension.PYTHON:
            return None if environment is None else str(getattr(environment, dimension.value))
    value = getattr(context, dimension.value)
    return None if value is None else str(value.value if isinstance(value, StrEnum) else value)


@dataclass(frozen=True, slots=True)
class Group:
    """The selected events that are `values` in a breakdown's dimensions, and how many
    installations sent them."""

    values: tuple[str | None, ...]
    events: int
    clients: int


def _group_order(group: Group) -> tuple[int, int, tuple[tuple[bool, str], ...]]:
    return (
        -group.events,
        -group.clients,
        tuple((value is not None, value or "") for value in group.values),
    )


def breakdown(
    events: Iterable[StoredEvent], selection: Selection, by: tuple[Dimension, ...], limit: int
) -> tuple[Group, ...]:
    """The selected events grouped by `by`: most events first, then most installations, then
    the values, a group that does not say before one that does; at most `limit`."""

    check_breakdown(by, limit)
    counted: Counter[tuple[str | None, ...]] = Counter()
    clients: dict[tuple[str | None, ...], set[str]] = {}
    for stored in events:
        if not selection.holds(stored):
            continue
        key = tuple(dimension_value(stored, dimension) for dimension in by)
        counted[key] += 1
        clients.setdefault(key, set()).add(stored.client_id)
    return ordered_groups((Group(key, n, len(clients[key])) for key, n in counted.items()), limit)


def ordered_groups(groups: Iterable[Group], limit: int) -> tuple[Group, ...]:
    """`groups` in a breakdown's order, at most `limit`: every repository orders them so."""

    return tuple(sorted(groups, key=_group_order)[:limit])


def check_breakdown(by: tuple[Dimension, ...], limit: int) -> None:
    """Refuse a breakdown no repository answers."""

    if not 1 <= len(by) <= MAX_DIMENSIONS or len(set(by)) != len(by):
        raise ValueError(f"a breakdown is by 1 to {MAX_DIMENSIONS} distinct dimensions")
    if not 1 <= limit <= MAX_GROUPS:
        raise ValueError(f"a breakdown lists 1 to {MAX_GROUPS} groups")
