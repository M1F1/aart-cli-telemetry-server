"""Hypothesis strategies for what a client sends and what the server keeps."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from hypothesis import strategies as st

from aart_cli_telemetry_server.domain.events import (
    SOURCES_FOR,
    Event,
    EventType,
    Source,
    StoredEvent,
)

#: The moment every generated history ends at; a test's clock reads this.
NOW = datetime(2026, 10, 29, 18, 0, tzinfo=UTC)

uuids = st.uuids().map(str)
names = st.from_regex(r"[0-9A-Za-z._@:+-]{1,20}", fullmatch=True)
kinds = st.sampled_from(("mcp", "skill", "agent", "command"))
versions = st.none() | st.from_regex(r"[0-9]{1,2}\.[0-9]{1,2}\.[0-9]{1,2}", fullmatch=True)
moments = st.integers(0, 40 * 24 * 3600 * 1_000_000).map(
    lambda micro: NOW - timedelta(microseconds=micro)
)


@st.composite
def events(draw: st.DrawFn, ids: st.SearchStrategy[str] = uuids) -> Event:
    kind = draw(st.sampled_from(list(EventType)))
    return Event(
        event_id=draw(ids),
        occurred_at=draw(moments),
        event_type=kind,
        source=draw(st.sampled_from(sorted(SOURCES_FOR[kind]))),
        artifact_id=draw(
            st.sampled_from(("github", "jira-mcp", "review", "Zeta", "alpha")) | names
        ),
        artifact_type=draw(kinds),
        artifact_version=None if kind is EventType.UNMANAGED_USED else draw(versions),
    )


@st.composite
def stored_events(draw: st.DrawFn) -> StoredEvent:
    return StoredEvent(
        event=draw(events()),
        client_id=draw(st.sampled_from(CLIENTS)),
        client_version="0.22.0",
        received_at=NOW,
    )


CLIENTS = (
    "0b6f4d1e-8f8a-4a43-9a52-2f0f5d0f6a11",
    "1c7a5e2f-9a9b-4b54-8b63-3a1a6e1a7b22",
    "2d8b6f3a-aaac-4c65-9c74-4b2b7f2b8c33",
)

histories = st.lists(stored_events(), max_size=40, unique_by=lambda stored: stored.event_id)


def numbered(
    number: int, kind: EventType = EventType.USED, client: str = CLIENTS[0]
) -> StoredEvent:
    """A fixed stored event: the same `number` is the same event."""

    source = Source.CLAUDE_HOOK if kind is EventType.USED else Source.AART_CLI
    return StoredEvent(
        Event(
            f"00000000-0000-4000-8000-{number:012d}",
            NOW - timedelta(minutes=number),
            kind,
            source,
            f"artifact-{number % 7}",
            "mcp",
            None,
        ),
        client,
        "0.22.0",
        NOW,
    )


def wire(event: Event) -> dict[str, Any]:
    """`event` as a client puts it in a request."""

    document: dict[str, Any] = {
        "event_id": event.event_id,
        "occurred_at": event.occurred_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "event_type": event.event_type.value,
        "source": event.source.value,
        "artifact_id": event.artifact_id,
        "artifact_type": event.artifact_type,
    }
    if event.artifact_version is not None:
        document["artifact_version"] = event.artifact_version
    return document


def request(items: list[Event], client_id: str = CLIENTS[0]) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "client_id": client_id,
            "client_version": "0.22.0",
            "events": [wire(item) for item in items],
        }
    ).encode()


__all__ = [
    "CLIENTS",
    "NOW",
    "Source",
    "events",
    "histories",
    "numbered",
    "request",
    "stored_events",
    "wire",
]
