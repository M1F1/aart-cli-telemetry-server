"""`aart_cli.failed`: read as the contract says, kept beside its event, grouped by defect."""

from __future__ import annotations

import copy
import json
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st
from returns.result import Failure, Success

from aart_cli_telemetry_server.adapters.memory import InMemoryEventRepository
from aart_cli_telemetry_server.adapters.sql import (
    SqlEventRepository,
    open_repository,
    sqlite_url,
)
from aart_cli_telemetry_server.domain import stats
from aart_cli_telemetry_server.domain.contract import Refusal, read_batch
from aart_cli_telemetry_server.domain.events import (
    Event,
    EventType,
    Source,
    StoredEvent,
    fingerprint,
)
from aart_cli_telemetry_server.effects import perform
from aart_cli_telemetry_server.web.app import create_app

from .strategies import CLIENTS, NOW, failed, request

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = json.loads((ROOT / "contract" / "telemetry-api-v1.examples.json").read_text("utf-8"))
FAILED = next(example["request"] for example in EXAMPLES["valid"] if "failure" in example["name"])
WEEK = stats.window(7, NOW)


def _crash() -> dict[str, Any]:
    document: dict[str, Any] = copy.deepcopy(FAILED)
    document["events"] = document["events"][:1]
    return document


def _reasons(document: object) -> list[str]:
    result = read_batch(json.dumps(document).encode())
    assert isinstance(result, Failure), result
    refusal = result.failure()
    assert isinstance(refusal, Refusal)
    return [item.reason for item in refusal.rejected_events]


def _refit(event: dict[str, Any]) -> None:
    """Give `event` the fingerprint its failure has now, as aart-cli would."""

    failure = event["failure"]
    failure["fingerprint"] = fingerprint(
        event["artifact_type"],
        event["artifact_id"],
        failure.get("exception"),
        failure["frames"],
        failure["diagnostics"],
    )


# -- the contract ----------------------------------------------------------------------------------


def test_the_fingerprints_aart_cli_computed_are_the_ones_this_server_computes() -> None:
    batch = read_batch(json.dumps(FAILED).encode()).unwrap()
    for event, sent in zip(batch.events, FAILED["events"], strict=True):
        assert event.event_type is EventType.FAILED
        assert event.failure is not None
        assert event.failure.fingerprint == sent["failure"]["fingerprint"]
        assert (event.artifact_type, event.artifact_id) == (
            sent["artifact_type"],
            sent["artifact_id"],
        )
    assert [event.failure.exit_code for event in batch.events if event.failure] == [None, 3, None]


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("a failure without its failure", "an aart_cli.failed event has a failure"),
        (
            "a failure with a fingerprint that is not its own",
            "the failure's fingerprint is not its own",
        ),
        (
            "a failure that names a frame outside aart-cli",
            "failure must be the contract's failure object",
        ),
        ("a failure that carries a message", "a field the contract does not have"),
        (
            "a crash without its exception",
            "an aart_cli.failed event is a crash, error or interruption at a place, with no "
            "artifact_version, and a crash names its exception",
        ),
        ("a use that carries a failure", "only an aart_cli.failed event has a failure"),
        ("a failure from a harness hook", "this source cannot report this event_type"),
    ],
)
def test_each_invalid_failure_says_which_rule_it_breaks(name: str, reason: str) -> None:
    (example,) = (item for item in EXAMPLES["invalid"] if item["name"] == name)
    document = _crash()
    document["events"].append(example["event"])
    assert _reasons(document) == [reason]


_CHANGES: tuple[Callable[[dict[str, Any]], object], ...] = (
    lambda e: e.update(artifact_type="error"),
    lambda e: e.update(artifact_id="consume.install"),
    lambda e: e["failure"].update(exception="ValueError"),
    lambda e: e["failure"]["frames"].pop(),
    lambda e: e["failure"]["frames"].__setitem__(0, "aart_cli.cli:run:362"),
    lambda e: e["failure"]["diagnostics"].append("registry-unreachable"),
)


@pytest.mark.parametrize("change", _CHANGES)
def test_what_names_the_defect_is_what_the_fingerprint_holds(
    change: Callable[[dict[str, Any]], object],
) -> None:
    document = _crash()
    change(document["events"][0])
    assert _reasons(document) == ["the failure's fingerprint is not its own"]
    _refit(document["events"][0])
    assert isinstance(read_batch(json.dumps(document).encode()), Success)


@given(line=st.integers(0, 999999), exit_code=st.integers(0, 255))
def test_a_line_or_an_exit_code_is_not_part_of_the_defect(line: int, exit_code: int) -> None:
    document = _crash()
    failure = document["events"][0]["failure"]
    failure["frames"][-1] = failure["frames"][-1].rsplit(":", 1)[0] + f":{line}"
    failure["exit_code"] = exit_code
    (event,) = read_batch(json.dumps(document).encode()).unwrap().events
    assert event.failure is not None
    assert event.failure.fingerprint == FAILED["events"][0]["failure"]["fingerprint"]


_INVALID_FAILURES: tuple[Callable[[dict[str, Any]], object], ...] = (
    lambda f: f.update(frames=["aart_cli." + "m" * 128 + ":main:1"]),
    lambda f: f.update(frames=["aart_cli.cli:main:1"] * 31),
    lambda f: f.update(frames=["aart_cli.cli:main:1234567"]),
    lambda f: f.update(frames=["aart_clix.cli:main:1"]),
    lambda f: f.update(frames=["aart_cli.cli:main"]),
    lambda f: f.update(frames=["aart_cli/cli.py:main:1"]),
    lambda f: f.update(diagnostics=["a", "a"]),
    lambda f: f.update(diagnostics=[str(n) for n in range(11)]),
    lambda f: f.update(diagnostics=["Not-A-Code"]),
    lambda f: f.update(exception="KeyError: 'secret'"),
    lambda f: f.update(exception="1Error"),
    lambda f: f.update(exit_code=256),
    lambda f: f.update(exit_code=True),
    lambda f: f.update(exit_code="3"),
    lambda f: f.update(python="3.11 (main)"),
    lambda f: f.update(platform="windows"),
    lambda f: f.pop("python"),
    lambda f: f.pop("frames"),
    lambda f: f.update(frames="aart_cli.cli:main:1"),
)


@pytest.mark.parametrize("change", _INVALID_FAILURES)
def test_a_failure_holds_only_what_its_patterns_allow(
    change: Callable[[dict[str, Any]], object],
) -> None:
    document = _crash()
    change(document["events"][0]["failure"])
    if "frames" in document["events"][0]["failure"] and isinstance(
        document["events"][0]["failure"]["frames"], list
    ):
        _refit(document["events"][0])
    assert _reasons(document) == ["failure must be the contract's failure object"]


@pytest.mark.parametrize(
    "change",
    [
        lambda e: e.update(artifact_type="mcp"),
        lambda e: e.update(artifact_id="Consume.Update"),
        lambda e: e.update(artifact_id="-update"),
        lambda e: e.update(artifact_version="0.24.0"),
    ],
)
def test_a_failure_is_a_kind_at_a_place_without_a_version(
    change: Callable[[dict[str, Any]], object],
) -> None:
    document = _crash()
    change(document["events"][0])
    _refit(document["events"][0])
    assert _reasons(document) == [
        "an aart_cli.failed event is a crash, error or interruption at a place, with no "
        "artifact_version, and a crash names its exception"
    ]


@given(field=st.from_regex(r"[a-z_]{1,20}", fullmatch=True), value=st.text(max_size=20))
def test_no_field_beyond_the_contract_rides_in_a_failure(field: str, value: str) -> None:
    document = _crash()
    if field in document["events"][0]["failure"] or field in ("exception", "exit_code"):
        return
    document["events"][0]["failure"][field] = value
    assert _reasons(document) == ["a field the contract does not have"]


@given(item=failed())
def test_every_failure_the_contract_allows_reads_back_as_it_was(item: Event) -> None:
    assert read_batch(request([item])).unwrap().events == (item,)


def test_a_failure_is_an_aart_cli_failed_event_and_no_other() -> None:
    (event,) = read_batch(json.dumps(_crash()).encode()).unwrap().events
    with pytest.raises(ValueError, match="no other"):
        replace(event, failure=None)
    with pytest.raises(ValueError, match="no version"):
        replace(event, artifact_version="1.0.0")
    with pytest.raises(ValueError, match="no other"):
        replace(event, event_type=EventType.INSTALLED)


# -- the definitions -------------------------------------------------------------------------------


def _occurrence(
    event: Event,
    *,
    line: int,
    minutes_ago: float,
    number: int,
    client: str = CLIENTS[0],
    version: str = "0.24.0",
    platform: str = "darwin",
) -> StoredEvent:
    assert event.failure is not None
    frames = tuple(frame.rsplit(":", 1)[0] + f":{line}" for frame in event.failure.frames)
    return StoredEvent(
        replace(
            event,
            event_id=f"00000000-0000-4000-8000-{number:012d}",
            occurred_at=NOW - timedelta(minutes=minutes_ago),
            failure=replace(event.failure, frames=frames, platform=platform),
        ),
        client,
        version,
        NOW,
    )


def _examples() -> tuple[Event, ...]:
    return read_batch(json.dumps(FAILED).encode()).unwrap().events


def test_failures_group_by_defect_and_say_it_as_its_latest_occurrence_does() -> None:
    crash, error, interrupted = _examples()
    history = [
        _occurrence(crash, line=10, minutes_ago=30, number=1),
        _occurrence(crash, line=20, minutes_ago=5, number=2, client=CLIENTS[1], platform="linux"),
        _occurrence(crash, line=15, minutes_ago=10, number=3, version="0.23.0"),
        _occurrence(error, line=1, minutes_ago=1, number=4),
        _occurrence(error, line=1, minutes_ago=2, number=5),
        _occurrence(interrupted, line=1, minutes_ago=0, number=6),
        _occurrence(interrupted, line=1, minutes_ago=60 * 24 * 8, number=7),
    ]

    found = stats.top_failures(history, WEEK, 10)

    assert [(item.fingerprint, item.occurrences) for item in found] == [
        (crash.failure.fingerprint, 3),  # type: ignore[union-attr]
        (error.failure.fingerprint, 2),  # type: ignore[union-attr]
        (interrupted.failure.fingerprint, 1),  # type: ignore[union-attr]
    ]
    first = found[0]
    assert (first.kind, first.place, first.exception) == ("crash", "consume.update", "KeyError")
    assert first.frames[-1].endswith(":20")
    assert (first.clients, first.versions, first.platforms) == (
        2,
        ("0.23.0", "0.24.0"),
        ("darwin", "linux"),
    )
    assert (first.first_seen, first.last_seen) == (
        NOW - timedelta(minutes=30),
        NOW - timedelta(minutes=5),
    )
    assert found[1].diagnostics == ("registry-unreachable",)
    assert stats.top_failures(history, WEEK, 1) == found[:1]
    with pytest.raises(ValueError, match="1 to"):
        stats.top_failures(history, WEEK, 0)

    summary = stats.summarise(history, WEEK)
    assert (summary.failures, summary.uses, summary.installs) == (6, 0, 0)


def test_as_often_the_latest_comes_first_then_the_fingerprint() -> None:
    crash, error, _ = _examples()
    history = [
        _occurrence(crash, line=1, minutes_ago=9, number=1),
        _occurrence(error, line=1, minutes_ago=3, number=2),
    ]
    assert [item.place for item in stats.top_failures(history, WEEK, 5)] == [
        "consume.install",
        "consume.update",
    ]
    same = [
        _occurrence(crash, line=1, minutes_ago=3, number=1),
        _occurrence(error, line=1, minutes_ago=3, number=2),
    ]
    names = [item.fingerprint for item in stats.top_failures(same, WEEK, 5)]
    assert names == sorted(names)


# -- the repositories ------------------------------------------------------------------------------


@pytest.fixture
def on_disk(tmp_path: Path) -> Iterator[SqlEventRepository]:
    opened = perform(open_repository(sqlite_url(str(tmp_path / "data")))).unwrap()
    yield opened
    opened.engine.dispose()


def test_a_version_1_database_without_failures_gains_the_table_and_keeps_its_events(
    tmp_path: Path,
) -> None:
    path = tmp_path / "telemetry.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "CREATE TABLE schema_info (id INTEGER PRIMARY KEY, version INTEGER NOT NULL);"
            "INSERT INTO schema_info VALUES (1, 1);"
            "CREATE TABLE event (event_id VARCHAR(36) PRIMARY KEY, client_id VARCHAR(36) NOT NULL,"
            " client_version VARCHAR(64) NOT NULL, occurred_at DATETIME NOT NULL,"
            " occurred_day DATE NOT NULL, received_at DATETIME NOT NULL,"
            " event_type VARCHAR(32) NOT NULL, source VARCHAR(32) NOT NULL,"
            " artifact_id VARCHAR(200) NOT NULL, artifact_type VARCHAR(64) NOT NULL,"
            " artifact_version VARCHAR(64));"
            "INSERT INTO event VALUES ('00000000-0000-4000-8000-000000000001', "
            "'0b6f4d1e-8f8a-4a43-9a52-2f0f5d0f6a11', '0.22.0', "
            "'2026-10-29 17:00:00.000000', '2026-10-29', "
            "'2026-10-29 17:00:00.000000', 'artifact.installed', 'aart_cli', 'github', 'mcp', "
            "NULL);"
        )

    repository = perform(open_repository(sqlite_url(str(tmp_path)))).unwrap()
    try:
        crash, _, _ = _examples()
        repository.add([_occurrence(crash, line=1, minutes_ago=1, number=2)])
        summary = perform(repository.summary(WEEK)).unwrap()
        assert (summary.installs, summary.failures) == (1, 1)
        assert len(perform(repository.top_failures(WEEK, 5)).unwrap()) == 1
    finally:
        repository.engine.dispose()


@pytest.mark.parametrize("make", [InMemoryEventRepository, lambda: None])
def test_an_id_kept_as_another_event_is_never_counted_as_a_failure(
    make: Callable[[], InMemoryEventRepository | None], on_disk: SqlEventRepository
) -> None:
    repository = make() or on_disk
    crash, _, _ = _examples()
    failure = _occurrence(crash, line=1, minutes_ago=1, number=1)
    install = StoredEvent(
        Event(
            failure.event_id,
            failure.event.occurred_at,
            EventType.INSTALLED,
            Source.AART_CLI,
            "github",
            "mcp",
            None,
        ),
        CLIENTS[0],
        "0.24.0",
        NOW,
    )
    repository.add([install])
    repository.add([failure])

    summary = perform(repository.summary(WEEK)).unwrap()
    assert (summary.installs, summary.failures) == (1, 0)
    assert perform(repository.top_failures(WEEK, 5)).unwrap() == ()


def test_the_failures_endpoint_says_each_defect_and_nothing_else(
    on_disk: SqlEventRepository,
) -> None:
    client = TestClient(create_app(on_disk, clock=lambda: NOW))
    document = copy.deepcopy(FAILED)
    for event in document["events"]:
        event["occurred_at"] = "2026-10-29T17:00:00Z"
    assert client.post("/api/v1/events/batch", json=document).status_code == 200

    found = client.get("/api/v1/stats/failures?days=7").json()
    assert client.get("/api/v1/stats/summary?days=7").json()["failures"] == 3
    assert found["days"] == 7
    crash = next(item for item in found["failures"] if item["kind"] == "crash")
    assert crash == {
        "fingerprint": "2ffcacfa0fb53a5d",
        "kind": "crash",
        "place": "consume.update",
        "exception": "KeyError",
        "frames": FAILED["events"][0]["failure"]["frames"],
        "diagnostics": [],
        "occurrences": 1,
        "clients": 1,
        "versions": ["0.24.0"],
        "platforms": ["darwin"],
        "first_seen": "2026-10-29T17:00:00.000000Z",
        "last_seen": "2026-10-29T17:00:00.000000Z",
    }
    assert [item["kind"] for item in found["failures"]].count("crash") == 1
    for bad in ("limit=0", f"limit={stats.MAX_TOP + 1}", "days=0"):
        assert client.get(f"/api/v1/stats/failures?{bad}").status_code == 422
