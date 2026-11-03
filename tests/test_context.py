"""Revision 7: where and how an event happened, and what its request was sent from.

The contract takes each field only in the event types that can say it and refuses the rest by
name; what it takes is kept with the event, listed with it, and selects and groups the read API's
answers and the MCP's.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st
from returns.result import Failure, Success

from aart_cli_telemetry_server.adapters.memory import InMemoryEventRepository
from aart_cli_telemetry_server.domain.artifacts import Group, ordered_groups
from aart_cli_telemetry_server.domain.contract import read_batch
from aart_cli_telemetry_server.domain.events import (
    CONTEXT_FOR,
    Context,
    Environment,
    Event,
    EventType,
)
from aart_cli_telemetry_server.web.app import create_app

from .strategies import CLIENTS, NOW, events, request

_DIGEST = "sha256:" + "0f" * 32
_SESSION = "5a1f0c2e-1b2c-4d3e-8f40-516273849a5b"
#: One value the contract takes for each field.
_VALUES: dict[str, object] = {
    "harness": "opencode",
    "scope": "user",
    "registry": "company",
    "interface": "tui",
    "previous_version": "6.1.0",
    "via": "plugin",
    "collection": "starter-kit",
    "collection_version": "3.0.0",
    "package_digest": _DIGEST,
    "duration_ms": 1500,
    "configuration_inputs": 2,
    "credential_inputs": 1,
    "tool": "create_issue",
    "session": _SESSION,
}


def _event(number: int, event_type: str, **fields: object) -> dict[str, object]:
    source = {
        "artifact.used": "opencode_plugin",
        "unmanaged.used": "opencode_plugin",
    }.get(event_type, "aart_cli")
    document: dict[str, object] = {
        "event_id": f"00000000-0000-4000-8000-{number:012d}",
        "occurred_at": "2026-10-29T17:00:00Z",
        "event_type": event_type,
        "source": source,
        "artifact_id": "github",
        "artifact_type": "mcp",
    }
    if event_type != "unmanaged.used":
        document["artifact_version"] = "6.2.0"
    return document | fields


def _body(*items: dict[str, object], **envelope: object) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "client_id": CLIENTS[0],
            "client_version": "0.29.0",
            "events": list(items),
            **envelope,
        }
    ).encode()


def _reasons(raw: bytes) -> list[str]:
    match read_batch(raw):
        case Failure(refusal):
            return [item.reason for item in refusal.rejected_events] or [refusal.error]
    raise AssertionError("accepted")


@given(st.lists(events(), min_size=1, max_size=5, unique_by=lambda event: event.event_id))
def test_every_context_the_contract_allows_is_read_back_as_sent(items: list[Event]) -> None:
    environment = Environment("darwin", "arm64", "3.11.9")

    match read_batch(request(items, environment=environment)):
        case Success(batch):
            assert [event.context for event in batch.events] == [item.context for item in items]
            assert batch.environment == environment
        case Failure(refusal):
            raise AssertionError(refusal)


_LIFECYCLE = {"artifact.installed", "artifact.updated", "artifact.removed"}
_PLACED = {"artifact.installed", "artifact.updated"}
_USES = {"artifact.used", "unmanaged.used"}
#: The contract's table "Where and how (revision 7)", written out rather than read from the code.
_HELD_IN = {
    "harness": _LIFECYCLE | _USES,
    "scope": _LIFECYCLE | {"artifact.used"},
    "registry": _LIFECYCLE | {"artifact.used"},
    "interface": _LIFECYCLE,
    "previous_version": {"artifact.updated"},
    "via": _LIFECYCLE,
    "collection": _LIFECYCLE,
    "collection_version": _LIFECYCLE,
    "package_digest": _PLACED,
    "duration_ms": _LIFECYCLE,
    "configuration_inputs": _PLACED,
    "credential_inputs": _PLACED,
    "tool": _USES,
    "session": _USES,
}


def test_the_table_names_every_field_and_the_code_holds_it() -> None:
    assert set(_HELD_IN) == set(_VALUES) == set(CONTEXT_FOR)
    assert {name: {item.value for item in held} for name, held in CONTEXT_FOR.items()} == _HELD_IN


@pytest.mark.parametrize("name", sorted(_VALUES))
@pytest.mark.parametrize(
    "event_type", [item.value for item in EventType if item.value != "aart_cli.failed"]
)
def test_a_field_is_taken_exactly_in_the_event_types_that_can_say_it(
    name: str, event_type: str
) -> None:
    fields: dict[str, object] = {name: _VALUES[name]}
    if name in ("collection", "collection_version"):
        fields |= {"via": "collection", "collection": "starter-kit"}
    raw = _body(_event(1, event_type, **fields))

    allowed = event_type in _HELD_IN[name] and (
        name not in ("collection", "collection_version") or event_type in _HELD_IN["via"]
    )
    assert isinstance(read_batch(raw), Success) == allowed
    if not allowed:
        assert _reasons(raw) == ["this event_type does not hold that field"]


@pytest.mark.parametrize("name", sorted(_VALUES))
def test_a_failure_holds_no_context(name: str) -> None:
    failed = _event(1, "aart_cli.failed", **{name: _VALUES[name]})
    failed |= {"source": "aart_cli", "artifact_type": "aart-cli", "artifact_id": "aart-cli"}

    assert "this event_type does not hold that field" in _reasons(_body(failed))


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"collection": "starter-kit"}, "a collection is named exactly when via is collection"),
        ({"via": "collection", "collection_version": "1.0.0"}, "a collection is named"),
        ({"harness": "claude"}, "a use's harness is its source's"),
        ({"scope": "global"}, "scope must be user or project"),
        ({"registry": "Company"}, "registry must be a lowercase slug of 1-64 characters"),
        ({"harness": "a" * 65}, "harness must be a lowercase slug of 1-64 characters"),
        ({"session": "ses_123"}, "session must be a lowercase UUID"),
        ({"duration_ms": -1}, "duration_ms must be an integer from 0 to 86400000"),
        ({"duration_ms": 1.5}, "duration_ms must be an integer"),
        ({"package_digest": "md5:abc"}, "package_digest must be sha256:"),
        ({"surname": "x"}, "a field the contract does not have"),
    ],
)
def test_a_context_the_contract_does_not_allow_is_refused_and_says_why(
    fields: dict[str, object], reason: str
) -> None:
    event_type = (
        "artifact.used" if "harness" in fields or "session" in fields else "artifact.installed"
    )
    (said,) = _reasons(_body(_event(1, event_type, **fields)))

    assert said.startswith(reason)


def test_a_tool_is_only_an_mcp_s() -> None:
    skill = _event(1, "artifact.used", tool="search") | {"artifact_type": "skill"}

    assert _reasons(_body(skill)) == ["a tool is an mcp's"]


@pytest.mark.parametrize(
    "environment",
    [
        {"os": "darwin", "arch": "arm64"},
        {"os": "plan9", "arch": "arm64", "python": "3.11.9"},
        {"os": "darwin", "arch": "arm64", "python": "3.11.9", "host": "x"},
    ],
)
def test_an_environment_is_os_arch_and_python_or_the_request_is_refused(
    environment: dict[str, str],
) -> None:
    assert _reasons(_body(_event(1, "artifact.installed"), environment=environment)) == [
        "environment must be the contract's os, arch and python"
    ]


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(InMemoryEventRepository(), clock=lambda: NOW))


def _send(client: TestClient, raw: bytes, client_id: str = CLIENTS[0]) -> None:
    body = json.loads(raw) | {"client_id": client_id}
    answer = client.post("/api/v1/events/batch", content=json.dumps(body).encode())
    assert answer.status_code == 200, answer.text


def _history(client: TestClient) -> None:
    """Two people install github from the TUI and the CLI in opencode; CI installs it for
    claude in a project; one person uses one of its tools."""

    _send(
        client,
        _body(
            _event(
                1,
                "artifact.installed",
                harness="opencode",
                scope="user",
                registry="company",
                interface="tui",
                package_digest=_DIGEST,
                duration_ms=900,
            ),
            _event(
                2,
                "artifact.used",
                scope="user",
                registry="company",
                tool="create_issue",
                session=_SESSION,
            ),
            environment={"os": "darwin", "arch": "arm64", "python": "3.11.9"},
        ),
    )
    _send(
        client,
        _body(
            _event(
                3,
                "artifact.installed",
                harness="opencode",
                scope="user",
                registry="company",
                interface="cli",
            )
        ),
        CLIENTS[1],
    )
    _send(
        client,
        _body(
            _event(
                4,
                "artifact.installed",
                harness="claude",
                scope="project",
                registry="local-1",
                interface="cli",
            ),
            automated=True,
        ),
        CLIENTS[2],
    )


def test_a_kept_event_is_listed_with_what_it_said(client: TestClient) -> None:
    _history(client)

    listed = {
        event["event_id"][-1]: event for event in client.get("/api/v1/events").json()["events"]
    }
    installed, used, bare = listed["1"], listed["2"], listed["3"]
    assert {key: installed[key] for key in ("harness", "scope", "interface", "package_digest")} == {
        "harness": "opencode",
        "scope": "user",
        "interface": "tui",
        "package_digest": _DIGEST,
    }
    assert (installed["os"], installed["duration_ms"]) == ("darwin", 900)
    assert (used["tool"], used["session"], used["harness"]) == ("create_issue", _SESSION, None)
    assert (bare["os"], bare["via"], bare["tool"]) == (None, None, None)


def _groups(client: TestClient, query: str) -> list[dict[str, Any]]:
    answer = client.get(f"/api/v1/breakdown?{query}")
    assert answer.status_code == 200, answer.text
    groups: list[dict[str, Any]] = answer.json()["groups"]
    return groups


def test_a_breakdown_groups_by_what_events_said_and_counts_installations(
    client: TestClient,
) -> None:
    _history(client)

    assert _groups(client, "by=harness&event_type=artifact.installed") == [
        {"harness": "opencode", "events": 2, "clients": 2},
        {"harness": "claude", "events": 1, "clients": 1},
    ]
    # A use that names no harness is its source's.
    assert _groups(client, "by=harness&event_type=artifact.used") == [
        {"harness": "opencode", "events": 1, "clients": 1}
    ]
    assert _groups(client, "by=interface&by=automated&event_type=artifact.installed") == [
        {"interface": "cli", "automated": "false", "events": 1, "clients": 1},
        {"interface": "cli", "automated": "true", "events": 1, "clients": 1},
        {"interface": "tui", "automated": "false", "events": 1, "clients": 1},
    ]
    assert _groups(client, "by=scope&automated=false") == [
        {"scope": "user", "events": 3, "clients": 2}
    ]


def test_groups_come_most_events_first_then_most_installations_then_not_said_first() -> None:
    groups = [
        Group(("opencode",), 2, 2),
        Group(("claude",), 5, 1),
        Group((None,), 2, 2),
        Group(("codex",), 2, 3),
        Group(("claude",), 2, 2),
    ]

    assert ordered_groups(groups, 4) == (
        Group(("claude",), 5, 1),
        Group(("codex",), 2, 3),
        Group((None,), 2, 2),
        Group(("claude",), 2, 2),
    )


@pytest.mark.parametrize(
    ("query", "count"),
    [
        ("harness=opencode", 3),
        ("harness=claude", 1),
        ("scope=project", 1),
        ("registry=company", 3),
        ("interface=tui", 1),
        ("automated=false&interface=cli", 1),
    ],
)
def test_every_list_is_narrowed_by_harness_scope_registry_and_interface(
    client: TestClient, query: str, count: int
) -> None:
    _history(client)

    assert len(client.get(f"/api/v1/events?{query}").json()["events"]) == count


@pytest.mark.parametrize(
    "query",
    [
        "/api/v1/breakdown",
        "/api/v1/breakdown?by=colour",
        "/api/v1/breakdown?by=harness&by=harness",
        "/api/v1/breakdown?by=harness&by=scope&by=via&by=os",
        "/api/v1/breakdown?by=harness&members=true",
        "/api/v1/events?scope=global",
    ],
)
def test_a_breakdown_that_cannot_be_read_is_refused(client: TestClient, query: str) -> None:
    assert client.get(query).status_code == 422


def test_the_mcp_breaks_down_as_the_api_does(client: TestClient) -> None:
    _history(client)

    answer = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "breakdown",
                "arguments": {"by": ["harness", "interface"], "automated": False},
            },
        },
    ).json()["result"]

    assert answer["isError"] is False
    expected = client.get("/api/v1/breakdown?by=harness&by=interface&automated=false").json()
    assert answer["structuredContent"] == expected


def test_a_context_is_kept_as_the_request_that_first_sent_it_said(client: TestClient) -> None:
    first = _event(1, "artifact.installed", harness="opencode")
    _send(client, _body(first))
    _send(client, _body(first | {"harness": "claude"}))

    (event,) = client.get("/api/v1/events").json()["events"]
    assert event["harness"] == "opencode"


def test_an_event_with_no_context_is_unchanged() -> None:
    match read_batch(_body(_event(1, "artifact.installed"))):
        case Success(batch):
            assert batch.events[0].context == Context()
            assert batch.environment is None
        case Failure(refusal):
            raise AssertionError(refusal)
