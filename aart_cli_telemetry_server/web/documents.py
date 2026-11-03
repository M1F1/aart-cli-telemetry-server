"""The documents the read API and the dashboard's aggregates answer with, over HTTP and MCP."""

from __future__ import annotations

from dataclasses import fields
from enum import StrEnum
from typing import Any

from aart_cli_telemetry_server.domain.artifacts import (
    ArtifactDetail,
    ArtifactRow,
    ClientVersion,
    Dimension,
    EventView,
    Group,
)
from aart_cli_telemetry_server.domain.events import Context, stamp
from aart_cli_telemetry_server.domain.stats import (
    ArtifactUsage,
    DailyCount,
    FailureUsage,
    SourceUsage,
    Summary,
    UnmanagedUsage,
)

__all__ = [
    "artifact_detail_document",
    "artifact_document",
    "by_source_document",
    "client_versions_document",
    "daily_document",
    "event_document",
    "events_document",
    "failures_document",
    "summary_document",
    "top_artifacts_document",
    "top_unmanaged_document",
]


def summary_document(days: int, found: Summary) -> dict[str, Any]:
    return {
        "days": days,
        "uses": found.uses,
        "installs": found.installs,
        "updates": found.updates,
        "removals": found.removals,
        "active_clients": found.active_clients,
        "using_clients": found.using_clients,
        "used_artifacts": found.used_artifacts,
        "unmanaged_uses": found.unmanaged_uses,
        "failures": found.failures,
        "automated_clients": found.automated_clients,
    }


def top_artifacts_document(days: int, found: tuple[ArtifactUsage, ...]) -> dict[str, Any]:
    return {
        "days": days,
        "artifacts": [
            {
                "artifact_type": item.artifact_type,
                "artifact_id": item.artifact_id,
                "uses": item.uses,
                "clients": item.clients,
                "installs": item.installs,
                "last_used": stamp(item.last_used),
            }
            for item in found
        ],
    }


def top_unmanaged_document(days: int, found: tuple[UnmanagedUsage, ...]) -> dict[str, Any]:
    return {
        "days": days,
        "tools": [
            {
                "artifact_type": item.artifact_type,
                "artifact_id": item.artifact_id,
                "uses": item.uses,
                "clients": item.clients,
                "last_used": stamp(item.last_used),
            }
            for item in found
        ],
    }


def failures_document(days: int, found: tuple[FailureUsage, ...]) -> dict[str, Any]:
    return {
        "days": days,
        "failures": [
            {
                "fingerprint": item.fingerprint,
                "kind": item.kind,
                "place": item.place,
                "exception": item.exception,
                "frames": list(item.frames),
                "causes": [
                    {"exception": cause.exception, "frames": list(cause.frames)}
                    for cause in item.causes
                ],
                "diagnostics": list(item.diagnostics),
                "occurrences": item.occurrences,
                "clients": item.clients,
                "versions": list(item.versions),
                "platforms": list(item.platforms),
                "first_seen": stamp(item.first_seen),
                "last_seen": stamp(item.last_seen),
            }
            for item in found
        ],
    }


def daily_document(days: int, found: tuple[DailyCount, ...]) -> dict[str, Any]:
    return {
        "days": days,
        "daily": [
            {
                "day": item.day.isoformat(),
                "uses": item.uses,
                "installs": item.installs,
                "updates": item.updates,
                "removals": item.removals,
                "active_clients": item.active_clients,
                "automated_clients": item.automated_clients,
            }
            for item in found
        ],
    }


def by_source_document(days: int, found: tuple[SourceUsage, ...]) -> dict[str, Any]:
    return {
        "days": days,
        "by_source": [
            {
                "source": item.source.value,
                "artifact_type": item.artifact_type,
                "uses": item.uses,
            }
            for item in found
        ],
    }


def artifact_document(item: ArtifactRow) -> dict[str, Any]:
    return {
        "artifact_type": item.artifact_type,
        "artifact_id": item.artifact_id,
        "plugin": item.plugin,
        "latest_version": item.latest_version,
        "installs": item.installs,
        "updates": item.updates,
        "removals": item.removals,
        "holders": item.holders,
        "uses": item.uses,
        "member_uses": item.member_uses,
        "installed_by": item.installed_by,
        "used_by": item.used_by,
        "first_seen": None if item.first_seen is None else stamp(item.first_seen),
        "last_seen": None if item.last_seen is None else stamp(item.last_seen),
    }


def artifact_detail_document(found: ArtifactDetail) -> dict[str, Any]:
    return {
        **artifact_document(found.artifact),
        "versions": [
            {
                "artifact_version": item.artifact_version,
                "installs": item.installs,
                "updates": item.updates,
                "removals": item.removals,
                "uses": item.uses,
                "last_seen": stamp(item.last_seen),
            }
            for item in found.versions
        ],
        "daily": [
            {
                "day": item.day.isoformat(),
                "installs": item.installs,
                "updates": item.updates,
                "removals": item.removals,
                "uses": item.uses,
            }
            for item in found.days
        ],
        "sources": [{"source": item.source.value, "events": item.events} for item in found.sources],
        "members": [artifact_document(item) for item in found.members],
    }


def event_document(item: EventView) -> dict[str, Any]:
    return {
        "event_id": item.event_id,
        "occurred_at": stamp(item.occurred_at),
        "event_type": item.event_type.value,
        "source": item.source.value,
        "artifact_type": item.artifact_type,
        "artifact_id": item.artifact_id,
        "artifact_version": item.artifact_version,
        "client_id": item.client_id,
        "client_version": item.client_version,
        "automated": item.automated,
        **{
            name: _plain(getattr(item.context, name))
            for name in (field.name for field in fields(Context))
        },
        **{
            name: None if item.environment is None else getattr(item.environment, name)
            for name in ("os", "arch", "python")
        },
    }


def _plain(value: object) -> object:
    return value.value if isinstance(value, StrEnum) else value


def breakdown_document(by: tuple[Dimension, ...], found: tuple[Group, ...]) -> dict[str, Any]:
    return {
        "by": [dimension.value for dimension in by],
        "groups": [
            {
                **{
                    dimension.value: value for dimension, value in zip(by, item.values, strict=True)
                },
                "events": item.events,
                "clients": item.clients,
            }
            for item in found
        ],
    }


def client_versions_document(found: tuple[ClientVersion, ...]) -> dict[str, Any]:
    return {
        "client_versions": [
            {
                "client_version": item.client_version,
                "clients": item.clients,
                "events": item.events,
                "last_seen": stamp(item.last_seen),
            }
            for item in found
        ]
    }


def events_document(found: tuple[EventView, ...], next_before: str | None) -> dict[str, Any]:
    return {"events": [event_document(item) for item in found], "next_before": next_before}
