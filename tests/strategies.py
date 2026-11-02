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
    Failure,
    Source,
    StoredEvent,
    fingerprint,
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


#: Few enough that one defect recurs, with other lines, versions and platforms.
_FRAMES = (
    "aart_cli.cli:main",
    "aart_cli.commands.marketplace:_update",
    "aart_cli.io.receipt_store:installations",
    "aart_cli:<module>",
)


@st.composite
def failed(
    draw: st.DrawFn, ids: st.SearchStrategy[str] = uuids, *, recurring: bool = False
) -> Event:
    """A failure the contract allows; `recurring` ones are a few defects at varying lines, so a
    history holds the same defect more than once."""

    kind = draw(st.sampled_from(("crash", "error", "interrupted")))
    places = st.sampled_from(("consume.update", "tui", "usage-hook"))
    if not recurring:
        places |= st.from_regex(r"[a-z0-9][a-z0-9._-]{0,20}", fullmatch=True)
    place = draw(places)
    named = st.sampled_from(("KeyError", "aart_cli.domain.Refused", "_Private"))
    if recurring:
        exception = "KeyError" if kind == "crash" else None
        shape: tuple[str, ...] = _FRAMES[:2]
        codes: tuple[str, ...] = ()
    else:
        exception = draw(named if kind == "crash" else st.none() | named)
        shape = tuple(draw(st.lists(st.sampled_from(_FRAMES), max_size=3)))
        codes = tuple(
            draw(st.lists(st.sampled_from(("registry-unreachable", "a.b-1", "0")), unique=True))
        )
    frames = tuple(f"{frame}:{draw(st.integers(0, 999999))}" for frame in shape)
    diagnostics = codes
    return Event(
        event_id=draw(ids),
        occurred_at=draw(moments),
        event_type=EventType.FAILED,
        source=Source.AART_CLI,
        artifact_id=place,
        artifact_type=kind,
        artifact_version=None,
        failure=Failure(
            fingerprint=fingerprint(kind, place, exception, frames, diagnostics),
            exception=exception,
            frames=frames,
            diagnostics=diagnostics,
            exit_code=draw(st.none() | st.integers(0, 255)),
            python=draw(st.sampled_from(("3.11.9", "3.12.4", "3.13.0rc1+"))),
            platform=draw(st.sampled_from(("darwin", "linux", "other"))),
        ),
    )


@st.composite
def events(
    draw: st.DrawFn, ids: st.SearchStrategy[str] = uuids, *, recurring: bool = False
) -> Event:
    kind = draw(st.sampled_from(list(EventType)))
    if kind is EventType.FAILED:
        return draw(failed(ids, recurring=recurring))
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
        event=draw(events(recurring=True)),
        client_id=draw(st.sampled_from(CLIENTS)),
        client_version=draw(st.sampled_from(("0.22.0", "0.24.0"))),
        received_at=NOW,
        automated=draw(st.booleans()),
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
    if event.failure is not None:
        document["failure"] = failure_document(event.failure)
    return document


def failure_document(failure: Failure) -> dict[str, Any]:
    document: dict[str, Any] = {"fingerprint": failure.fingerprint}
    if failure.exception is not None:
        document["exception"] = failure.exception
    document["frames"] = list(failure.frames)
    document["diagnostics"] = list(failure.diagnostics)
    if failure.exit_code is not None:
        document["exit_code"] = failure.exit_code
    document["python"] = failure.python
    document["platform"] = failure.platform
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
    "failed",
    "failure_document",
    "histories",
    "numbered",
    "request",
    "stored_events",
    "wire",
]
