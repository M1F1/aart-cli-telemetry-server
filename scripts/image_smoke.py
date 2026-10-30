"""Check a built container image the way a deployment uses it.

`python scripts/image_smoke.py IMAGE` needs Docker and nothing else. Every container and volume it
makes is named after this run and removed at the end.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

EXAMPLES = Path(__file__).resolve().parent.parent / "contract" / "telemetry-api-v1.examples.json"
PROXY = "--behind-authenticating-proxy"
POSTGRES_IMAGE = "postgres:16-alpine"


def docker(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *arguments], capture_output=True, text=True, check=check)


def request(port: int, path: str, body: object | None = None) -> tuple[int, Any]:
    data = None if body is None else json.dumps(body).encode()
    sent = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(sent, timeout=5) as answer:
            return answer.status, json.loads(answer.read())
    except urllib.error.HTTPError as refused:
        return refused.code, json.loads(refused.read() or b"null")


def batch() -> dict[str, Any]:
    """The contract's first valid example, with fresh ids and the current moment."""

    example: dict[str, Any] = json.loads(EXAMPLES.read_text())["valid"][0]["request"]
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    example["client_id"] = str(uuid.uuid4())
    example["events"] = [
        {**event, "event_id": str(uuid.uuid4()), "occurred_at": now} for event in example["events"]
    ]
    return example


@contextmanager
def server(image: str, name: str, *options: str) -> Iterator[int]:
    """A container as a deployment runs it, with its port published to loopback; `options` are
    `docker run`'s, such as the volume or the database URL."""

    docker(
        "run", "--detach", "--name", name, "--read-only", *options,
        "--publish", "127.0.0.1::8000",
        "--health-interval", "1s", "--health-start-period", "1s",
        image, PROXY,
    )  # fmt: skip
    try:
        port = int(docker("port", name, "8000/tcp").stdout.strip().rsplit(":", 1)[1])
        for _ in range(60):
            health = docker("inspect", "--format", "{{.State.Health.Status}}", name).stdout
            if health.strip() == "healthy":
                break
            time.sleep(0.5)
        else:
            raise AssertionError(f"never healthy:\n{docker('logs', name).stderr}")
        yield port
    finally:
        docker("rm", "--force", name, check=False)


def main(image: str) -> int:
    run = uuid.uuid4().hex[:8]
    volume = f"aart-cli-telemetry-smoke-{run}"
    docker("volume", "create", volume)
    try:
        refused = docker("run", "--rm", image, check=False)
        assert refused.returncode == 2, refused
        assert "refusing to listen on 0.0.0.0" in refused.stderr, refused.stderr
        print("ok: without --behind-authenticating-proxy the image refuses to listen")

        driver = docker("run", "--rm", "--entrypoint", "python", image, "-c", "import psycopg")
        assert driver.returncode == 0
        print("ok: the PostgreSQL driver is in the image")

        sent = batch()
        with server(image, f"{volume}-first", "--volume", f"{volume}:/data") as port:
            status, answer = request(port, "/api/v1/events/batch", sent)
            assert status == 200, (status, answer)
            expected = [event["event_id"] for event in sent["events"]]
            assert answer == {"acknowledged_event_ids": expected}, answer
            print(f"ok: a read-only container as uid 10001 stored {len(expected)} event(s)")

            user = docker("exec", f"{volume}-first", "id", "-u").stdout.strip()
            assert user == "10001", user
            mode = docker(
                "exec", f"{volume}-first", "stat", "-c", "%a %u", "/data/telemetry.sqlite3"
            ).stdout.strip()
            assert mode == "600 10001", mode
            print("ok: the database is 0600 and owned by the server's user")

            copy = docker(
                "exec", f"{volume}-first",
                "aart-cli-telemetry-server", "backup", "/data/backup.sqlite3",
            )  # fmt: skip
            assert copy.stdout.strip() == "/data/backup.sqlite3", copy
            print("ok: backup runs in the container")

        with server(image, f"{volume}-second", "--volume", f"{volume}:/data") as port:
            status, summary = request(port, "/api/v1/stats/summary?days=1")
            assert status == 200, (status, summary)
            assert summary["uses"] == len(expected), summary
            status, page = request(port, "/healthz")
            assert (status, page) == (200, {"status": "ok"}), (status, page)
            print("ok: a new container on the same volume serves what the first stored")

        postgresql(image, run)
    finally:
        docker("volume", "rm", "--force", volume, check=False)
    return 0


def postgresql(image: str, run: str) -> None:
    """The image on PostgreSQL named by `AART_CLI_TELEMETRY_DATABASE_URL`, on a network of its own.

    The database trusts the network, so no password is made up here or shown anywhere.
    """

    network = f"aart-cli-telemetry-smoke-{run}"
    database = f"{network}-postgres"
    docker("network", "create", network)
    try:
        docker(
            "run", "--detach", "--name", database, "--network", network,
            "--env", "POSTGRES_USER=telemetry", "--env", "POSTGRES_DB=telemetry",
            "--env", "POSTGRES_HOST_AUTH_METHOD=trust", POSTGRES_IMAGE,
        )  # fmt: skip
        for _ in range(120):
            # Over TCP: the image's first, initialising server listens only on its socket.
            ready = docker("exec", database, "pg_isready", "-h", "127.0.0.1", check=False)
            if ready.returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise AssertionError(f"PostgreSQL never ready:\n{docker('logs', database).stderr}")
        url = f"postgresql+psycopg://telemetry@{database}:5432/telemetry"
        sent = batch()
        with server(
            image, f"{network}-server", "--network", network,
            "--env", f"AART_CLI_TELEMETRY_DATABASE_URL={url}",
        ) as port:  # fmt: skip
            status, answer = request(port, "/api/v1/events/batch", sent)
            assert status == 200, (status, answer)
            status, summary = request(port, "/api/v1/stats/summary?days=1")
            assert summary["uses"] == len(sent["events"]), summary
        rows = docker(
            "exec", database, "psql", "-U", "telemetry", "-tAc", "select count(*) from event"
        ).stdout.strip()
        assert rows == str(len(sent["events"])), rows
        print("ok: with AART_CLI_TELEMETRY_DATABASE_URL the image stores in PostgreSQL")
    finally:
        docker("rm", "--force", database, check=False)
        docker("network", "rm", network, check=False)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "aart-cli-telemetry-server:dev"))
