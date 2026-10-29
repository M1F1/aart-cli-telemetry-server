"""Receiving a batch, and reading the dashboard's numbers."""

from __future__ import annotations

from dataclasses import dataclass

from returns.io import IOResult
from returns.pipeline import flow
from returns.pointfree import bind

from aart_cli_telemetry_server.domain.contract import Refusal, read_batch
from aart_cli_telemetry_server.domain.events import Batch, acknowledged, stored_events
from aart_cli_telemetry_server.domain.stats import (
    ArtifactUsage,
    DailyCount,
    SourceUsage,
    Summary,
    window,
)

from .ports import Clock, EventRepository, StorageError

__all__ = [
    "Acknowledgement",
    "IngestFailure",
    "artifacts_by_source",
    "daily_counts",
    "ingest",
    "summary",
    "top_artifacts",
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


def daily_counts(
    days: int, repository: EventRepository, clock: Clock
) -> IOResult[tuple[DailyCount, ...], StorageError]:
    return repository.daily(window(days, clock()))


def artifacts_by_source(
    days: int, repository: EventRepository, clock: Clock
) -> IOResult[tuple[SourceUsage, ...], StorageError]:
    return repository.by_source(window(days, clock()))
