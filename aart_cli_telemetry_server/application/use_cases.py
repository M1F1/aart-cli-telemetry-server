"""Receiving a batch, and reading the dashboard's numbers."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from returns.io import IOResult
from returns.pipeline import flow
from returns.pointfree import bind

from aart_cli_telemetry_server.domain.artifacts import (
    EPOCH,
    ArtifactDetail,
    ArtifactRow,
    ClientVersion,
    EventView,
    Order,
    Selection,
    artifact_detail,
    artifact_rows,
    ranked,
)
from aart_cli_telemetry_server.domain.contract import Refusal, read_batch
from aart_cli_telemetry_server.domain.events import Batch, acknowledged, stored_events
from aart_cli_telemetry_server.domain.stats import (
    ArtifactUsage,
    DailyCount,
    FailureUsage,
    SourceUsage,
    Summary,
    UnmanagedUsage,
    window,
)

from .ports import Clock, EventRepository, StorageError

__all__ = [
    "Acknowledgement",
    "IngestFailure",
    "artifact",
    "artifacts",
    "artifacts_by_source",
    "client_versions",
    "daily_counts",
    "events",
    "ingest",
    "summary",
    "top_artifacts",
    "top_failures",
    "top_unmanaged",
]

IngestFailure = Refusal | StorageError


@dataclass(frozen=True, slots=True)
class Acknowledgement:
    """Every id the request carried: stored now or before, each is safe for the client to forget."""

    event_ids: tuple[str, ...]


def ingest(
    raw: bytes, repository: EventRepository, clock: Clock
) -> IOResult[Acknowledgement, IngestFailure]:
    """Read `raw` as a batch and keep it; an acknowledgement, or why not."""

    def keep(batch: Batch) -> IOResult[Acknowledgement, IngestFailure]:
        kept: IOResult[None, IngestFailure] = repository.add(stored_events(batch, clock()))
        return kept.map(lambda _: Acknowledgement(acknowledged(batch)))

    read: IOResult[Batch, IngestFailure] = IOResult.from_result(read_batch(raw))
    return flow(read, bind(keep))


def summary(
    days: int, repository: EventRepository, clock: Clock
) -> IOResult[Summary, StorageError]:
    return repository.summary(window(days, clock()))


def top_artifacts(
    days: int, limit: int, repository: EventRepository, clock: Clock
) -> IOResult[tuple[ArtifactUsage, ...], StorageError]:
    return repository.top_artifacts(window(days, clock()), limit)


def top_unmanaged(
    days: int, limit: int, repository: EventRepository, clock: Clock
) -> IOResult[tuple[UnmanagedUsage, ...], StorageError]:
    return repository.top_unmanaged(window(days, clock()), limit)


def top_failures(
    days: int, limit: int, repository: EventRepository, clock: Clock
) -> IOResult[tuple[FailureUsage, ...], StorageError]:
    return repository.top_failures(window(days, clock()), limit)


def daily_counts(
    days: int, repository: EventRepository, clock: Clock
) -> IOResult[tuple[DailyCount, ...], StorageError]:
    return repository.daily(window(days, clock()))


def artifacts_by_source(
    days: int, repository: EventRepository, clock: Clock
) -> IOResult[tuple[SourceUsage, ...], StorageError]:
    return repository.by_source(window(days, clock()))


def _holding(selection: Selection) -> Selection:
    """Who holds an artifact is read from every install, update and removal up to `until`."""

    return replace(selection, since=EPOCH, event_type=None)


def artifacts(
    selection: Selection, order: Order, limit: int, search: str | None, repository: EventRepository
) -> IOResult[tuple[ArtifactRow, ...], StorageError]:
    """Every artifact the selected events are about, ordered, at most `limit`.

    A kind narrows the rows, not what is counted: a plugin's row still adds up its skills' uses.
    """

    every = replace(selection, artifact_type=None)

    def listed(rows: tuple[ArtifactRow, ...]) -> tuple[ArtifactRow, ...]:
        kind = selection.artifact_type
        return ranked(
            tuple(row for row in rows if kind is None or row.artifact_type == kind),
            order,
            limit,
            search,
        )

    return repository.tallies(every).bind(
        lambda counted: repository.reach(every).bind(
            lambda reached: repository.holdings(_holding(every)).map(
                lambda held: listed(artifact_rows(counted, reached, held))
            )
        )
    )


def artifact(
    selection: Selection, artifact_type: str, artifact_id: str, repository: EventRepository
) -> IOResult[ArtifactDetail | None, StorageError]:
    """One artifact and its members, from the events `selection` holds about them."""

    about = replace(selection, artifact_type=artifact_type, artifact_id=artifact_id, members=True)
    return repository.tallies(about).bind(
        lambda counted: repository.reach(about).bind(
            lambda reached: repository.holdings(_holding(about)).map(
                lambda held: artifact_detail(counted, reached, held, artifact_type, artifact_id)
            )
        )
    )


def events(
    selection: Selection,
    limit: int,
    before: tuple[datetime, str] | None,
    repository: EventRepository,
) -> IOResult[tuple[EventView, ...], StorageError]:
    return repository.recent(selection, limit, before)


def client_versions(
    selection: Selection, repository: EventRepository
) -> IOResult[tuple[ClientVersion, ...], StorageError]:
    return repository.client_versions(selection)
