"""The dashboard's definitions, in `domain.stats`, on examples and on every history."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st

from aart_telemetry_server.domain import stats
from aart_telemetry_server.domain.events import (
    Batch,
    Event,
    EventType,
    Source,
    StoredEvent,
    acknowledged,
    stamp,
    stored_events,
)

from .strategies import CLIENTS, NOW, events, histories

WEEK = stats.window(7, NOW)
_NUMBERS = itertools.count()


def _stored(
    kind: EventType,
    name: str = "github",
    *,
    days_ago: float = 0,
    client: str = CLIENTS[0],
    source: Source | None = None,
    event_id: str | None = None,
) -> StoredEvent:
    chosen = source or (Source.AART_CLI if kind is not EventType.USED else Source.CLAUDE_HOOK)
    number = next(_NUMBERS)
    return StoredEvent(
        Event(
            event_id or f"00000000-0000-4000-8000-{number:012d}",
            NOW - timedelta(days=days_ago),
            kind,
            chosen,
            name,
            "mcp",
            None,
        ),
        client,
        "0.22.0",
        NOW,
    )


def test_installs_and_uses_are_counted_apart() -> None:
    history = [
        _stored(EventType.INSTALLED),
        _stored(EventType.USED),
        _stored(EventType.USED, client=CLIENTS[1]),
        _stored(EventType.USED, "jira", source=Source.OPENCODE_PLUGIN),
        _stored(EventType.REMOVED, "jira"),
        _stored(EventType.UPDATED, days_ago=30),
    ]
    assert stats.summarise(history, WEEK) == stats.Summary(
        uses=3,
        installs=1,
        updates=0,
        removals=1,
        active_clients=2,
        using_clients=2,
        used_artifacts=2,
    )
    top = stats.top_artifacts(history, WEEK, 10)
    assert [(item.artifact_id, item.uses, item.clients, item.installs) for item in top] == [
        ("github", 2, 2, 1),
        ("jira", 1, 1, 0),
    ]
    assert [(item.source, item.uses) for item in stats.by_source(history, WEEK)] == [
        (Source.CLAUDE_HOOK, 2),
        (Source.OPENCODE_PLUGIN, 1),
    ]
    assert [(day.uses, day.installs, day.removals) for day in stats.daily(history, WEEK)] == [
        (3, 1, 1)
    ]


def test_the_period_starts_exactly_days_before_now() -> None:
    edge = _stored(EventType.USED, days_ago=7)
    past = _stored(EventType.USED, days_ago=7.000001)
    assert stats.summarise([edge, past], WEEK).uses == 1


def test_a_period_and_a_list_have_bounds() -> None:
    for days in (0, stats.MAX_DAYS + 1):
        with pytest.raises(ValueError, match="days"):
            stats.window(days, NOW)
    for limit in (0, stats.MAX_TOP + 1):
        with pytest.raises(ValueError, match="artifacts"):
            stats.top_artifacts([], WEEK, limit)


def test_ties_are_broken_by_name_then_kind() -> None:
    history = [_stored(EventType.USED, name) for name in ("b", "a", "c", "a")]
    assert [item.artifact_id for item in stats.top_artifacts(history, WEEK, 3)] == ["a", "b", "c"]


@given(history=histories, days=st.integers(1, 60))
def test_every_count_agrees_with_every_other(history: list[StoredEvent], days: int) -> None:
    period = stats.window(days, NOW)
    summary = stats.summarise(history, period)
    days_counted = stats.daily(history, period)
    every = stats.top_artifacts(history, period, stats.MAX_TOP)

    assert summary.uses == sum(day.uses for day in days_counted) == sum(i.uses for i in every)
    assert summary.uses == sum(item.uses for item in stats.by_source(history, period))
    assert summary.installs == sum(day.installs for day in days_counted)
    assert summary.used_artifacts == len(every)
    assert summary.using_clients <= summary.active_clients <= len(CLIENTS)
    assert all(day.active_clients <= summary.active_clients for day in days_counted)
    assert [day.day for day in days_counted] == sorted({day.day for day in days_counted})
    ranks = [(-item.uses, item.artifact_id, item.artifact_type) for item in every]
    assert ranks == sorted(ranks)


@given(history=histories, short=st.integers(1, 30), extra=st.integers(0, 30))
def test_a_longer_period_never_counts_less(
    history: list[StoredEvent], short: int, extra: int
) -> None:
    near = stats.summarise(history, stats.window(short, NOW))
    far = stats.summarise(history, stats.window(short + extra, NOW))
    assert near.uses <= far.uses
    assert near.active_clients <= far.active_clients


@given(
    items=st.lists(
        events(st.sampled_from([f"{n:08d}-0000-4000-8000-000000000000" for n in range(5)])),
        min_size=1,
    )
)
def test_a_batch_keeps_each_id_once_and_acknowledges_each_once(items: list[Event]) -> None:
    batch = Batch(CLIENTS[0], "0.22.0", tuple(items))
    kept = stored_events(batch, NOW)
    assert [stored.event_id for stored in kept] == list(acknowledged(batch))
    assert all(
        stored.event is next(i for i in items if i.event_id == stored.event_id) for stored in kept
    )


def test_an_event_is_kept_in_utc_and_from_a_source_that_can_report_it() -> None:
    with pytest.raises(ValueError, match="UTC"):
        Event("x", NOW.replace(tzinfo=None), EventType.USED, Source.CLAUDE_HOOK, "a", "mcp", None)
    with pytest.raises(ValueError, match="source"):
        Event("x", NOW, EventType.USED, Source.AART_CLI, "a", "mcp", None)


def test_each_count_is_its_own_and_ordered() -> None:
    history = [
        _stored(EventType.INSTALLED, "zeta"),
        _stored(EventType.INSTALLED, "zeta", client=CLIENTS[1]),
        _stored(EventType.UPDATED, "zeta", days_ago=1),
        _stored(EventType.USED, "zeta", source=Source.CLAUDE_HOOK, days_ago=2),
        _stored(EventType.USED, "zeta", source=Source.OPENCODE_PLUGIN, days_ago=1),
        _stored(EventType.USED, "zeta", source=Source.OPENCODE_PLUGIN),
    ]
    (top,) = stats.top_artifacts(history, WEEK, 10)
    assert top == stats.ArtifactUsage("mcp", "zeta", 3, 1, 2, NOW)
    assert stats.by_source(history, WEEK) == (
        stats.SourceUsage(Source.OPENCODE_PLUGIN, "mcp", 2),
        stats.SourceUsage(Source.CLAUDE_HOOK, "mcp", 1),
    )
    assert [
        (day.day, day.uses, day.installs, day.updates) for day in stats.daily(history, WEEK)
    ] == [
        ((NOW - timedelta(days=2)).date(), 1, 0, 0),
        ((NOW - timedelta(days=1)).date(), 1, 0, 1),
        (NOW.date(), 1, 2, 0),
    ]


def test_a_period_is_counted_back_from_now_in_utc() -> None:
    elsewhere = NOW.astimezone(timezone(timedelta(hours=-5)))
    counted = stats.window(7, elsewhere)
    assert counted == WEEK
    assert counted.since.tzinfo is UTC


def test_a_stamp_is_utc_to_the_microsecond() -> None:
    moment = datetime(2026, 10, 29, 19, 0, 0, 5, tzinfo=timezone(timedelta(hours=1)))
    assert stamp(moment) == "2026-10-29T18:00:00.000005Z"


def test_a_moment_in_another_zone_is_refused() -> None:
    elsewhere = NOW.astimezone(timezone(timedelta(hours=1)))
    with pytest.raises(ValueError, match="a moment is kept in UTC"):
        Event("x", elsewhere, EventType.USED, Source.CLAUDE_HOOK, "a", "mcp", None)
    with pytest.raises(ValueError, match="this source cannot report this event type"):
        Event("x", NOW, EventType.USED, Source.AART_CLI, "a", "mcp", None)


def test_a_kept_event_remembers_the_client_and_version() -> None:
    batch = Batch(CLIENTS[1], "0.23.0", (_stored(EventType.USED).event,))
    (kept,) = stored_events(batch, NOW)
    assert (kept.client_id, kept.client_version, kept.received_at) == (CLIENTS[1], "0.23.0", NOW)
