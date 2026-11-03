"""The MCP server at `/mcp`: an agent learns what it can ask, asks, and is refused what it cannot.

The answers are the read API's own documents; this holds the protocol around them -- the
handshake, the tools listed and their schemas, a tool's result and its errors, notifications, and
the requests refused before any tool runs.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aart_cli_telemetry_server.adapters.memory import InMemoryEventRepository
from aart_cli_telemetry_server.web.app import create_app
from aart_cli_telemetry_server.web.mcp import MAX_REQUEST_BYTES, PROTOCOL_VERSIONS

from .strategies import NOW
from .test_read_api import _superpowers

_TOOLS = {
    "summary",
    "breakdown",
    "list_artifacts",
    "get_artifact",
    "list_events",
    "client_versions",
    "failures",
    "unmanaged_tools",
    "daily",
}


@pytest.fixture
def client() -> TestClient:
    repository = InMemoryEventRepository()
    repository.add(_superpowers())
    return TestClient(create_app(repository, clock=lambda: NOW))


def _ask(client: TestClient, method: str, params: object = None, **headers: str) -> Any:
    message: dict[str, object] = {"jsonrpc": "2.0", "id": 7, "method": method}
    if params is not None:
        message["params"] = params
    return client.post("/mcp", json=message, headers=headers)


def _call(client: TestClient, name: str, **arguments: object) -> dict[str, Any]:
    answer = _ask(client, "tools/call", {"name": name, "arguments": arguments})
    assert answer.status_code == 200
    result: dict[str, Any] = answer.json()["result"]
    return result


def test_the_handshake_names_the_server_and_agrees_a_version(client: TestClient) -> None:
    for asked, agreed in (("2025-03-26", "2025-03-26"), ("1999-01-01", PROTOCOL_VERSIONS[0])):
        answer = _ask(client, "initialize", {"protocolVersion": asked, "capabilities": {}}).json()
        assert answer["id"] == 7
        assert answer["result"]["protocolVersion"] == agreed
        assert answer["result"]["capabilities"] == {"tools": {"listChanged": False}}
        assert answer["result"]["serverInfo"]["name"] == "aart-cli-telemetry"
    assert _ask(client, "ping").json()["result"] == {}


def test_every_tool_is_listed_read_only_with_a_closed_schema(client: TestClient) -> None:
    tools = _ask(client, "tools/list").json()["result"]["tools"]

    assert {tool["name"] for tool in tools} == _TOOLS
    for tool in tools:
        assert tool["annotations"]["readOnlyHint"] is True
        assert tool["inputSchema"]["type"] == "object"
        assert tool["inputSchema"]["additionalProperties"] is False
        assert tool["description"]
    detail = next(tool for tool in tools if tool["name"] == "get_artifact")
    assert set(detail["inputSchema"]["required"]) == {"artifact_type", "artifact_id"}


def test_a_tool_answers_what_the_read_api_answers(client: TestClient) -> None:
    for name, path, arguments in (
        ("list_artifacts", "/api/v1/artifacts?q=super&limit=2", {"q": "super", "limit": 2}),
        (
            "get_artifact",
            "/api/v1/artifacts/plugin/superpowers",
            {
                "artifact_type": "plugin",
                "artifact_id": "superpowers",
            },
        ),
        ("client_versions", "/api/v1/client-versions?automated=false", {"automated": False}),
        ("summary", "/api/v1/stats/summary?days=7", {"days": 7}),
        ("daily", "/api/v1/stats/daily?days=2", {"days": 2}),
        ("failures", "/api/v1/stats/failures", {}),
        ("unmanaged_tools", "/api/v1/stats/top-unmanaged?limit=3", {"limit": 3}),
    ):
        result = _call(client, name, **arguments)
        expected = client.get(path).json()
        assert result["isError"] is False, name
        assert result["structuredContent"] == expected, name
        assert json.loads(result["content"][0]["text"]) == expected, name


def test_events_page_through_the_tool_as_through_the_api(client: TestClient) -> None:
    pages: list[dict[str, Any]] = []
    before = None
    while before is not None or not pages:
        arguments: dict[str, object] = {"artifact_id": "superpowers", "members": True, "limit": 5}
        if before is not None:
            arguments["before"] = before
        pages.append(_call(client, "list_events", **arguments)["structuredContent"])
        before = pages[-1]["next_before"]

    seen = [event["event_id"] for page in pages for event in page["events"]]
    assert [len(page["events"]) for page in pages] == [5, 5, 5, 0]
    expected = client.get("/api/v1/events?artifact_id=superpowers&members=true&limit=500").json()
    assert seen == [event["event_id"] for event in expected["events"]]


@pytest.mark.parametrize(
    ("name", "arguments", "said"),
    [
        ("list_artifacts", {"sort": "size"}, "sort"),
        ("list_artifacts", {"limit": 0}, "limit"),
        ("list_artifacts", {"colour": "red"}, "colour"),
        ("get_artifact", {"artifact_type": "plugin"}, "artifact_id"),
        ("get_artifact", {"artifact_type": "plugin", "artifact_id": "nothing"}, "nothing"),
        ("list_events", {"members": True}, "members needs artifact_id"),
        ("list_events", {"before": "yesterday"}, "before"),
        ("summary", {"days": 0}, "days"),
    ],
)
def test_a_tool_asked_wrongly_says_why_as_its_result(
    client: TestClient, name: str, arguments: dict[str, object], said: str
) -> None:
    result = _call(client, name, **arguments)

    assert result["isError"] is True
    assert "structuredContent" not in result
    assert said in result["content"][0]["text"]


def test_what_is_not_a_tool_or_a_method_is_a_json_rpc_error(client: TestClient) -> None:
    unknown_tool = _ask(client, "tools/call", {"name": "delete_everything"}).json()
    assert unknown_tool["error"]["code"] == -32602
    assert _ask(client, "tools/call", ["list_artifacts"]).json()["error"]["code"] == -32602
    unknown_method = _ask(client, "resources/list").json()
    assert (unknown_method["id"], unknown_method["error"]["code"]) == (7, -32601)


def test_a_notification_is_accepted_without_an_answer(client: TestClient) -> None:
    answer = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})

    assert (answer.status_code, answer.content) == (202, b"")


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        (b"{not json", 400, -32700),
        (b"[]", 400, -32600),
        (b'{"jsonrpc": "1.0", "id": 1, "method": "ping"}', 400, -32600),
        (b" " * (MAX_REQUEST_BYTES + 1), 413, -32600),
    ],
)
def test_what_is_not_one_json_rpc_message_is_refused(
    client: TestClient, body: bytes, status: int, code: int
) -> None:
    answer = client.post("/mcp", content=body, headers={"content-type": "application/json"})

    assert answer.status_code == status
    assert answer.json()["error"]["code"] == code


def test_a_page_from_another_site_may_not_ask(client: TestClient) -> None:
    refused = _ask(client, "tools/list", origin="https://elsewhere.example.invalid")
    assert refused.status_code == 403
    same = _ask(client, "tools/list", origin="http://testserver")
    assert same.status_code == 200


def test_the_server_offers_no_stream(client: TestClient) -> None:
    answer = client.get("/mcp")

    assert answer.status_code == 405
    assert answer.headers["allow"] == "POST"
