"""Deployment targets: an environment file holds no secret, a target is found by its module alone,
the plan is printed unless applied, and the smoke check stores nothing.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Sequence
from itertools import pairwise
from pathlib import Path

import pytest
import uvicorn
from hypothesis import given
from hypothesis import strategies as st

import deploy
import deploy.targets
from aart_cli_telemetry_server.adapters.sql import SqlEventRepository, open_repository, sqlite_url
from aart_cli_telemetry_server.domain.stats import window
from aart_cli_telemetry_server.effects import perform
from aart_cli_telemetry_server.web.app import create_app
from deploy import Refused, Step, apply, describe, plan_for, read_environment, settings_for, smoke
from deploy.targets import docker

from .strategies import NOW

_SAFE_KEYS = st.from_regex(r"[A-Z][A-Z0-9_]{0,15}", fullmatch=True).filter(
    lambda key: (
        not any(word in key for word in ("PASSWORD", "PASSWD", "SECRET", "TOKEN"))
        and not any(word in key for word in ("CREDENTIAL", "PRIVATE"))
    )
)
_SAFE_VALUES = st.text(
    alphabet=st.characters(min_codepoint=0x21, max_codepoint=0x7E, exclude_characters="@"),
    max_size=40,
)
#: What a secret looks like here: long enough that finding it in a message is no coincidence.
_SECRET_VALUES = st.from_regex(r"[a-z0-9]{16,24}", fullmatch=True)


# -- the environment file ---------------------------------------------------------------------


@given(st.dictionaries(_SAFE_KEYS, _SAFE_VALUES, max_size=8))
def test_an_environment_file_reads_back_what_it_holds(settings: dict[str, str]) -> None:
    text = "# settings\n\n" + "".join(f"{key} = {value}\n" for key, value in settings.items())
    assert read_environment(text) == settings


@pytest.mark.parametrize(
    ("text", "said"),
    [
        ("IMAGE\n", "line 1 is not KEY=VALUE"),
        ("image=x\n", "line 1 is not KEY=VALUE"),
        ("# a comment\nIMAGE=a\nIMAGE=b\n", "IMAGE is set twice"),
    ],
)
def test_a_line_that_is_not_a_setting_is_refused(text: str, said: str) -> None:
    with pytest.raises(Refused, match=said):
        read_environment(text)


@given(
    key=st.sampled_from(
        ["DB_PASSWORD", "PASSWD", "API_SECRET", "ACCESS_TOKEN", "CREDENTIALS", "PRIVATE_KEY"]
    ),
    value=_SECRET_VALUES,
)
def test_a_key_that_names_a_secret_is_refused_without_its_value(key: str, value: str) -> None:
    with pytest.raises(Refused) as refused:
        read_environment(f"{key}={value}\n")
    assert "names a secret" in str(refused.value)
    assert value not in str(refused.value)


@given(value=_SECRET_VALUES, scheme=st.sampled_from(["postgresql+psycopg", "https", "redis"]))
def test_a_url_carrying_a_password_is_refused_without_it(value: str, scheme: str) -> None:
    with pytest.raises(Refused) as refused:
        read_environment(f"DATABASE={scheme}://telemetry:{value}@db:5432/telemetry\n")
    assert "password in a URL" in str(refused.value)
    assert value not in str(refused.value)


def test_a_url_without_a_password_is_a_setting() -> None:
    assert read_environment("REGISTRY=https://registry.example/v2\nDB=postgresql://u@db/x\n") == {
        "REGISTRY": "https://registry.example/v2",
        "DB": "postgresql://u@db/x",
    }


# -- targets --------------------------------------------------------------------------------


def test_settings_are_the_target_s_defaults_then_the_file_and_nothing_else() -> None:
    assert settings_for(docker, {"IMAGE": "i"}) == {**docker.OPTIONAL, "IMAGE": "i"}
    with pytest.raises(Refused, match="docker needs IMAGE"):
        settings_for(docker, {"VOLUME": "v"})
    with pytest.raises(Refused, match="docker does not read IMAGES, PORT"):
        settings_for(docker, {"IMAGE": "i", "IMAGES": "i", "PORT": "1"})


def test_a_target_is_found_by_its_module_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "elsewhere.py").write_text(
        "from deploy import Step\n"
        "NAME = 'elsewhere'\nREQUIRED = ('HOST',)\nOPTIONAL = {}\n"
        "def plan(settings):\n    return (Step(('true', settings['HOST']), 'why'),)\n"
    )
    (tmp_path / "_helper.py").write_text("NAME = 'never'\n")
    monkeypatch.setattr(deploy.targets, "__path__", [*deploy.targets.__path__, str(tmp_path)])
    monkeypatch.delitem(sys.modules, "deploy.targets.elsewhere", raising=False)

    found = deploy.known_targets()
    # A copy of this repository has targets of its own beside these.
    assert {"docker", "elsewhere"} <= set(found)
    assert "never" not in found
    assert plan_for(found["elsewhere"], {"HOST": "h"}) == (Step(("true", "h"), "why"),)


def _commands(settings: dict[str, str]) -> list[tuple[str, ...]]:
    return [step.argv for step in plan_for(docker, settings)]


def test_docker_replaces_the_container_on_the_same_volume_and_waits_for_health() -> None:
    steps = plan_for(docker, {"IMAGE": "registry.example/telemetry:1.2.3"})
    assert [step.argv[1] for step in steps] == ["pull", "stop", "rm", "run", "inspect"]
    assert [step.may_fail for step in steps] == [False, True, True, False, False]
    run = steps[3].argv
    assert run[-1] == "registry.example/telemetry:1.2.3"
    for option in (
        ("--publish", "127.0.0.1:8000:8000"),
        ("--volume", "aart-cli-telemetry:/data"),
        ("--env", "AART_CLI_TELEMETRY_BEHIND_AUTHENTICATING_PROXY=true"),
        ("--restart", "unless-stopped"),
    ):
        assert option in pairwise(run)
    assert "--read-only" in run
    assert "--env-file" not in run
    assert (steps[4].until, steps[4].attempts) == ("healthy", 90)


@pytest.mark.parametrize("name", sorted(deploy.known_targets()))
def test_every_setting_a_target_reads_can_be_written_in_an_environment_file(name: str) -> None:
    target = deploy.known_targets()[name]
    for key in (*target.REQUIRED, *target.OPTIONAL):
        assert read_environment(f"{key}=x") == {key: "x"}


def test_docker_on_another_host_names_its_context_in_every_command() -> None:
    commands = _commands(
        {"IMAGE": "i", "DOCKER_CONTEXT": "telemetry-host", "ENV_FILE": "/srv/t.env"}
    )
    assert all(command[:3] == ("docker", "--context", "telemetry-host") for command in commands)
    run = commands[3]
    assert ("--env-file", "/srv/t.env") in pairwise(run)


def test_docker_pulls_unless_told_not_to_and_nothing_else_is_a_choice() -> None:
    assert [command[1] for command in _commands({"IMAGE": "i", "PULL": "false"})] == [
        "stop",
        "rm",
        "run",
        "inspect",
    ]
    with pytest.raises(Refused, match="PULL is true or false"):
        _commands({"IMAGE": "i", "PULL": "yes"})


# -- printing and applying a plan ---------------------------------------------------------


class _Runner:
    def __init__(self, answers: dict[str, list[tuple[int, str]]]) -> None:
        self.answers = answers
        self.ran: list[str] = []

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.ran.append(argv[0])
        code, out = (self.answers.get(argv[0]) or [(0, "")]).pop(0)
        return subprocess.CompletedProcess(list(argv), code, out, "it went wrong")


_PLAN = (
    Step(("first",), "one"),
    Step(("optional",), "two", may_fail=True),
    Step(("wait",), "three", until="healthy", attempts=5),
    Step(("last",), "four"),
)


def test_a_plan_runs_in_order_past_a_step_that_may_fail_and_waits_until() -> None:
    runner = _Runner({"optional": [(1, "")], "wait": [(0, "starting"), (0, "healthy")]})
    said: list[str] = []
    assert apply(_PLAN, run=runner, say=said.append, wait=lambda _: None) == 0
    assert runner.ran == ["first", "optional", "wait", "wait", "last"]
    assert said[:2] == ["# 1. one", "first"]


def test_a_plan_stops_at_the_first_step_that_fails() -> None:
    runner = _Runner({"first": [(3, "")]})
    said: list[str] = []
    assert apply(_PLAN, run=runner, say=said.append, wait=lambda _: None) == 1
    assert runner.ran == ["first"]
    assert said[-2:] == ["step 1 failed with exit code 3", "it went wrong"]


def test_a_plan_stops_when_what_it_waits_for_never_comes() -> None:
    runner = _Runner({"wait": [(0, "starting")] * 5})
    said: list[str] = []
    assert apply(_PLAN, run=runner, say=said.append, wait=lambda _: None) == 1
    assert runner.ran == ["first", "optional", *["wait"] * 5]
    assert said[-1] == "step 3 never printed 'healthy'"


def test_without_apply_the_plan_is_printed_and_nothing_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def never(*_: object, **__: object) -> None:
        raise AssertionError("ran a command")

    monkeypatch.setattr(subprocess, "run", never)
    environment = tmp_path / "x.env"
    environment.write_text("IMAGE=registry.example/telemetry:1\n")
    assert deploy.main(["--target", "docker", "--environment", str(environment)]) == 0
    assert (
        capsys.readouterr().out
        == describe(plan_for(docker, {"IMAGE": "registry.example/telemetry:1"})) + "\n"
    )


@pytest.mark.parametrize(
    ("argv", "text", "said"),
    [
        (["--target", "nowhere"], "IMAGE=i\n", "no target nowhere; known: "),
        (["--target", "docker"], None, "the environment file cannot be read"),
        (["--target", "docker"], "IMAGE=i\nAPI_TOKEN=abcdefabcdef1234\n", "names a secret"),
    ],
)
def test_what_cannot_be_planned_is_refused_without_a_value(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    text: str | None,
    said: str,
) -> None:
    environment = tmp_path / "x.env"
    if text is not None:
        environment.write_text(text)
    assert deploy.main([*argv, "--environment", str(environment)]) == 2
    error = capsys.readouterr().err
    assert said in error
    assert "abcdefabcdef1234" not in error


# -- the smoke check -------------------------------------------------------------------------


def _answers(**by_path: tuple[int | None, bytes]) -> smoke.Fetch:
    def fetch(url: str, data: bytes | None) -> tuple[int | None, bytes]:
        return by_path[url.rsplit("/", 1)[-1]]

    return fetch


_GOOD = {"healthz": (200, b'{"status":"ok"}'), "batch": (422, b"{}"), "dashboard": (200, b"")}


@pytest.mark.parametrize(
    ("answers", "code", "last"),
    [
        (_GOOD, 0, "ok: GET /dashboard answered 200, with no sign-in in front of it"),
        (
            {**_GOOD, "dashboard": (302, b"")},
            0,
            "ok: GET /dashboard answered 302, a sign-in in front of it",
        ),
        ({**_GOOD, "dashboard": (None, b"")}, 0, "note: GET /dashboard answered None"),
        (
            {**_GOOD, "healthz": (503, b"")},
            1,
            "ok: GET /dashboard answered 200, with no sign-in in front of it",
        ),
    ],
)
def test_the_smoke_check_says_each_answer(
    answers: dict[str, tuple[int | None, bytes]], code: int, last: str
) -> None:
    said: list[str] = []
    assert smoke.check("https://telemetry.example/", _answers(**answers), said.append) == code
    assert said[-1] == last


def test_an_ingest_endpoint_behind_a_sign_in_fails_the_smoke_check() -> None:
    said: list[str] = []
    answers = {**_GOOD, "batch": (401, b"")}
    assert smoke.check("https://telemetry.example", _answers(**answers), said.append) == 1
    assert said[1].startswith(
        "FAIL: POST /api/v1/events/batch with {} answered 401; aart-cli sends"
    )


@given(password=_SECRET_VALUES)
def test_an_address_with_a_password_is_refused_without_it(password: str) -> None:
    said: list[str] = []
    assert smoke.check(f"https://user:{password}@telemetry.example", _answers(), said.append) == 2
    assert said == ["refused: the address may not carry a user name or password"]
    assert smoke.check("ftp://telemetry.example", _answers(), said.append) == 2


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


def test_the_smoke_check_passes_on_the_server_and_stores_nothing(
    served: tuple[str, SqlEventRepository],
) -> None:
    url, repository = served
    said: list[str] = []
    assert smoke.check(url, say=said.append) == 0
    assert said == [
        "ok: GET /healthz answered 200",
        "ok: POST /api/v1/events/batch with {} answered 422",
        "ok: GET /dashboard answered 200, with no sign-in in front of it",
    ]
    summary = perform(repository.summary(window(30, NOW))).unwrap()
    assert (summary.uses, summary.installs, summary.failures, summary.active_clients) == (
        0,
        0,
        0,
        0,
    )
