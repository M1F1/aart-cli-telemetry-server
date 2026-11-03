"""An `EventRepository` on SQLAlchemy Core: SQLite now, PostgreSQL by changing the URL.

Everything here is written once for both: the schema is SQLAlchemy metadata, an id already kept is
skipped with the dialect's `ON CONFLICT DO NOTHING`, moments are naive UTC in the database and
aware UTC everywhere else, and a day is a stored column rather than a dialect's date function.
What only SQLite has -- the file's mode, WAL and the online backup -- is set apart and asked for
only when the URL is SQLite's.

A database this server did not create, or created at another schema version, is refused and left
exactly as it is. A table schema version 1 gained later -- `failure`, for `aart_cli.failed`,
`automation`, for events sent as automation, and `failure_cause`, for what a failure's exception
was raised from -- is created in a version-1 database that does not have it yet, and nothing else
in it changes. Servers
starting together on one database check and create the schema one at a time, and a connection the
database dropped -- a restart, a failover, an idle timeout -- is replaced before it is used.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, TypeVar

from returns.io import IOFailure, IOResult, IOSuccess
from sqlalchemy import (
    Column,
    Connection,
    Date,
    DateTime,
    Dialect,
    Engine,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    case,
    create_engine,
    distinct,
    event,
    exists,
    func,
    inspect,
    select,
)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.types import TypeDecorator

from aart_cli_telemetry_server.application.ports import StorageError
from aart_cli_telemetry_server.domain.artifacts import (
    MANAGED,
    MAX_EVENTS,
    ClientVersion,
    EventView,
    Holding,
    Reach,
    Selection,
    Tally,
    ordered_versions,
    tally_key,
)
from aart_cli_telemetry_server.domain.events import (
    Cause,
    Event,
    EventType,
    Failure,
    Source,
    StoredEvent,
)
from aart_cli_telemetry_server.domain.stats import (
    ArtifactUsage,
    DailyCount,
    FailureUsage,
    SourceUsage,
    Summary,
    UnmanagedUsage,
    Window,
)

__all__ = [
    "DATABASE",
    "SCHEMA_VERSION",
    "SqlEventRepository",
    "automation",
    "failure_causes",
    "failures",
    "open_repository",
    "sqlite_url",
]

DATABASE = "telemetry.sqlite3"
SCHEMA_VERSION = 1
_BUSY_MILLISECONDS = 10_000
_WAL_RETRY_SECONDS = 0.01
#: The PostgreSQL advisory lock each server starting on a database holds while it checks and
#: creates the schema. Any fixed number does; this one is "aart".
_SCHEMA_LOCK = 0x61617274

_T = TypeVar("_T")


class _UtcDateTime(TypeDecorator[datetime]):
    """Aware UTC in Python, naive UTC in the database, whichever database it is."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return None if value is None else value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return None if value is None else value.replace(tzinfo=UTC)


metadata = MetaData()

schema_info = Table(
    "schema_info",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column("version", Integer, nullable=False),
)

events = Table(
    "event",
    metadata,
    Column("event_id", String(36), primary_key=True),
    Column("client_id", String(36), nullable=False),
    Column("client_version", String(64), nullable=False),
    Column("occurred_at", _UtcDateTime(), nullable=False),
    Column("occurred_day", Date(), nullable=False),
    Column("received_at", _UtcDateTime(), nullable=False),
    Column("event_type", String(32), nullable=False),
    Column("source", String(32), nullable=False),
    Column("artifact_id", String(200), nullable=False),
    Column("artifact_type", String(64), nullable=False),
    Column("artifact_version", String(64), nullable=True),
    Index("event_moment", "occurred_at"),
    Index("event_artifact", "event_type", "artifact_type", "artifact_id"),
)

#: What an `aart_cli.failed` event carries besides its kind and place, which are the event's.
failures = Table(
    "failure",
    metadata,
    Column("event_id", String(36), ForeignKey("event.event_id"), primary_key=True),
    Column("fingerprint", String(16), nullable=False),
    Column("exception", String(128), nullable=True),
    Column("frames", Text(), nullable=False),
    Column("diagnostics", Text(), nullable=False),
    Column("exit_code", Integer(), nullable=True),
    Column("python", String(32), nullable=False),
    Column("platform", String(8), nullable=False),
    Index("failure_fingerprint", "fingerprint"),
)

#: What a failure's exception was raised from, or while handling, in order: a JSON list of
#: `{"exception", "frames"}`. A failure without causes has no row.
failure_causes = Table(
    "failure_cause",
    metadata,
    Column("event_id", String(36), ForeignKey("event.event_id"), primary_key=True),
    Column("causes", Text(), nullable=False),
)

#: The events a request sent as automation, such as a CI job; every other event is a person's.
automation = Table(
    "automation",
    metadata,
    Column("event_id", String(36), ForeignKey("event.event_id"), primary_key=True),
)
#: Whether the event of the row at hand was sent as automation.
_automated = exists().where(automation.c.event_id == events.c.event_id)


def sqlite_url(data_dir: str) -> str:
    return f"sqlite:///{Path(data_dir) / DATABASE}"


def _wait_when_busy(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _configure(connection: Any, _: Any) -> None:
        cursor = connection.cursor()
        cursor.execute(f"PRAGMA busy_timeout = {_BUSY_MILLISECONDS}")
        cursor.close()


def _wal(engine: Engine) -> None:
    """Readers never wait for the writer. Kept in the file, so set once, after the schema check.

    Servers switching a new file together can find it locked without waiting, where SQLite will
    not wait because waiting could deadlock; such a server tries again, a little later, until one
    of them has switched it or the busy timeout has passed.
    """

    deadline = time.monotonic() + _BUSY_MILLISECONDS / 1000
    while True:
        try:
            with engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA journal_mode = WAL")
        except OperationalError as error:
            if "locked" not in str(error.orig) or time.monotonic() >= deadline:
                raise
            time.sleep(_WAL_RETRY_SECONDS)
        else:
            return


def _owner_only_file(url: str) -> None:
    """The SQLite file and its directory exist, the owner's alone, before anything opens them."""

    database = make_url(url).database
    if not database or database == ":memory:":
        return
    path = Path(database)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.close(os.open(path, os.O_RDWR | os.O_CREAT, 0o600))


def _storage_error(error: Exception) -> StorageError:
    return StorageError(type(error).__name__)


def _guarded(action: Callable[[], _T]) -> IOResult[_T, StorageError]:
    try:
        return IOSuccess(action())
    except SQLAlchemyError as error:
        return IOFailure(_storage_error(error))


class SqlEventRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @property
    def engine(self) -> Engine:
        return self._engine

    def _by_name(self, column: Any) -> Any:
        """Names ordered by code point, as `domain.stats` orders them.

        SQLite compares text bytewise already; PostgreSQL uses the database's collation unless told.
        """

        return column.collate("C") if self._engine.dialect.name == "postgresql" else column

    # -- writing ------------------------------------------------------------------------------

    def add(self, stored: Sequence[StoredEvent]) -> IOResult[None, StorageError]:
        rows = [_row(item) for item in stored]
        failed = [_failure_row(item) for item in stored if item.event.failure is not None]
        caused = [
            _causes_row(item) for item in stored if item.event.failure and item.event.failure.causes
        ]
        automated = {item.event_id for item in stored if item.automated}
        dialect = self._engine.dialect.name
        insert = postgresql.insert if dialect == "postgresql" else sqlite.insert

        def write() -> None:
            if not rows:
                return
            with self._engine.begin() as connection:
                added: list[str] = list(
                    connection.execute(
                        insert(events)
                        .on_conflict_do_nothing(index_elements=["event_id"])
                        .returning(events.c.event_id),
                        rows,
                    ).scalars()
                )
                # Only an event this request added is marked: an id kept before keeps what the
                # request that first sent it said.
                marked = [{"event_id": name} for name in added if name in automated]
                # An id kept before keeps its event; a failure row is read only with an
                # aart_cli.failed event of the same id, so one written beside another kind of
                # event is never counted.
                if failed:
                    connection.execute(
                        insert(failures).on_conflict_do_nothing(index_elements=["event_id"]),
                        failed,
                    )
                if caused:
                    connection.execute(
                        insert(failure_causes).on_conflict_do_nothing(index_elements=["event_id"]),
                        caused,
                    )
                if marked:
                    connection.execute(automation.insert(), marked)

        return _guarded(write)

    def ping(self) -> IOResult[None, StorageError]:
        def read() -> None:
            with self._engine.connect() as connection:
                connection.execute(select(events.c.event_id).limit(1)).all()

        return _guarded(read)

    # -- aggregates -----------------------------------------------------------------------------

    def _rows(self, statement: Any) -> IOResult[list[Any], StorageError]:
        def read() -> list[Any]:
            with self._engine.connect() as connection:
                return list(connection.execute(statement).all())

        return _guarded(read)

    def summary(self, period: Window) -> IOResult[Summary, StorageError]:
        used = events.c.event_type == EventType.USED.value
        statement = select(
            _count(EventType.USED),
            _count(EventType.INSTALLED),
            _count(EventType.UPDATED),
            _count(EventType.REMOVED),
            _clients(automated=False),
            _clients(automated=False, held=used),
            func.count(
                distinct(case((used, events.c.artifact_type + "\x1f" + events.c.artifact_id)))
            ),
            _count(EventType.UNMANAGED_USED),
            _count(EventType.FAILED),
            _clients(automated=True),
        ).where(events.c.occurred_at >= period.since)
        return self._rows(statement).map(lambda rows: Summary(*(int(n or 0) for n in rows[0])))

    def top_artifacts(
        self, period: Window, limit: int
    ) -> IOResult[tuple[ArtifactUsage, ...], StorageError]:
        installed = (
            select(
                events.c.artifact_type,
                events.c.artifact_id,
                func.count().label("installs"),
            )
            .where(events.c.occurred_at >= period.since)
            .where(events.c.event_type == EventType.INSTALLED.value)
            .group_by(events.c.artifact_type, events.c.artifact_id)
            .subquery()
        )
        uses = func.count().label("uses")
        used = (
            select(
                events.c.artifact_type,
                events.c.artifact_id,
                uses,
                func.count(distinct(events.c.client_id)),
                func.max(events.c.occurred_at),
            )
            .where(events.c.occurred_at >= period.since)
            .where(events.c.event_type == EventType.USED.value)
            .group_by(events.c.artifact_type, events.c.artifact_id)
            .subquery()
        )
        statement = (
            select(
                used.c.artifact_type,
                used.c.artifact_id,
                used.c.uses,
                used.c[3],
                func.coalesce(installed.c.installs, 0),
                used.c[4],
            )
            .select_from(
                used.outerjoin(
                    installed,
                    (installed.c.artifact_type == used.c.artifact_type)
                    & (installed.c.artifact_id == used.c.artifact_id),
                )
            )
            .order_by(
                used.c.uses.desc(),
                self._by_name(used.c.artifact_id),
                self._by_name(used.c.artifact_type),
            )
            .limit(limit)
        )
        return self._rows(statement).map(
            lambda rows: tuple(
                ArtifactUsage(kind, name, int(n), int(clients), int(installs), _aware(last))
                for kind, name, n, clients, installs, last in rows
            )
        )

    def top_unmanaged(
        self, period: Window, limit: int
    ) -> IOResult[tuple[UnmanagedUsage, ...], StorageError]:
        uses = func.count().label("uses")
        statement = (
            select(
                events.c.artifact_type,
                events.c.artifact_id,
                uses,
                func.count(distinct(events.c.client_id)),
                func.max(events.c.occurred_at),
            )
            .where(events.c.occurred_at >= period.since)
            .where(events.c.event_type == EventType.UNMANAGED_USED.value)
            .group_by(events.c.artifact_type, events.c.artifact_id)
            .order_by(
                uses.desc(),
                self._by_name(events.c.artifact_id),
                self._by_name(events.c.artifact_type),
            )
            .limit(limit)
        )
        return self._rows(statement).map(
            lambda rows: tuple(
                UnmanagedUsage(kind, name, int(n), int(clients), _aware(last))
                for kind, name, n, clients, last in rows
            )
        )

    def top_failures(
        self, period: Window, limit: int
    ) -> IOResult[tuple[FailureUsage, ...], StorageError]:
        joined = failures.join(events, events.c.event_id == failures.c.event_id)
        held = (events.c.occurred_at >= period.since) & (
            events.c.event_type == EventType.FAILED.value
        )
        occurrences = func.count().label("occurrences")
        last = func.max(events.c.occurred_at).label("last_seen")
        ranked = (
            select(failures.c.fingerprint, occurrences, last)
            .select_from(joined)
            .where(held)
            .group_by(failures.c.fingerprint)
            .order_by(occurrences.desc(), last.desc(), self._by_name(failures.c.fingerprint))
            .limit(limit)
        )

        def read() -> tuple[FailureUsage, ...]:
            with self._engine.connect() as connection:
                names = [str(row[0]) for row in connection.execute(ranked).all()]
                if not names:
                    return ()
                rows = connection.execute(
                    select(
                        failures.c.fingerprint,
                        events.c.event_id,
                        events.c.occurred_at,
                        events.c.client_id,
                        events.c.client_version,
                        events.c.artifact_type,
                        events.c.artifact_id,
                        failures.c.exception,
                        failures.c.frames,
                        failures.c.diagnostics,
                        failures.c.platform,
                        failure_causes.c.causes,
                    )
                    .select_from(
                        joined.outerjoin(
                            failure_causes, failure_causes.c.event_id == failures.c.event_id
                        )
                    )
                    .where(held & failures.c.fingerprint.in_(names))
                ).all()
            return tuple(
                _failure_usage(name, [row for row in rows if row[0] == name]) for name in names
            )

        return _guarded(read)

    def daily(self, period: Window) -> IOResult[tuple[DailyCount, ...], StorageError]:
        statement = (
            select(
                events.c.occurred_day,
                _count(EventType.USED),
                _count(EventType.INSTALLED),
                _count(EventType.UPDATED),
                _count(EventType.REMOVED),
                _clients(automated=False),
                _clients(automated=True),
            )
            .where(events.c.occurred_at >= period.since)
            .group_by(events.c.occurred_day)
            .order_by(events.c.occurred_day)
        )
        return self._rows(statement).map(
            lambda rows: tuple(
                DailyCount(_day(day), *(int(n or 0) for n in counts)) for day, *counts in rows
            )
        )

    def by_source(self, period: Window) -> IOResult[tuple[SourceUsage, ...], StorageError]:
        uses = func.count().label("uses")
        statement = (
            select(events.c.source, events.c.artifact_type, uses)
            .where(events.c.occurred_at >= period.since)
            .where(events.c.event_type == EventType.USED.value)
            .group_by(events.c.source, events.c.artifact_type)
            .order_by(
                uses.desc(), self._by_name(events.c.source), self._by_name(events.c.artifact_type)
            )
        )
        return self._rows(statement).map(
            lambda rows: tuple(
                SourceUsage(Source(source), kind, int(n)) for source, kind, n in rows
            )
        )

    # -- the read model -------------------------------------------------------------------------

    def tallies(self, selection: Selection) -> IOResult[tuple[Tally, ...], StorageError]:
        grouped = (
            events.c.artifact_type,
            events.c.artifact_id,
            events.c.artifact_version,
            events.c.event_type,
            events.c.source,
            events.c.occurred_day,
        )
        statement = (
            select(
                *grouped,
                func.count(),
                func.min(events.c.occurred_at),
                func.max(events.c.occurred_at),
            )
            .where(_selected(selection) & _of(MANAGED))
            .group_by(*grouped)
        )
        return self._rows(statement).map(
            lambda rows: tuple(
                sorted(
                    (
                        Tally(
                            kind,
                            name,
                            version,
                            EventType(event_type),
                            Source(source),
                            _day(day),
                            int(n),
                            _aware(first),
                            _aware(last),
                        )
                        for kind, name, version, event_type, source, day, n, first, last in rows
                    ),
                    key=tally_key,
                )
            )
        )

    def reach(self, selection: Selection) -> IOResult[tuple[Reach, ...], StorageError]:
        grouped = (events.c.artifact_type, events.c.artifact_id, events.c.event_type)
        statement = (
            select(*grouped, func.count(distinct(events.c.client_id)))
            .where(_selected(selection) & _of(MANAGED))
            .group_by(*grouped)
        )
        return self._rows(statement).map(
            lambda rows: tuple(
                sorted(
                    (
                        Reach(kind, name, EventType(event_type), int(n))
                        for kind, name, event_type, n in rows
                    ),
                    key=lambda item: (item.artifact_type, item.artifact_id, item.event_type.value),
                )
            )
        )

    def holdings(self, selection: Selection) -> IOResult[tuple[Holding, ...], StorageError]:
        held = func.max(case((_of(_HELD), events.c.occurred_at))).label("held")
        removed = func.max(
            case((events.c.event_type == EventType.REMOVED.value, events.c.occurred_at))
        ).label("removed")
        per_client = (
            select(events.c.artifact_type, events.c.artifact_id, held, removed)
            .where(_selected(selection) & _of(_LIFECYCLE))
            .group_by(events.c.artifact_type, events.c.artifact_id, events.c.client_id)
            .subquery()
        )
        holds = per_client.c.held.is_not(None) & (
            per_client.c.removed.is_(None) | (per_client.c.held > per_client.c.removed)
        )
        statement = select(
            per_client.c.artifact_type,
            per_client.c.artifact_id,
            func.sum(case((holds, 1), else_=0)),
        ).group_by(per_client.c.artifact_type, per_client.c.artifact_id)
        return self._rows(statement).map(
            lambda rows: tuple(
                sorted(
                    (Holding(kind, name, int(n or 0)) for kind, name, n in rows),
                    key=lambda item: (item.artifact_type, item.artifact_id),
                )
            )
        )

    def recent(
        self, selection: Selection, limit: int, before: tuple[datetime, str] | None
    ) -> IOResult[tuple[EventView, ...], StorageError]:
        if not 1 <= limit <= MAX_EVENTS:
            raise ValueError(f"a page is 1 to {MAX_EVENTS} events long")
        held = _selected(selection)
        if before is not None:
            moment, name = before
            held &= (events.c.occurred_at < moment) | (
                (events.c.occurred_at == moment) & (self._by_name(events.c.event_id) < name)
            )
        statement = (
            select(
                events.c.event_id,
                events.c.occurred_at,
                events.c.event_type,
                events.c.source,
                events.c.artifact_type,
                events.c.artifact_id,
                events.c.artifact_version,
                events.c.client_id,
                events.c.client_version,
                _automated,
            )
            .where(held)
            .order_by(events.c.occurred_at.desc(), self._by_name(events.c.event_id).desc())
            .limit(limit)
        )
        return self._rows(statement).map(
            lambda rows: tuple(
                EventView(
                    name,
                    _aware(moment),
                    EventType(event_type),
                    Source(source),
                    kind,
                    artifact,
                    version,
                    client,
                    client_version,
                    bool(automated),
                )
                for (
                    name,
                    moment,
                    event_type,
                    source,
                    kind,
                    artifact,
                    version,
                    client,
                    client_version,
                    automated,
                ) in rows
            )
        )

    def client_versions(
        self, selection: Selection
    ) -> IOResult[tuple[ClientVersion, ...], StorageError]:
        statement = (
            select(
                events.c.client_version,
                func.count(distinct(events.c.client_id)),
                func.count(),
                func.max(events.c.occurred_at),
            )
            .where(_selected(selection))
            .group_by(events.c.client_version)
        )
        return self._rows(statement).map(
            lambda rows: ordered_versions(
                ClientVersion(version, int(clients), int(n), _aware(last))
                for version, clients, n, last in rows
            )
        )

    # -- SQLite only ----------------------------------------------------------------------------

    def backup(self, target: str) -> IOResult[str, StorageError]:
        """A consistent copy of a SQLite database at `target`, taken while it is in use."""

        if self._engine.dialect.name != "sqlite":
            return IOFailure(StorageError("only a SQLite database is backed up here"))

        def copy() -> str:
            destination = Path(target)
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.close(os.open(destination, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600))
            source = self._engine.raw_connection()
            try:
                with sqlite3.connect(destination) as copied:
                    source.driver_connection.backup(copied)  # type: ignore[union-attr]
                copied.close()
            finally:
                source.close()
            return str(destination)

        return _guarded(copy)


_LIFECYCLE = frozenset({EventType.INSTALLED, EventType.UPDATED, EventType.REMOVED})
_HELD = frozenset({EventType.INSTALLED, EventType.UPDATED})


def _of(kinds: frozenset[EventType]) -> Any:
    return events.c.event_type.in_(sorted(kind.value for kind in kinds))


def _selected(selection: Selection) -> Any:
    """The events `selection` holds, as `domain.artifacts.Selection.holds` defines them."""

    held = events.c.occurred_at >= selection.since
    if selection.until is not None:
        held &= events.c.occurred_at < selection.until
    if selection.event_type is not None:
        held &= events.c.event_type == selection.event_type.value
    if selection.source is not None:
        held &= events.c.source == selection.source.value
    if selection.client_version is not None:
        held &= events.c.client_version == selection.client_version
    if selection.automated is not None:
        held &= _automated if selection.automated else ~_automated
    if selection.artifact_id is None:
        if selection.artifact_type is not None:
            held &= events.c.artifact_type == selection.artifact_type
        return held
    own = events.c.artifact_id == selection.artifact_id
    if selection.artifact_type is not None:
        own &= events.c.artifact_type == selection.artifact_type
    if selection.members:
        # A prefix compared as text, not LIKE: `_` is a wildcard there and SQLite's LIKE ignores
        # case.
        prefix = f"{selection.artifact_id}:"
        own |= (
            (events.c.artifact_type == "skill")
            & (func.substr(events.c.artifact_id, 1, len(prefix)) == prefix)
            & (func.length(events.c.artifact_id) > len(prefix))
        )
    return held & own


def _count(kind: EventType) -> Any:
    return func.sum(case((events.c.event_type == kind.value, 1), else_=0))


def _clients(*, automated: bool, held: Any = None) -> Any:
    """The distinct installations among the events `held` that were, or were not, automation."""

    sent = _automated if automated else ~_automated
    return func.count(distinct(case((sent if held is None else sent & held, events.c.client_id))))


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _day(value: date | str) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


def _row(stored: StoredEvent) -> dict[str, object]:
    happened: Event = stored.event
    return {
        "event_id": happened.event_id,
        "client_id": stored.client_id,
        "client_version": stored.client_version,
        "occurred_at": happened.occurred_at,
        "occurred_day": stored.occurred_day,
        "received_at": stored.received_at,
        "event_type": happened.event_type.value,
        "source": happened.source.value,
        "artifact_id": happened.artifact_id,
        "artifact_type": happened.artifact_type,
        "artifact_version": happened.artifact_version,
    }


def _failure_row(stored: StoredEvent) -> dict[str, object]:
    failure: Failure | None = stored.event.failure
    if failure is None:
        raise ValueError("only an aart_cli.failed event has a failure row")
    return {
        "event_id": stored.event_id,
        "fingerprint": failure.fingerprint,
        "exception": failure.exception,
        "frames": json.dumps(list(failure.frames)),
        "diagnostics": json.dumps(list(failure.diagnostics)),
        "exit_code": failure.exit_code,
        "python": failure.python,
        "platform": failure.platform,
    }


def _causes_row(stored: StoredEvent) -> dict[str, object]:
    failure: Failure | None = stored.event.failure
    if failure is None or not failure.causes:
        raise ValueError("only a failure with causes has a cause row")
    return {
        "event_id": stored.event_id,
        "causes": json.dumps(
            [
                {"exception": cause.exception, "frames": list(cause.frames)}
                for cause in failure.causes
            ]
        ),
    }


def _causes(stored: str | None) -> tuple[Cause, ...]:
    return tuple(
        Cause(str(cause["exception"]), tuple(cause["frames"]))
        for cause in json.loads(stored or "[]")
    )


def _failure_usage(name: str, rows: list[Any]) -> FailureUsage:
    """One defect from the rows of its occurrences, as `domain.stats.top_failures` defines it."""

    latest = max(rows, key=lambda row: (_aware(row[2]), row[1]))
    return FailureUsage(
        fingerprint=name,
        kind=latest[5],
        place=latest[6],
        exception=latest[7],
        frames=tuple(json.loads(latest[8])),
        diagnostics=tuple(json.loads(latest[9])),
        occurrences=len(rows),
        clients=len({row[3] for row in rows}),
        versions=tuple(sorted({row[4] for row in rows})),
        platforms=tuple(sorted({row[10] for row in rows})),
        first_seen=min(_aware(row[2]) for row in rows),
        last_seen=_aware(latest[2]),
        causes=_causes(latest[11]),
    )


def _prepare(engine: Engine) -> None:
    """Create the schema in an empty database; refuse one at another version, changing nothing.

    One transaction, holding the lock every server starting on the database takes, so replicas
    starting together do this one at a time: the first creates what is missing and the others
    find it there.
    """

    with engine.begin() as connection:
        _one_at_a_time(connection)
        tables = set(inspect(connection).get_table_names())
        if not tables:
            metadata.create_all(connection)
            connection.execute(schema_info.insert(), {"id": 1, "version": SCHEMA_VERSION})
            return
        if "schema_info" not in tables:
            raise _Refused("this database was not created by aart-cli-telemetry-server")
        version = connection.execute(select(schema_info.c.version)).scalar()
        if version != SCHEMA_VERSION:
            raise _Refused(
                f"this database has schema version {version}; this server reads {SCHEMA_VERSION}"
            )
        for later in (failures, automation, failure_causes):
            if later.name not in tables:
                later.create(connection)


def _one_at_a_time(connection: Connection) -> None:
    """Hold, until this transaction ends, the lock each server starting on the database takes."""

    if connection.dialect.name == "postgresql":
        connection.execute(select(func.pg_advisory_xact_lock(_SCHEMA_LOCK)))
    elif connection.dialect.name == "sqlite":
        # The write lock now rather than at the first write, so a second server waits here (up to
        # the busy timeout) instead of reading a schema the first is still creating.
        connection.exec_driver_sql("BEGIN IMMEDIATE")


class _Refused(Exception):
    pass


def open_repository(url: str) -> IOResult[SqlEventRepository, StorageError]:
    """The repository at `url`, its schema created when the database is empty."""

    try:
        backend = make_url(url).get_backend_name()
        if backend == "sqlite":
            _owner_only_file(url)
        # A pooled connection is checked before it is used, so the first request after the
        # database restarted is served rather than answered 503.
        engine = create_engine(url, pool_pre_ping=True)
        if backend == "sqlite":
            _wait_when_busy(engine)
        _prepare(engine)
        if backend == "sqlite":
            _wal(engine)
    except _Refused as refused:
        return IOFailure(StorageError(str(refused)))
    except (SQLAlchemyError, OSError) as error:
        return IOFailure(_storage_error(error))
    return IOSuccess(SqlEventRepository(engine))
