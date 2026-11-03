"""The read API: what a person or an agent asks about artifacts, over HTTP.

The numbers are `domain.artifacts`' own, held there and in the repository contract; this holds what
the routes add: which events a query selects, what a plugin's detail holds, how events page, what
is refused, and that the API describes itself at `/docs`.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from aart_cli_telemetry_server.adapters.memory import InMemoryEventRepository
from aart_cli_telemetry_server.domain.events import Event, EventType, Source, StoredEvent
from aart_cli_telemetry_server.web.app import create_app

from .strategies import CLIENTS, NOW

_numbers = iter(range(1, 10_000))


def _event(
    kind: EventType,
    artifact_type: str,
    artifact_id: str,
    *,
    hours_ago: float = 1,
    client: str = CLIENTS[0],
    version: str | None = "6.2.0",
    client_version: str = "0.28.0",
    automated: bool = False,
) -> StoredEvent:
    source = Source.OPENCODE_PLUGIN if kind is EventType.USED else Source.AART_CLI
    return StoredEvent(
        Event(
            f"00000000-0000-4000-8000-{next(_numbers):012d}",
            NOW - timedelta(hours=hours_ago),
            kind,
            source,
            artifact_id,
            artifact_type,
            version,
        ),
        client,
        client_version,
        NOW,
        automated,
    )


def _superpowers() -> list[StoredEvent]:
    """Three installations install superpowers and its two skills; one removes them all; two use
    a skill; a CI job installs an MCP on another version of aart-cli."""

    held: list[StoredEvent] = []
    for client in CLIENTS:
        held += [
            _event(EventType.INSTALLED, "plugin", "superpowers", hours_ago=30, client=client),
            _event(EventType.INSTALLED, "skill", "superpowers:tdd", hours_ago=30, client=client),
            _event(
                EventType.INSTALLED,
                "skill",
                "superpowers:brainstorming",
                hours_ago=30,
                client=client,
            ),
        ]
    held += [
        _event(EventType.REMOVED, "plugin", "superpowers", hours_ago=2, client=CLIENTS[2]),
        _event(EventType.REMOVED, "skill", "superpowers:tdd", hours_ago=2, client=CLIENTS[2]),
        _event(
            EventType.REMOVED, "skill", "superpowers:brainstorming", hours_ago=2, client=CLIENTS[2]
        ),
        _event(EventType.USED, "skill", "superpowers:tdd", hours_ago=3),
        _event(EventType.USED, "skill", "superpowers:tdd", hours_ago=1, client=CLIENTS[1]),
        _event(EventType.USED, "skill", "superpowers:brainstorming", hours_ago=1),
        _event(
            EventType.INSTALLED,
            "mcp",
            "github",
            hours_ago=5,
            version="1.0.0",
            client_version="0.27.1",
            automated=True,
        ),
    ]
    return held


@pytest.fixture
def client() -> TestClient:
    repository = InMemoryEventRepository()
    repository.add(_superpowers())
    return TestClient(create_app(repository, clock=lambda: NOW))


def _names(answer: dict[str, list[dict[str, object]]], key: str = "artifacts") -> list[object]:
    return [row["artifact_id"] for row in answer[key]]


def test_every_installed_artifact_is_listed_by_name_used_or_not(client: TestClient) -> None:
    found = client.get("/api/v1/artifacts").json()

    # Installs first, then uses with a plugin's skills' uses, then name.
    assert _names(found) == [
        "superpowers",
        "superpowers:tdd",
        "superpowers:brainstorming",
        "github",
    ]
    plugin = found["artifacts"][0]
    assert {
        key: plugin[key]
        for key in ("artifact_type", "plugin", "latest_version", "installs", "removals")
    } == {
        "artifact_type": "plugin",
        "plugin": None,
        "latest_version": "6.2.0",
        "installs": 3,
        "removals": 1,
    }
    assert (plugin["holders"], plugin["uses"], plugin["member_uses"]) == (2, 0, 3)
    tdd = found["artifacts"][1]
    assert (tdd["plugin"], tdd["installs"], tdd["holders"], tdd["uses"], tdd["used_by"]) == (
        "superpowers",
        3,
        2,
        2,
        2,
    )


def test_a_list_is_narrowed_by_name_kind_order_and_who_sent(client: TestClient) -> None:
    def names(query: str) -> list[object]:
        return _names(client.get(f"/api/v1/artifacts?{query}").json())

    assert names("q=TDD") == ["superpowers:tdd"]
    assert names("artifact_type=skill&sort=uses") == [
        "superpowers:tdd",
        "superpowers:brainstorming",
    ]
    assert names("sort=name&limit=2") == ["github", "superpowers"]
    assert names("automated=true") == ["github"]
    assert names("client_version=0.27.1") == ["github"]
    # Installed a day ago and removed in the last day: the day holds only the removal and uses.
    one_day = client.get("/api/v1/artifacts?days=1&artifact_type=plugin").json()["artifacts"]
    assert [(row["installs"], row["removals"], row["holders"]) for row in one_day] == [(0, 1, 2)]


def test_a_plugin_s_detail_holds_its_versions_days_sources_and_skills(client: TestClient) -> None:
    found = client.get("/api/v1/artifacts/plugin/superpowers").json()

    assert (found["installs"], found["holders"], found["member_uses"]) == (3, 2, 3)
    assert found["versions"][0]["artifact_version"] == "6.2.0"
    assert [day["installs"] for day in found["daily"]] == [3, 0]
    assert found["sources"] == [{"source": "aart_cli", "events": 4}]
    assert [(row["artifact_id"], row["uses"]) for row in found["members"]] == [
        ("superpowers:brainstorming", 1),
        ("superpowers:tdd", 2),
    ]
    skill = client.get("/api/v1/artifacts/skill/superpowers:tdd").json()
    assert (skill["plugin"], skill["uses"], skill["members"]) == ("superpowers", 2, [])


def test_an_artifact_nothing_is_about_is_not_found(client: TestClient) -> None:
    assert client.get("/api/v1/artifacts/plugin/nothing").status_code == 404
    assert client.get("/api/v1/artifacts/mcp/superpowers").status_code == 404


def test_events_page_newest_first_and_follow_their_cursor(client: TestClient) -> None:
    query = "/api/v1/events?artifact_id=superpowers&members=true&limit=4"
    first = client.get(query).json()
    second = client.get(f"{query}&before={first['next_before']}").json()

    seen = first["events"] + second["events"]
    assert len({event["event_id"] for event in seen}) == len(seen) == 8
    moments = [event["occurred_at"] for event in seen]
    assert moments == sorted(moments, reverse=True)
    assert first["events"][0]["event_type"] == "artifact.used"
    assert {"client_id", "client_version", "automated", "source"} <= set(first["events"][0])
    rest = client.get(f"{query}&before={second['next_before']}").json()
    assert len(rest["events"]) == 4
    assert client.get(f"{query}&limit=500").json()["next_before"] is None


def test_events_of_a_type_and_without_members(client: TestClient) -> None:
    used = client.get("/api/v1/events?event_type=artifact.used").json()["events"]
    assert len(used) == 3
    own = client.get("/api/v1/events?artifact_id=superpowers").json()["events"]
    assert {event["artifact_id"] for event in own} == {"superpowers"}


def test_client_versions_say_how_many_installations_sent_from_each(client: TestClient) -> None:
    found = client.get("/api/v1/client-versions").json()["client_versions"]

    assert [(row["client_version"], row["clients"], row["events"]) for row in found] == [
        ("0.28.0", 3, 15),
        ("0.27.1", 1, 1),
    ]


@pytest.mark.parametrize(
    "query",
    [
        "/api/v1/events?members=true",
        "/api/v1/events?before=yesterday",
        "/api/v1/events?before=2026-10-29T17:00:00.000000Z,not-an-id",
        "/api/v1/events?limit=501",
        "/api/v1/artifacts?sort=size",
        "/api/v1/artifacts?days=0",
        "/api/v1/artifacts?source=somewhere",
    ],
)
def test_a_query_that_cannot_be_read_is_refused(client: TestClient, query: str) -> None:
    assert client.get(query).status_code == 422


def test_the_api_describes_itself(client: TestClient) -> None:
    described = client.get("/openapi.json").json()

    for path in (
        "/api/v1/artifacts",
        "/api/v1/artifacts/{artifact_type}/{artifact_id}",
        "/api/v1/events",
        "/api/v1/client-versions",
        "/api/v1/stats/summary",
        "/api/v1/events/batch",
    ):
        assert path in described["paths"], path
    assert "/dashboard" not in described["paths"]
    assert client.get("/docs").status_code == 200
