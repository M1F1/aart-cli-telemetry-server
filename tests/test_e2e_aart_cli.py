"""End to end with a real aart-cli: its outbox, its sender, this server, this database.

Skipped unless `AART_CLI_PYTHON` names a Python interpreter that imports `aart_cli` (for example
an aart-cli checkout's virtual environment). It runs this server on a free loopback port, turns
telemetry on in a throwaway `AART_CLI_HOME`, puts lifecycle events in aart-cli's own outbox the way
a completed install does, sends them with `aart-cli consume telemetry sync`, sends them again, and
reads them back from this server's database once each. A second test keeps a use of an MCP no
artifact answers to, sends it as `unmanaged.used`, and after `consume telemetry unmanaged off` sends
the next one no more. Nothing touches the real home: HOME and AART_CLI_HOME are temporary
directories; PYTHONPATH is passed on, so it can name an aart-cli checkout.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn

from aart_cli_telemetry_server.adapters.sql import SqlEventRepository, open_repository, sqlite_url
from aart_cli_telemetry_server.domain.stats import window
from aart_cli_telemetry_server.effects import perform
from aart_cli_telemetry_server.web.app import create_app

from .strategies import NOW

PYTHON = os.environ.get("AART_CLI_PYTHON", "").strip()
pytestmark = pytest.mark.skipif(not PYTHON, reason="set AART_CLI_PYTHON to run against aart-cli")

_SEED = """
import sys
from aart_cli.domain.telemetry import TelemetryEvent
from aart_cli.io.telemetry_store import TelemetryOutbox

outbox = TelemetryOutbox(sys.argv[1] + "/state")
events = tuple(
    TelemetryEvent(
        f"00000000-0000-4000-8000-{n:012d}",
        "2026-10-29T17:00:00Z",
        "artifact.installed",
        "aart_cli",
        f"artifact-{n}",
        "mcp",
        "1.0.0",
    )
    for n in range(int(sys.argv[2]))
)
# Fixed ids rather than the ones the outbox draws, so a second home sends the same ones again.
print(outbox.record_action("action-1", lambda ids: events))
"""

_USE = """
import sys
from aart_cli.domain.artifact_usage import ArtifactUse, UsageKind
from aart_cli.io.telemetry_store import TelemetryOutbox

use = ArtifactUse("claude", UsageKind.MCP, "context7")
print(TelemetryOutbox(sys.argv[1] + "/state").record_use(use, "2026-10-29T17:00:00Z", sys.argv[2]))
"""


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def served(tmp_path: Path) -> Iterator[tuple[str, SqlEventRepository]]:
    repository = perform(open_repository(sqlite_url(str(tmp_path / "server")))).unwrap()
    port = _free_port()
    running = uvicorn.Server(
        uvicorn.Config(create_app(repository), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=running.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not running.started and time.monotonic() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", repository
    running.should_exit = True
    thread.join(10)
    repository.engine.dispose()


def _run(environment: dict[str, str], *argv: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603 -- the interpreter the person named, fixed arguments
        [PYTHON, *argv], env=environment, capture_output=True, timeout=60, check=False
    )


def _telemetry(environment: dict[str, str], *argv: str) -> dict[str, object]:
    finished = _run(environment, "-m", "aart_cli", "consume", "telemetry", *argv, "--json")
    document: dict[str, object] = json.loads(finished.stdout)
    return document


def test_aart_cli_delivers_each_event_once(
    served: tuple[str, SqlEventRepository], tmp_path: Path
) -> None:
    url, repository = served

    def a_machine(name: str) -> tuple[dict[str, str], Path]:
        home = tmp_path / name
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(tmp_path / f"{name}-user"),
            "AART_CLI_HOME": str(home),
            "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
        }
        assert _telemetry(environment, "status")["enabled"] is False
        assert _telemetry(environment, "enable", "--endpoint", url)["ok"] is True
        seeded = _run(environment, "-c", _SEED, str(home), "150")
        assert seeded.stdout == b"Ok(value=True)\n"
        return environment, home

    first, _ = a_machine("first")
    report = _telemetry(first, "sync")
    assert (report["sent"], report["requests"], report["reason"]) == (150, 2, None)

    # The same ids again -- what a client resends after an answer it never received.
    again, _ = a_machine("again")
    assert _telemetry(again, "sync")["sent"] == 150

    summary = perform(repository.summary(window(30, NOW))).unwrap()
    assert summary.installs == 150


def test_a_use_aart_cli_did_not_install_is_sent_until_the_person_turns_it_off(
    served: tuple[str, SqlEventRepository], tmp_path: Path
) -> None:
    url, repository = served
    home = tmp_path / "home"
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path / "user"),
        "AART_CLI_HOME": str(home),
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
    }
    assert _telemetry(environment, "enable", "--endpoint", url)["ok"] is True
    assert _telemetry(environment, "status")["unmanaged_uses"] == {"sent": True, "chosen": False}

    assert _run(environment, "-c", _USE, str(home), "use-1").stdout == b"Ok(value=True)\n"
    assert _telemetry(environment, "sync")["sent"] == 1

    assert _telemetry(environment, "unmanaged", "off")["ok"] is True
    assert _run(environment, "-c", _USE, str(home), "use-2").stdout == b"Ok(value=True)\n"
    report = _telemetry(environment, "sync", "--force")
    assert (report["sent"], report["skipped"]) == (0, 1)

    period = window(30, NOW)
    summary = perform(repository.summary(period)).unwrap()
    assert (summary.uses, summary.unmanaged_uses) == (0, 1)
    (tool,) = perform(repository.top_unmanaged(period, 10)).unwrap()
    assert (tool.artifact_type, tool.artifact_id, tool.uses) == ("mcp", "context7", 1)
