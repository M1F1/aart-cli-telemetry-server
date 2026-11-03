"""Hypothesis strategies for what a client sends and what the server keeps."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

from hypothesis import strategies as st

from aart_cli_telemetry_server.domain.artifacts import EPOCH, Selection
from aart_cli_telemetry_server.domain.events import (
    CONTEXT_FOR,
    HARNESS_OF,
    SOURCES_FOR,
    Cause,
    Context,
    Environment,
    Event,
    EventType,
    Failure,
    Interface,
    Scope,
    Source,
    StoredEvent,
    Via,
    fingerprint,
)

_V = TypeVar("_V")

#: The moment every generated history ends at; a test's clock reads this.
NOW = datetime(2026, 10, 29, 18, 0, tzinfo=UTC)

uuids = st.uuids().map(str)
names = st.from_regex(r"[0-9A-Za-z._@:+-]{1,20}", fullmatch=True)
kinds = st.sampled_from(("mcp", "skill", "agent", "command", "plugin"))
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
    causes: tuple[Cause, ...] = ()
    if exception is not None and recurring and draw(st.booleans()):
        line = draw(st.integers(0, 999999))
        causes = (Cause("OSError", (f"{_FRAMES[2]}:{line}",)),)
    elif exception is not None and not recurring:
        causes = tuple(
            Cause(
                draw(named),
                tuple(
                    f"{frame}:{draw(st.integers(0, 999999))}"
                    for frame in draw(st.lists(st.sampled_from(_FRAMES), max_size=2))
                ),
            )
            for _ in range(draw(st.integers(0, 2)))
        )
    return Event(
        event_id=draw(ids),
        occurred_at=draw(moments),
        event_type=EventType.FAILED,
        source=Source.AART_CLI,
        artifact_id=place,
        artifact_type=kind,
        artifact_version=None,
        failure=Failure(
            fingerprint=fingerprint(kind, place, exception, frames, diagnostics, causes=causes),
            exception=exception,
            frames=frames,
            diagnostics=diagnostics,
            exit_code=draw(st.none() | st.integers(0, 255)),
            python=draw(st.sampled_from(("3.11.9", "3.12.4", "3.13.0rc1+"))),
            platform=draw(st.sampled_from(("darwin", "linux", "other"))),
            causes=causes,
        ),
    )


HARNESSES = ("claude", "opencode", "codex")
REGISTRIES = ("company", "local-1")


@st.composite
def contexts(draw: st.DrawFn, event_type: EventType, source: Source, artifact_type: str) -> Context:
    """A context the contract allows this event, each field seldom given."""

    def seldom(name: str, values: st.SearchStrategy[_V]) -> _V | None:
        allowed = event_type in CONTEXT_FOR[name]
        return draw(values) if allowed and draw(st.integers(0, 2)) == 0 else None

    harness = HARNESS_OF.get(source)
    via = seldom("via", st.sampled_from(list(Via)))
    collected = via is Via.COLLECTION and draw(st.booleans())
    return Context(
        harness=seldom(
            "harness", st.sampled_from(HARNESSES) if harness is None else st.just(harness)
        ),
        scope=seldom("scope", st.sampled_from(list(Scope))),
        registry=seldom("registry", st.sampled_from(REGISTRIES)),
        interface=seldom("interface", st.sampled_from(list(Interface))),
        previous_version=seldom("previous_version", st.sampled_from(("1.0.0", "2.1.0"))),
        via=via if via is not Via.COLLECTION or collected else Via.DIRECT,
        collection="starter-kit" if collected else None,
        collection_version=(draw(st.none() | st.just("3.0.0")) if collected else None),
        package_digest=seldom("package_digest", st.just("sha256:" + "ab" * 32)),
        duration_ms=seldom("duration_ms", st.integers(0, 90_000)),
        configuration_inputs=seldom("configuration_inputs", st.integers(0, 3)),
        credential_inputs=seldom("credential_inputs", st.integers(0, 2)),
        tool=(
            seldom("tool", st.sampled_from(("create_issue", "search")))
            if artifact_type == "mcp"
            else None
        ),
        session=seldom("session", st.sampled_from(SESSIONS)),
    )


SESSIONS = ("5a1f0c2e-1b2c-4d3e-8f40-516273849a5b", "6b2f1d3e-2c3d-4e4f-9a51-62738495ab6c")
environments = st.none() | st.builds(
    Environment,
    st.sampled_from(("darwin", "linux", "windows", "other")),
    st.sampled_from(("arm64", "x86_64")),
    st.sampled_from(("3.11.9", "3.12.4")),
)


@st.composite
def events(
    draw: st.DrawFn, ids: st.SearchStrategy[str] = uuids, *, recurring: bool = False
) -> Event:
    kind = draw(st.sampled_from(list(EventType)))
    if kind is EventType.FAILED:
        return draw(failed(ids, recurring=recurring))
    source = draw(st.sampled_from(sorted(SOURCES_FOR[kind])))
    artifact_type = draw(kinds)
    return Event(
        event_id=draw(ids),
        occurred_at=draw(moments),
        event_type=kind,
        source=source,
        artifact_id=draw(
            st.sampled_from(
                (
                    "github",
                    "jira-mcp",
                    "review",
                    "Zeta",
                    "alpha",
                    "superpowers",
                    "superpowers:brainstorming",
                    "superpowers:tdd",
                    "super_powers:tdd",
                )
            )
            | names
        ),
        artifact_type=artifact_type,
        artifact_version=None if kind is EventType.UNMANAGED_USED else draw(versions),
        context=draw(contexts(kind, source, artifact_type)),
    )


@st.composite
def stored_events(draw: st.DrawFn) -> StoredEvent:
    return StoredEvent(
        event=draw(events(recurring=True)),
        client_id=draw(st.sampled_from(CLIENTS)),
        client_version=draw(st.sampled_from(("0.22.0", "0.24.0"))),
        received_at=NOW,
        automated=draw(st.booleans()),
        environment=draw(environments),
    )


CLIENTS = (
    "0b6f4d1e-8f8a-4a43-9a52-2f0f5d0f6a11",
    "1c7a5e2f-9a9b-4b54-8b63-3a1a6e1a7b22",
    "2d8b6f3a-aaac-4c65-9c74-4b2b7f2b8c33",
)

histories = st.lists(stored_events(), max_size=40, unique_by=lambda stored: stored.event_id)


@st.composite
def selections(draw: st.DrawFn) -> Selection:
    """Any selection, leaning to the names, versions and moments histories hold, each filter
    seldom set, so most selections hold some of a history."""

    def seldom(values: st.SearchStrategy[_V], odds: int = 4) -> _V | None:
        return draw(values) if draw(st.integers(1, odds)) == 1 else None

    artifact_id = seldom(st.sampled_from(("github", "superpowers", "super_powers", "Zeta")) | names)
    since = draw(st.just(EPOCH) | moments)
    until = seldom(moments)
    return Selection(
        since=since,
        until=until if until is None or until > since else since + (since - until),
        event_type=seldom(st.sampled_from(list(EventType))),
        source=seldom(st.sampled_from(list(Source))),
        client_version=seldom(st.sampled_from(("0.22.0", "0.24.0", "9.9.9"))),
        automated=seldom(st.booleans()),
        artifact_type=seldom(kinds),
        artifact_id=artifact_id,
        members=artifact_id is not None and draw(st.booleans()),
        harness=seldom(st.sampled_from(HARNESSES), 10),
        scope=seldom(st.sampled_from(list(Scope)), 10),
        registry=seldom(st.sampled_from(REGISTRIES), 10),
        interface=seldom(st.sampled_from(list(Interface)), 10),
    )


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
    for name in event.context.given():
        value = getattr(event.context, name)
        document[name] = getattr(value, "value", value)
    return document


def failure_document(failure: Failure) -> dict[str, Any]:
    document: dict[str, Any] = {"fingerprint": failure.fingerprint}
    if failure.exception is not None:
        document["exception"] = failure.exception
    document["frames"] = list(failure.frames)
    if failure.causes:
        document["causes"] = [
            {"exception": cause.exception, "frames": list(cause.frames)} for cause in failure.causes
        ]
    document["diagnostics"] = list(failure.diagnostics)
    if failure.exit_code is not None:
        document["exit_code"] = failure.exit_code
    document["python"] = failure.python
    document["platform"] = failure.platform
    return document


def request(
    items: list[Event], client_id: str = CLIENTS[0], environment: Environment | None = None
) -> bytes:
    document: dict[str, Any] = {
        "schema_version": 1,
        "client_id": client_id,
        "client_version": "0.22.0",
        "events": [wire(item) for item in items],
    }
    if environment is not None:
        document["environment"] = {
            "os": environment.os,
            "arch": environment.arch,
            "python": environment.python,
        }
    return json.dumps(document).encode()


__all__ = [
    "CLIENTS",
    "HARNESSES",
    "NOW",
    "REGISTRIES",
    "SESSIONS",
    "Source",
    "contexts",
    "environments",
    "events",
    "failed",
    "failure_document",
    "histories",
    "numbered",
    "request",
    "selections",
    "stored_events",
    "wire",
]
