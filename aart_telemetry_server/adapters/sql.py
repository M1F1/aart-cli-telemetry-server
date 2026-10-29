"""An `EventRepository` on SQLAlchemy Core: SQLite now, PostgreSQL by changing the URL.

Everything here is written once for both: the schema is SQLAlchemy metadata, an id already kept is
skipped with the dialect's `ON CONFLICT DO NOTHING`, moments are naive UTC in the database and
aware UTC everywhere else, and a day is a stored column rather than a dialect's date function.
What only SQLite has -- the file's mode, WAL and the online backup -- is set apart and asked for
only when the URL is SQLite's.

A database this server did not create, or created at another schema version, is refused and left
exactly as it is.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, TypeVar

from returns.io import IOFailure, IOResult, IOSuccess
from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Dialect,
    Engine,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    case,
    create_engine,
    distinct,
    event,
    func,
    inspect,
    select,
)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.types import TypeDecorator

from aart_telemetry_server.application.ports import StorageError
from aart_telemetry_server.domain.events import Event, EventType, Source, StoredEvent
from aart_telemetry_server.domain.stats import (
    ArtifactUsage,
    DailyCount,
    SourceUsage,
    Summary,
    Window,
)

__all__ = [
    "DATABASE",
    "SCHEMA_VERSION",
    "SqlEventRepository",
    "open_repository",
    "sqlite_url",
]

DATABASE = "telemetry.sqlite3"
SCHEMA_VERSION = 1
_BUSY_MILLISECONDS = 10_000

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


def sqlite_url(data_dir: str) -> str:
    return f"sqlite:///{Path(data_dir) / DATABASE}"


def _wait_when_busy(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _configure(connection: Any, _: Any) -> None:
        cursor = connection.cursor()
        cursor.execute(f"PRAGMA busy_timeout = {_BUSY_MILLISECONDS}")
        cursor.close()


def _wal(engine: Engine) -> None:
    """Readers never wait for the writer. Kept in the file, so set once, after the schema check."""

    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode = WAL")


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
        dialect = self._engine.dialect.name
        insert = postgresql.insert if dialect == "postgresql" else sqlite.insert

        def write() -> None:
            if not rows:
                return
            with self._engine.begin() as connection:
                connection.execute(
                    insert(events).on_conflict_do_nothing(index_elements=["event_id"]), rows
                )

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
            func.count(distinct(events.c.client_id)),
            func.count(distinct(case((used, events.c.client_id)))),
            func.count(
                distinct(case((used, events.c.artifact_type + "\x1f" + events.c.artifact_id)))
            ),
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

    def daily(self, period: Window) -> IOResult[tuple[DailyCount, ...], StorageError]:
        statement = (
            select(
                events.c.occurred_day,
                _count(EventType.USED),
                _count(EventType.INSTALLED),
                _count(EventType.UPDATED),
                _count(EventType.REMOVED),
                func.count(distinct(events.c.client_id)),
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


def _count(kind: EventType) -> Any:
    return func.sum(case((events.c.event_type == kind.value, 1), else_=0))


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


def _prepare(engine: Engine) -> None:
    """Create the schema in an empty database; refuse one at another version, changing nothing."""

    tables = set(inspect(engine).get_table_names())
    if not tables:
        with engine.begin() as connection:
            metadata.create_all(connection)
            connection.execute(schema_info.insert(), {"id": 1, "version": SCHEMA_VERSION})
        return
    if "schema_info" not in tables:
        raise _Refused("this database was not created by aart-telemetry-server")
    with engine.connect() as connection:
        version = connection.execute(select(schema_info.c.version)).scalar()
    if version != SCHEMA_VERSION:
        raise _Refused(
            f"this database has schema version {version}; this server reads {SCHEMA_VERSION}"
        )


class _Refused(Exception):
    pass


def open_repository(url: str) -> IOResult[SqlEventRepository, StorageError]:
    """The repository at `url`, its schema created when the database is empty."""

    try:
        backend = make_url(url).get_backend_name()
        if backend == "sqlite":
            _owner_only_file(url)
        engine = create_engine(url)
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
