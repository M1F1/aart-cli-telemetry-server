"""Every `EventRepository` keeps an id once and counts as `domain.stats` defines.

The same tests run against each repository: in memory, SQLite, and PostgreSQL when
`AART_CLI_TELEMETRY_TEST_POSTGRES_URL` names a database this suite may empty (otherwise it is
skipped).
The aggregates are compared with the pure definitions over generated histories, so a repository
that groups, filters or orders differently from the definitions fails here, not on a dashboard.
"""

from __future__ import annotations

import dataclasses
import os
import sqlite3
import stat
import threading
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import TypeVar

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from returns.io import IOResult, IOSuccess
from returns.pipeline import is_successful
from sqlalchemy import Table, create_engine, delete, inspect, select, text

from aart_cli_telemetry_server.adapters.memory import InMemoryEventRepository
from aart_cli_telemetry_server.adapters.sql import (
    EVENT_WIDE,
    SCHEMA_VERSION,
    SqlEventRepository,
    automation,
    event_context,
    events,
    failure_causes,
    failures,
    metadata,
    open_repository,
    schema_info,
    sqlite_url,
)
from aart_cli_telemetry_server.application.ports import EventRepository, StorageError
from aart_cli_telemetry_server.domain import artifacts, stats
from aart_cli_telemetry_server.domain.events import (
    Context,
    Environment,
    Event,
    EventType,
    Interface,
    Scope,
    Source,
    StoredEvent,
    Via,
)
from aart_cli_telemetry_server.effects import perform

from .strategies import CLIENTS, NOW, histories, numbered, selections

POSTGRES = os.environ.get("AART_CLI_TELEMETRY_TEST_POSTGRES_URL", "").strip()


_T = TypeVar("_T")


def _value(result: IOResult[_T, StorageError]) -> _T:
    return perform(result).unwrap()


def _sql(url: str) -> SqlEventRepository:
    repository = _value(open_repository(url))
    with repository.engine.begin() as connection:
        connection.execute(delete(failure_causes))
        connection.execute(delete(failures))
        connection.execute(delete(automation))
        connection.execute(delete(event_context))
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
        lambda r: r.top_failures(period, 3),
        lambda r: r.top_failures(period, stats.MAX_TOP),
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
        StoredEvent(
            stored.event, stored.client_id, "9.9.9", stored.received_at, not stored.automated
        )
        for stored in history
    ]
    repository.add(changed)

    _same_numbers(repository, _oracle(history), 45)


def test_nothing_to_add_and_ping(make: Callable[[], EventRepository]) -> None:
    repository = make()
    assert repository.add([]) == IOSuccess(None)
    assert repository.ping() == IOSuccess(None)
    assert _value(repository.summary(stats.window(1, NOW))) == stats.Summary(
        0, 0, 0, 0, 0, 0, 0, 0, 0, 0
    )


@settings(
    max_examples=80, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    history=histories,
    selection=selections(),
    limit=st.integers(1, artifacts.MAX_EVENTS),
    cursor=st.none() | st.integers(0, 39),
    by=st.lists(
        st.sampled_from(list(artifacts.Dimension)),
        min_size=1,
        max_size=artifacts.MAX_DIMENSIONS,
        unique=True,
    ).map(tuple),
    groups=st.integers(1, 6),
)
def test_every_read_model_answer_agrees_with_the_definitions(
    make: Callable[[], EventRepository],
    *,
    history: list[StoredEvent],
    selection: artifacts.Selection,
    limit: int,
    cursor: int | None,
    by: tuple[artifacts.Dimension, ...],
    groups: int,
) -> None:
    repository = make()
    repository.add(history)
    oracle = _oracle(history)
    before = None
    if cursor is not None and history:
        chosen = history[cursor % len(history)].event
        before = (chosen.occurred_at, chosen.event_id)
    reads: tuple[Callable[[EventRepository], IOResult[object, StorageError]], ...] = (
        lambda r: r.tallies(selection),
        lambda r: r.reach(selection),
        lambda r: r.holdings(selection),
        lambda r: r.recent(selection, limit, before),
        lambda r: r.recent(selection, 3, None),
        lambda r: r.client_versions(selection),
        lambda r: r.breakdown(selection, by, groups),
        lambda r: r.breakdown(selection, by, artifacts.MAX_GROUPS),
    )
    for read in reads:
        assert _value(read(repository)) == _value(read(oracle))


def _at(
    number: int,
    kind: EventType,
    name: str,
    moment: datetime,
    *,
    artifact_type: str = "skill",
    client: str = CLIENTS[0],
) -> StoredEvent:
    source = Source.OPENCODE_PLUGIN if kind is EventType.USED else Source.AART_CLI
    return StoredEvent(
        Event(
            f"00000000-0000-4000-8000-{number:012d}",
            moment,
            kind,
            source,
            name,
            artifact_type,
            "1.0.1",
        ),
        client,
        "0.24.0",
        NOW,
    )


#: Where a rule's edge is: an install and a removal at one moment, an event at `until`, a name
#: that only looks like a member, and a member of another kind.
_EDGES = [
    _at(1, EventType.INSTALLED, "superpowers", NOW, artifact_type="plugin"),
    _at(2, EventType.REMOVED, "superpowers", NOW, artifact_type="plugin"),
    _at(3, EventType.INSTALLED, "superpowers", NOW, artifact_type="plugin", client=CLIENTS[1]),
    _at(4, EventType.INSTALLED, "superpowers:tdd", NOW),
    _at(5, EventType.USED, "superpowers:tdd", NOW - timedelta(hours=1)),
    _at(6, EventType.USED, "superpowers:", NOW - timedelta(hours=1)),
    _at(7, EventType.USED, "superpowers:grep", NOW, artifact_type="mcp"),
    _at(8, EventType.USED, "super_powers:tdd", NOW),
    _at(9, EventType.USED, "superpowersX", NOW),
]


@pytest.mark.parametrize(
    "selection",
    [
        artifacts.Selection(artifacts.EPOCH),
        artifacts.Selection(artifacts.EPOCH, until=NOW),
        artifacts.Selection(artifacts.EPOCH, artifact_id="superpowers", members=True),
        artifacts.Selection(
            artifacts.EPOCH, artifact_type="plugin", artifact_id="superpowers", members=True
        ),
        artifacts.Selection(artifacts.EPOCH, artifact_id="super_powers", members=True),
    ],
)
def test_the_read_model_s_edges_agree_with_the_definitions(
    make: Callable[[], EventRepository], selection: artifacts.Selection
) -> None:
    repository = make()
    repository.add(_EDGES)
    oracle = _oracle(_EDGES)

    reads: tuple[Callable[[EventRepository], IOResult[object, StorageError]], ...] = (
        lambda r: r.tallies(selection),
        lambda r: r.reach(selection),
        lambda r: r.holdings(selection),
        lambda r: r.recent(selection, artifacts.MAX_EVENTS, None),
        lambda r: r.client_versions(selection),
    )
    for read in reads:
        assert _value(read(repository)) == _value(read(oracle))


def _situated(
    number: int,
    kind: EventType,
    source: Source,
    context: Context,
    *,
    artifact_type: str = "mcp",
    automated: bool = False,
    environment: Environment | None = None,
) -> StoredEvent:
    return StoredEvent(
        Event(
            f"00000000-0000-4000-8000-{number:012d}",
            NOW - timedelta(minutes=number),
            kind,
            source,
            "github",
            artifact_type,
            None if kind is EventType.UNMANAGED_USED else "1.0.0",
            context=context,
        ),
        CLIENTS[number % 2],
        "0.24.0",
        NOW,
        automated,
        environment,
    )


#: Where a context rule's edge is: a use that names no harness but has a source that is one, a
#: lifecycle event that names none, an event with an environment and no context, and automation.
_CONTEXTS = [
    _situated(
        1,
        EventType.INSTALLED,
        Source.AART_CLI,
        Context(
            harness="opencode",
            scope=Scope.USER,
            registry="company",
            interface=Interface.TUI,
            via=Via.COLLECTION,
            collection="starter-kit",
            package_digest="sha256:" + "ab" * 32,
            duration_ms=1200,
            configuration_inputs=2,
            credential_inputs=1,
        ),
    ),
    _situated(
        2,
        EventType.UPDATED,
        Source.AART_CLI,
        Context(harness="claude", scope=Scope.PROJECT, previous_version="0.9.0"),
        automated=True,
    ),
    _situated(3, EventType.REMOVED, Source.AART_CLI, Context()),
    _situated(
        4,
        EventType.USED,
        Source.CLAUDE_HOOK,
        Context(scope=Scope.USER, registry="company", tool="create_issue"),
        environment=Environment("darwin", "arm64", "3.11.9"),
    ),
    _situated(5, EventType.USED, Source.OPENCODE_PLUGIN, Context(harness="opencode")),
    _situated(
        6,
        EventType.UNMANAGED_USED,
        Source.CLAUDE_HOOK,
        Context(tool="search", session="5a1f0c2e-1b2c-4d3e-8f40-516273849a5b"),
        environment=Environment("linux", "x86_64", "3.12.4"),
    ),
    _situated(
        7,
        EventType.INSTALLED,
        Source.AART_CLI,
        Context(),
        environment=Environment("linux", "x86_64", "3.12.4"),
    ),
]


@pytest.mark.parametrize(
    "selection",
    [
        artifacts.Selection(artifacts.EPOCH),
        artifacts.Selection(artifacts.EPOCH, harness="claude"),
        artifacts.Selection(artifacts.EPOCH, harness="opencode"),
        artifacts.Selection(artifacts.EPOCH, scope=Scope.USER),
        artifacts.Selection(artifacts.EPOCH, registry="company", automated=False),
        artifacts.Selection(artifacts.EPOCH, interface=Interface.TUI),
        artifacts.Selection(artifacts.EPOCH, automated=True),
    ],
)
def test_a_context_s_edges_agree_with_the_definitions(
    make: Callable[[], EventRepository], selection: artifacts.Selection
) -> None:
    repository = make()
    repository.add(_CONTEXTS)
    oracle = _oracle(_CONTEXTS)

    reads: list[Callable[[EventRepository], IOResult[object, StorageError]]] = [
        lambda r: r.tallies(selection),
        lambda r: r.recent(selection, artifacts.MAX_EVENTS, None),
    ]
    for read in reads:
        assert _value(read(repository)) == _value(read(oracle))
    for dimension in artifacts.Dimension:
        by = (dimension,)
        assert _value(repository.breakdown(selection, by, artifacts.MAX_GROUPS)) == _value(
            oracle.breakdown(selection, by, artifacts.MAX_GROUPS)
        ), dimension
    assert _value(oracle.recent(selection, artifacts.MAX_EVENTS, None)), "selects something"


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
def test_the_wide_view_is_every_event_with_all_it_said(tmp_path: Path, backend: str) -> None:
    url = _shared(tmp_path, backend)
    _empty(url)
    repository = _sql(url)
    repository.add(_CONTEXTS)
    every = artifacts.Selection(artifacts.EPOCH)
    listed = _value(repository.recent(every, artifacts.MAX_EVENTS, None))
    kept = {stored.event_id: stored for stored in _CONTEXTS}

    with repository.engine.connect() as connection:
        rows = {
            row["event_id"]: row
            for row in connection.execute(text("SELECT * FROM event_wide")).mappings()
        }
    repository.engine.dispose()

    assert set(rows) == {view.event_id for view in listed}
    for view in listed:
        row = rows[view.event_id]
        said = {item.name: getattr(view.context, item.name) for item in dataclasses.fields(Context)}
        said["harness"] = artifacts.harness_of(kept[view.event_id])
        environment = view.environment
        assert {name: row[name] for name in said} == said
        assert (row["os"], row["arch"], row["python"]) == (
            (None, None, None)
            if environment is None
            else (environment.os, environment.arch, environment.python)
        )
        assert bool(row["automated"]) is view.automated
        assert (row["event_type"], row["source"], row["artifact_id"], row["client_id"]) == (
            view.event_type.value,
            view.source.value,
            view.artifact_id,
            view.client_id,
        )


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
        repository.top_failures(period, 3),
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


def _empty(url: str) -> None:
    """No tables; for SQLite no file either, so each round starts in a new file's journal mode."""

    if url.startswith("sqlite"):
        database = url.removeprefix("sqlite:///")
        for suffix in ("", "-wal", "-shm"):
            Path(f"{database}{suffix}").unlink(missing_ok=True)
        return
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text(f"DROP VIEW IF EXISTS {EVENT_WIDE}"))
        metadata.drop_all(connection)
    engine.dispose()


def _opened_together(url: str, replicas: int = 4) -> list[bool]:
    """Whether each of `replicas` openers of `url`, started at one moment, opened it."""

    barrier = threading.Barrier(replicas)
    opened: list[bool] = []

    def start() -> None:
        barrier.wait()
        result = perform(open_repository(url))
        opened.append(is_successful(result))
        if is_successful(result):
            result.unwrap().engine.dispose()

    threads = [threading.Thread(target=start) for _ in range(replicas)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    return opened


def _shared(tmp_path: Path, backend: str) -> str:
    if backend == "postgresql":
        if not POSTGRES:
            pytest.skip("AART_CLI_TELEMETRY_TEST_POSTGRES_URL names no database")
        return POSTGRES
    (tmp_path / "data").mkdir(mode=0o700)
    return sqlite_url(str(tmp_path / "data"))


#: Rounds of servers starting together. On a new SQLite file one round in about five lost a
#: server without the retry `_wal` makes, so thirty miss that about once in a thousand runs;
#: PostgreSQL lost every round without its lock.
ROUNDS = {"sqlite": 30, "postgresql": 5}


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
def test_replicas_starting_together_on_an_empty_database_all_open_it(
    tmp_path: Path, backend: str
) -> None:
    url = _shared(tmp_path, backend)
    for _ in range(ROUNDS[backend]):
        _empty(url)
        assert _opened_together(url) == [True] * 4
        engine = create_engine(url)
        with engine.connect() as connection:
            assert connection.execute(select(schema_info.c.version)).scalars().all() == [
                SCHEMA_VERSION
            ]
        engine.dispose()


@pytest.mark.parametrize(
    "later",
    [failures, automation, failure_causes, event_context],
    ids=["failure", "automation", "failure_cause", "event_context"],
)
@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
def test_replicas_starting_together_on_a_database_before_a_later_table_all_open_it(
    tmp_path: Path, backend: str, later: Table
) -> None:
    url = _shared(tmp_path, backend)
    for _ in range(ROUNDS[backend]):
        _empty(url)
        _value(open_repository(url)).engine.dispose()
        engine = create_engine(url)
        with engine.begin() as connection:
            connection.execute(text(f"DROP VIEW {EVENT_WIDE}"))
            later.drop(connection)
        assert _opened_together(url) == [True] * 4
        assert later.name in inspect(engine).get_table_names()
        assert EVENT_WIDE in inspect(engine).get_view_names()
        engine.dispose()


def test_a_connection_the_database_dropped_is_replaced_before_it_is_used() -> None:
    if not POSTGRES:
        pytest.skip("AART_CLI_TELEMETRY_TEST_POSTGRES_URL names no database")
    repository = _sql(POSTGRES)
    assert repository.ping() == IOSuccess(None)
    # What a restart, a failover or an idle timeout does to the connections a pool holds.
    administrator = create_engine(POSTGRES)
    with administrator.connect() as connection:
        connection.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                " WHERE datname = current_database() AND pid <> pg_backend_pid()"
            )
        )
    administrator.dispose()

    assert repository.ping() == IOSuccess(None)
    assert repository.add([numbered(1)]) == IOSuccess(None)
    repository.engine.dispose()


def test_only_sqlite_is_backed_up_here() -> None:
    repository = _sql("sqlite://")
    object.__setattr__(repository.engine.dialect, "name", "postgresql")
    try:
        refused = perform(repository.backup("/nonexistent/copy"))
    finally:
        object.__setattr__(repository.engine.dialect, "name", "sqlite")
    assert refused.failure() == StorageError("only a SQLite database is backed up here")
