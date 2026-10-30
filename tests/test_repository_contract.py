"""Every `EventRepository` keeps an id once and counts as `domain.stats` defines.

The same tests run against each repository: in memory, SQLite, and PostgreSQL when
`AART_CLI_TELEMETRY_TEST_POSTGRES_URL` names a database this suite may empty (otherwise it is
skipped).
The aggregates are compared with the pure definitions over generated histories, so a repository
that groups, filters or orders differently from the definitions fails here, not on a dashboard.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TypeVar

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from returns.io import IOResult, IOSuccess
from sqlalchemy import delete

from aart_cli_telemetry_server.adapters.memory import InMemoryEventRepository
from aart_cli_telemetry_server.adapters.sql import (
    SCHEMA_VERSION,
    SqlEventRepository,
    events,
    open_repository,
    sqlite_url,
)
from aart_cli_telemetry_server.application.ports import EventRepository, StorageError
from aart_cli_telemetry_server.domain import stats
from aart_cli_telemetry_server.domain.events import StoredEvent
from aart_cli_telemetry_server.effects import perform

from .strategies import NOW, histories, numbered

POSTGRES = os.environ.get("AART_CLI_TELEMETRY_TEST_POSTGRES_URL", "").strip()


_T = TypeVar("_T")


def _value(result: IOResult[_T, StorageError]) -> _T:
    return perform(result).unwrap()


def _sql(url: str) -> SqlEventRepository:
    repository = _value(open_repository(url))
    with repository.engine.begin() as connection:
        connection.execute(delete(events))
    return repository


FACTORIES: dict[str, Callable[[], EventRepository]] = {
    "memory": InMemoryEventRepository,
    "sqlite": lambda: _sql("sqlite://"),
}
if POSTGRES:
    FACTORIES["postgresql"] = lambda: _sql(POSTGRES)


@pytest.fixture(params=sorted(FACTORIES))
def make(request: pytest.FixtureRequest) -> Callable[[], EventRepository]:
    return FACTORIES[request.param]


def _oracle(history: list[StoredEvent]) -> InMemoryEventRepository:
    oracle = InMemoryEventRepository()
    oracle.add(history)
    return oracle


def _same_numbers(repository: EventRepository, oracle: EventRepository, days: int) -> None:
    period = stats.window(days, NOW)
    reads: tuple[Callable[[EventRepository], IOResult[object, StorageError]], ...] = (
        lambda r: r.summary(period),
        lambda r: r.daily(period),
        lambda r: r.by_source(period),
        lambda r: r.top_artifacts(period, 5),
        lambda r: r.top_artifacts(period, stats.MAX_TOP),
        lambda r: r.top_unmanaged(period, 5),
        lambda r: r.top_unmanaged(period, stats.MAX_TOP),
    )
    for read in reads:
        assert _value(read(repository)) == _value(read(oracle))


@settings(
    max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(history=histories, days=st.integers(1, 45), split=st.integers(0, 40))
def test_every_aggregate_agrees_with_the_definitions(
    make: Callable[[], EventRepository], history: list[StoredEvent], days: int, split: int
) -> None:
    repository = make()
    assert repository.add(history[:split]) == IOSuccess(None)
    assert repository.add(history[split:]) == IOSuccess(None)

    _same_numbers(repository, _oracle(history), days)


@settings(
    max_examples=30, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(history=histories, repeats=st.integers(2, 3))
def test_an_id_kept_before_is_kept_once_and_unchanged(
    make: Callable[[], EventRepository], history: list[StoredEvent], repeats: int
) -> None:
    repository = make()
    for _ in range(repeats):
        repository.add(history)
    changed = [
        StoredEvent(stored.event, stored.client_id, "9.9.9", stored.received_at)
        for stored in history
    ]
    repository.add(changed)

    _same_numbers(repository, _oracle(history), 45)


def test_nothing_to_add_and_ping(make: Callable[[], EventRepository]) -> None:
    repository = make()
    assert repository.add([]) == IOSuccess(None)
    assert repository.ping() == IOSuccess(None)
    assert _value(repository.summary(stats.window(1, NOW))) == stats.Summary(0, 0, 0, 0, 0, 0, 0, 0)


# -- SQLite on disk --------------------------------------------------------------------------------


@pytest.fixture
def on_disk(tmp_path: Path) -> Iterator[tuple[SqlEventRepository, Path]]:
    data = tmp_path / "data"
    repository = _value(open_repository(sqlite_url(str(data))))
    yield repository, data / "telemetry.sqlite3"
    repository.engine.dispose()


def test_the_database_is_the_owner_s_alone_and_wal(
    on_disk: tuple[SqlEventRepository, Path],
) -> None:
    repository, path = on_disk
    repository.add([])

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("SELECT version FROM schema_info").fetchone()[0] == SCHEMA_VERSION


@pytest.mark.parametrize(
    "setup",
    [
        "CREATE TABLE schema_info (id INTEGER PRIMARY KEY, version INTEGER NOT NULL);"
        "INSERT INTO schema_info VALUES (1, 9);",
        "CREATE TABLE notes (body TEXT);",
    ],
)
def test_a_database_of_another_schema_or_owner_is_refused_and_left(
    tmp_path: Path, setup: str
) -> None:
    path = tmp_path / "telemetry.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(setup)
    before = path.read_bytes()

    refused = perform(open_repository(sqlite_url(str(tmp_path))))
    assert (
        "schema version 9" in refused.failure().reason or "not created" in refused.failure().reason
    )
    assert path.read_bytes() == before


def test_a_database_that_cannot_be_opened_is_a_storage_error(tmp_path: Path) -> None:
    (tmp_path / "telemetry.sqlite3").write_bytes(b"not a database at all, not even close....")
    refused = perform(open_repository(sqlite_url(str(tmp_path))))
    assert refused.failure() == StorageError("DatabaseError")


def test_a_failing_database_is_a_value_not_an_exception(
    on_disk: tuple[SqlEventRepository, Path],
) -> None:
    repository, path = on_disk
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE event")
    repository.engine.dispose()
    period = stats.window(1, NOW)
    for result in (
        repository.ping(),
        repository.add([numbered(1)]),
        repository.summary(period),
        repository.daily(period),
        repository.by_source(period),
        repository.top_artifacts(period, 3),
    ):
        assert perform(result).failure() == StorageError("OperationalError")


def test_concurrent_writers_lose_nothing(on_disk: tuple[SqlEventRepository, Path]) -> None:
    repository, _ = on_disk
    batches = [[numbered(n % 50) for n in range(b, b + 5)] for b in range(0, 80, 5)]
    distinct = {stored.event_id: stored for batch in batches for stored in batch}
    outcomes: list[object] = []
    threads = [
        threading.Thread(target=lambda b=batch: outcomes.append(repository.add(b)))
        for batch in batches
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert outcomes == [IOSuccess(None)] * len(batches)
    _same_numbers(repository, _oracle(list(distinct.values())), 45)


def test_backup_is_a_consistent_readable_owner_only_copy(
    on_disk: tuple[SqlEventRepository, Path], tmp_path: Path
) -> None:
    repository, _ = on_disk
    repository.add([numbered(n) for n in range(3)])

    copied = Path(_value(repository.backup(str(tmp_path / "backup" / "copy.sqlite3"))))

    assert stat.S_IMODE(copied.stat().st_mode) == 0o600
    with sqlite3.connect(copied) as connection:
        assert connection.execute("SELECT COUNT(*) FROM event").fetchone()[0] == 3


def test_only_sqlite_is_backed_up_here() -> None:
    repository = _sql("sqlite://")
    object.__setattr__(repository.engine.dialect, "name", "postgresql")
    try:
        refused = perform(repository.backup("/nonexistent/copy"))
    finally:
        object.__setattr__(repository.engine.dialect, "name", "sqlite")
    assert refused.failure() == StorageError("only a SQLite database is backed up here")
