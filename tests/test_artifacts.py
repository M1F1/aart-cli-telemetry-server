"""What is shown about an artifact is composed from the repository's answers without losing any.

Over generated histories: every selected event is counted once in exactly one row, a plugin's
skills' uses add up on its row, a detail's versions and days add up to its row, and a list is
ordered as it says and holds only what its search asks for.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import timedelta

from hypothesis import given
from hypothesis import strategies as st

from aart_cli_telemetry_server.domain.artifacts import (
    EPOCH,
    MANAGED,
    ArtifactRow,
    Order,
    Selection,
    artifact_detail,
    artifact_rows,
    holdings,
    ranked,
    reach,
    tallies,
)
from aart_cli_telemetry_server.domain.events import Event, EventType, Source, StoredEvent

from .strategies import CLIENTS, NOW, histories, selections


def _rows(history: list[StoredEvent], selection: Selection):  # type: ignore[no-untyped-def]
    return artifact_rows(
        tallies(history, selection), reach(history, selection), holdings(history, selection)
    )


@given(history=histories, selection=selections())
def test_every_selected_event_is_counted_once_in_its_own_row(
    history: list[StoredEvent], selection: Selection
) -> None:
    rows = _rows(history, selection)
    counted = Counter(
        (stored.event.artifact_type, stored.event.artifact_id, stored.event.event_type)
        for stored in history
        if stored.event.event_type in MANAGED and selection.holds(stored)
    )
    shown = Counter[tuple[str, str, EventType]]()
    for row in rows:
        for kind, n in (
            (EventType.INSTALLED, row.installs),
            (EventType.UPDATED, row.updates),
            (EventType.REMOVED, row.removals),
            (EventType.USED, row.uses),
        ):
            if n:
                shown[(row.artifact_type, row.artifact_id, kind)] = n
    assert shown == counted
    assert len({(row.artifact_type, row.artifact_id) for row in rows}) == len(rows)


@given(history=histories)
def test_a_plugin_s_row_adds_up_its_skills_uses_and_nothing_else_does(
    history: list[StoredEvent],
) -> None:
    rows = _rows(history, Selection(EPOCH))
    for row in rows:
        skills = sum(
            other.uses
            for other in rows
            if other.artifact_type == "skill"
            and other.artifact_id.startswith(f"{row.artifact_id}:")
            and other.artifact_id != f"{row.artifact_id}:"
        )
        assert row.member_uses == (skills if row.artifact_type == "plugin" else 0)
        plugin, colon, member = row.artifact_id.partition(":")
        named = row.artifact_type == "skill" and colon and plugin and member
        assert row.plugin == (plugin if named else None)


@given(history=histories, selection=selections())
def test_a_detail_s_versions_and_days_add_up_to_its_row(
    history: list[StoredEvent], selection: Selection
) -> None:
    for row in _rows(history, selection):
        detail = artifact_detail(
            tallies(history, selection),
            reach(history, selection),
            holdings(history, selection),
            row.artifact_type,
            row.artifact_id,
        )
        assert detail is not None
        assert detail.artifact == row
        for part in (detail.versions, detail.days):
            assert sum(item.installs for item in part) == row.installs
            assert sum(item.updates for item in part) == row.updates
            assert sum(item.removals for item in part) == row.removals
            assert sum(item.uses for item in part) == row.uses
        assert sum(item.events for item in detail.sources) == (
            row.installs + row.updates + row.removals + row.uses
        )
        assert [item.last_seen for item in detail.versions] == sorted(
            (item.last_seen for item in detail.versions), reverse=True
        )


@given(
    history=histories,
    order=st.sampled_from(list(Order)),
    limit=st.integers(1, 50),
    search=st.none() | st.sampled_from(("SUPER", "tdd", "g", "")),
)
def test_a_list_is_in_its_order_and_holds_only_what_it_searched_for(
    history: list[StoredEvent], order: Order, limit: int, search: str | None
) -> None:
    rows = _rows(history, Selection(EPOCH))
    listed = ranked(rows, order, limit, search)

    wanted = [row for row in rows if (search or "").casefold() in row.artifact_id.casefold()]
    assert len(listed) == min(limit, len(wanted))
    assert set(listed) <= set(wanted)
    keys_of: dict[Order, Callable[[ArtifactRow], tuple[float, ...]]] = {
        Order.INSTALLS: lambda row: (-row.installs, -(row.uses + row.member_uses)),
        Order.USES: lambda row: (-(row.uses + row.member_uses), -row.installs),
        Order.HOLDERS: lambda row: (-row.holders, -row.installs),
        Order.RECENT: lambda row: (-((row.last_seen or EPOCH) - EPOCH).total_seconds(),),
        Order.NAME: lambda row: (),
    }
    key = keys_of[order]
    keys = [(*key(row), row.artifact_id, row.artifact_type) for row in listed]
    assert keys == sorted(keys)
    if len(wanted) > limit:
        left = [row for row in wanted if row not in listed]
        assert max(keys) <= min((*key(row), row.artifact_id, row.artifact_type) for row in left)


def _lifecycle(kind: EventType, minutes: int, client: str = CLIENTS[0]) -> StoredEvent:
    return StoredEvent(
        Event(
            f"00000000-0000-4000-8000-{minutes:012d}",
            NOW - timedelta(minutes=minutes),
            kind,
            Source.AART_CLI,
            "superpowers",
            "plugin",
            "6.2.0",
        ),
        client,
        "0.28.0",
        NOW,
    )


def test_an_installation_holds_what_it_installed_or_updated_last() -> None:
    history = [
        _lifecycle(EventType.INSTALLED, 50),
        _lifecycle(EventType.REMOVED, 40),
        _lifecycle(EventType.UPDATED, 30, CLIENTS[1]),
        _lifecycle(EventType.INSTALLED, 20, CLIENTS[2]),
        _lifecycle(EventType.REMOVED, 10, CLIENTS[2]),
        _lifecycle(EventType.INSTALLED, 5, CLIENTS[2]),
    ]
    (held,) = holdings(history, Selection(EPOCH))
    assert held.holders == 2
    (before,) = holdings(history, Selection(EPOCH, until=NOW - timedelta(minutes=6)))
    assert before.holders == 1
