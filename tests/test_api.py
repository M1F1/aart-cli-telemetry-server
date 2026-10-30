"""The HTTP shell: stored once, acknowledged only after commit, refused readably."""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from returns.io import IOFailure, IOResult

from aart_cli_telemetry_server.adapters.memory import InMemoryEventRepository
from aart_cli_telemetry_server.adapters.sql import SqlEventRepository, open_repository, sqlite_url
from aart_cli_telemetry_server.application.ports import StorageError
from aart_cli_telemetry_server.domain.contract import MAX_BODY_BYTES
from aart_cli_telemetry_server.domain.events import StoredEvent
from aart_cli_telemetry_server.effects import perform
from aart_cli_telemetry_server.web.app import create_app

from .strategies import NOW

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = json.loads((ROOT / "contract" / "telemetry-api-v1.examples.json").read_text("utf-8"))
BATCH = "/api/v1/events/batch"


def _request(index: int = 1, moment: str = "2026-10-29T17:00:00Z") -> dict[str, Any]:
    document: dict[str, Any] = copy.deepcopy(EXAMPLES["valid"][index]["request"])
    for event in document["events"]:
        event["occurred_at"] = moment
    return document


@pytest.fixture
def repository(tmp_path: Path) -> Iterator[SqlEventRepository]:
    opened = perform(open_repository(sqlite_url(str(tmp_path / "data")))).unwrap()
    yield opened
    opened.engine.dispose()


@pytest.fixture
def client(repository: SqlEventRepository) -> TestClient:
    return TestClient(create_app(repository, clock=lambda: NOW))


def _kept(client: TestClient) -> int:
    summary = client.get("/api/v1/stats/summary?days=366").json()
    return int(
        summary["uses"]
        + summary["unmanaged_uses"]
        + summary["installs"]
        + summary["updates"]
        + summary["removals"]
    )


def test_a_batch_is_stored_and_every_id_acknowledged(client: TestClient) -> None:
    document = _request()
    response = client.post(BATCH, json=document)

    assert response.status_code == 200
    assert response.json() == {
        "acknowledged_event_ids": [event["event_id"] for event in document["events"]]
    }
    assert _kept(client) == 3


def test_the_same_batch_twice_counts_once(client: TestClient) -> None:
    document = _request()
    first = client.post(BATCH, json=document)
    second = client.post(BATCH, json=document)

    assert first.json() == second.json()
    summary = client.get("/api/v1/stats/summary").json()
    assert (summary["installs"], summary["uses"], summary["removals"]) == (1, 1, 1)


def test_an_invalid_event_stores_nothing_and_is_named(client: TestClient) -> None:
    document = _request()
    document["events"].append(EXAMPLES["invalid"][0]["event"])
    response = client.post(BATCH, json=document)

    assert response.status_code == 422
    body = response.json()
    assert [item["index"] for item in body["rejected_events"]] == [3]
    assert body["rejected_events"][0]["event_id"] == EXAMPLES["invalid"][0]["event"]["event_id"]
    assert "/Users/someone" not in response.text
    assert _kept(client) == 0


def test_not_json_is_400_and_too_large_is_413(client: TestClient) -> None:
    assert client.post(BATCH, content=b"{not json").status_code == 400
    assert client.post(BATCH, content=b"\xff\xfe").status_code == 400
    big = b"[" + b" " * MAX_BODY_BYTES + b"]"
    refused = client.post(BATCH, content=big)
    assert (refused.status_code, "rejected_events" in refused.json()) == (413, False)
    declared = client.post(BATCH, content=b"{}", headers={"Content-Length": "abc"})
    assert declared.status_code in {400, 413}


def test_concurrent_batches_lose_nothing(client: TestClient) -> None:
    def send(number: int) -> int:
        document = _request(0)
        document["events"][0]["event_id"] = f"00000000-0000-4000-8000-{number:012d}"
        document["events"][0]["occurred_at"] = "2026-10-29T17:00:00Z"
        return int(client.post(BATCH, json=document).status_code)

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(send, range(64)))

    assert statuses == [200] * 64
    assert _kept(client) == 64


def test_stats_separate_installs_from_uses(client: TestClient) -> None:
    client.post(BATCH, json=_request())
    top = client.get("/api/v1/stats/top-artifacts").json()["artifacts"]
    daily = client.get("/api/v1/stats/daily").json()["daily"]
    sources = client.get("/api/v1/stats/by-source").json()["by_source"]

    assert top == [
        {
            "artifact_type": "mcp",
            "artifact_id": "jira-mcp",
            "uses": 1,
            "clients": 1,
            "installs": 1,
            "last_used": "2026-10-29T17:00:00.000000Z",
        }
    ]
    assert daily == [
        {
            "day": "2026-10-29",
            "uses": 1,
            "installs": 1,
            "updates": 0,
            "removals": 1,
            "active_clients": 1,
        }
    ]
    assert sources == [{"source": "opencode_plugin", "artifact_type": "mcp", "uses": 1}]


def test_uses_of_tools_aart_cli_did_not_install_have_their_own_list(client: TestClient) -> None:
    client.post(BATCH, json=_request())
    client.post(BATCH, json=_request(2))

    top = client.get("/api/v1/stats/top-unmanaged").json()["tools"]
    summary = client.get("/api/v1/stats/summary").json()

    assert top == [
        {
            "artifact_type": "mcp",
            "artifact_id": "context7_resolve-library-id",
            "uses": 1,
            "clients": 1,
            "last_used": "2026-10-29T17:00:00.000000Z",
        },
        {
            "artifact_type": "skill",
            "artifact_id": "superpowers:brainstorming",
            "uses": 1,
            "clients": 1,
            "last_used": "2026-10-29T17:00:00.000000Z",
        },
    ]
    assert (summary["uses"], summary["unmanaged_uses"]) == (1, 2)
    assert [
        item["artifact_id"]
        for item in client.get("/api/v1/stats/top-artifacts").json()["artifacts"]
    ] == ["jira-mcp"]
    assert client.get("/api/v1/stats/top-unmanaged?limit=201").status_code == 422


def test_old_events_fall_outside_the_period_and_a_period_has_bounds(client: TestClient) -> None:
    client.post(BATCH, json=_request(moment="2020-01-01T00:00:00Z"))
    summary = client.get("/api/v1/stats/summary?days=7").json()
    assert (summary["days"], summary["uses"]) == (7, 0)
    for query in ("summary?days=0", "summary?days=367", "top-artifacts?limit=201"):
        assert client.get(f"/api/v1/stats/{query}").status_code == 422


def test_health_and_dashboard(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/", follow_redirects=False).headers["location"] == "/dashboard"
    page = client.get("/dashboard")
    assert page.status_code == 200
    assert "script-src 'self'" in page.headers["content-security-policy"]
    assert "https://" not in page.text
    for name in ("dashboard.js", "dashboard.css", "chart.umd.min.js"):
        assert client.get(f"/static/{name}").status_code == 200
    assert client.get("/static/..%2Fapp.py").status_code == 404
    assert client.get("/static/chart.js.LICENSE.md").status_code == 404


class _Broken(InMemoryEventRepository):
    """A database that is down."""

    def add(self, events: Sequence[StoredEvent]) -> IOResult[None, StorageError]:
        return IOFailure(StorageError("OperationalError"))

    def ping(self) -> IOResult[None, StorageError]:
        return IOFailure(StorageError("OperationalError"))

    def summary(self, period: Any) -> Any:
        return IOFailure(StorageError("OperationalError"))


def test_a_database_that_is_down_is_503_and_never_an_acknowledgement() -> None:
    client = TestClient(create_app(_Broken(), clock=lambda: NOW))

    stored = client.post(BATCH, json=_request())
    assert (stored.status_code, stored.json()) == (503, {"error": "the database is unavailable"})
    assert client.get("/healthz").status_code == 503
    assert client.get("/api/v1/stats/summary").status_code == 503
