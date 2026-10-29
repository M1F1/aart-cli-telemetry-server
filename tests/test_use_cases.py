"""The use cases against the in-memory repository and a fixed clock: no web, no database."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta

from hypothesis import given
from hypothesis import strategies as st
from returns.io import IOFailure, IOResult, IOSuccess
from returns.result import Failure, Success

from aart_telemetry_server.adapters.memory import InMemoryEventRepository
from aart_telemetry_server.application import use_cases
from aart_telemetry_server.application.ports import StorageError
from aart_telemetry_server.domain import stats
from aart_telemetry_server.domain.contract import RefusalKind
from aart_telemetry_server.domain.events import Event, StoredEvent
from aart_telemetry_server.effects import perform

from .strategies import CLIENTS, NOW, events, request


@given(items=st.lists(events(), min_size=1, max_size=10))
def test_a_batch_is_kept_once_received_now_and_every_id_acknowledged(items: list[Event]) -> None:
    repository = InMemoryEventRepository()
    raw = request(items + items[:1])

    first = perform(use_cases.ingest(raw, repository, lambda: NOW))
    again = perform(use_cases.ingest(raw, repository, lambda: NOW + timedelta(hours=1)))

    ids = list(dict.fromkeys(item.event_id for item in items))
    assert first == again == Success(use_cases.Acknowledgement(tuple(ids)))
    assert [stored.event_id for stored in repository.events] == ids
    assert {stored.received_at for stored in repository.events} == {NOW}
    assert {stored.client_id for stored in repository.events} == {CLIENTS[0]}


def test_a_refused_request_keeps_nothing() -> None:
    repository = InMemoryEventRepository()
    refused = perform(use_cases.ingest(b"{}", repository, lambda: NOW))
    assert isinstance(refused, Failure)
    assert refused.failure().kind is RefusalKind.INVALID
    assert repository.events == ()


class _Down(InMemoryEventRepository):
    def add(self, events: Sequence[StoredEvent]) -> IOResult[None, StorageError]:
        return IOFailure(StorageError("OperationalError"))


@given(items=st.lists(events(), min_size=1, max_size=3))
def test_a_database_that_fails_is_never_an_acknowledgement(items: list[Event]) -> None:
    failed = perform(use_cases.ingest(request(items), _Down(), lambda: NOW))
    assert failed == Failure(StorageError("OperationalError"))


class _Recording(InMemoryEventRepository):
    def __init__(self) -> None:
        super().__init__()
        self.periods: list[tuple[str, stats.Window, int | None]] = []

    def summary(self, period: stats.Window) -> IOResult[stats.Summary, StorageError]:
        self.periods.append(("summary", period, None))
        return super().summary(period)

    def top_artifacts(
        self, period: stats.Window, limit: int
    ) -> IOResult[tuple[stats.ArtifactUsage, ...], StorageError]:
        self.periods.append(("top", period, limit))
        return super().top_artifacts(period, limit)

    def daily(self, period: stats.Window) -> IOResult[tuple[stats.DailyCount, ...], StorageError]:
        self.periods.append(("daily", period, None))
        return super().daily(period)

    def by_source(
        self, period: stats.Window
    ) -> IOResult[tuple[stats.SourceUsage, ...], StorageError]:
        self.periods.append(("source", period, None))
        return super().by_source(period)


@given(days=st.integers(1, stats.MAX_DAYS), limit=st.integers(1, stats.MAX_TOP))
def test_each_reading_asks_for_the_last_days_before_the_clock(days: int, limit: int) -> None:
    repository = _Recording()
    since = stats.Window(NOW - timedelta(days=days))

    assert use_cases.summary(days, repository, lambda: NOW) == IOSuccess(
        stats.Summary(0, 0, 0, 0, 0, 0, 0)
    )
    assert use_cases.top_artifacts(days, limit, repository, lambda: NOW) == IOSuccess(())
    assert use_cases.daily_counts(days, repository, lambda: NOW) == IOSuccess(())
    assert use_cases.artifacts_by_source(days, repository, lambda: NOW) == IOSuccess(())
    assert repository.periods == [
        ("summary", since, None),
        ("top", since, limit),
        ("daily", since, None),
        ("source", since, None),
    ]
