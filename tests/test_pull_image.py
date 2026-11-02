"""Each image CI runs is pulled with credentials of its own, or with none: the registry signed in to
is the one the image's name gives, as Docker reads it, and the password reaches Docker only on
stdin, in a Docker configuration that exists for that one pull.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from scripts.pull_image import pull, registry

from .credential_fixtures import held

PASSWORD = held()


class Docker:
    """Records what `docker` was asked, and whether its configuration directory existed then."""

    def __init__(self, refuse: str | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.refuse = refuse

    def __call__(self, argv: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        env = options.get("env")
        config = None if env is None else env.get("DOCKER_CONFIG")
        self.calls.append(
            {
                "argv": argv,
                "input": options.get("input"),
                "config": config,
                "config_existed": config is not None and Path(config).is_dir(),
            }
        )
        if argv[1] == self.refuse:
            raise subprocess.CalledProcessError(1, argv)
        return subprocess.CompletedProcess(argv, 0)


_component = st.from_regex(r"[a-z0-9]+(?:[._-][a-z0-9]+)*", fullmatch=True)
_path = st.lists(_component, min_size=1, max_size=3).map("/".join)
# A first part without a dot, a port or a capital: Docker reads it as a repository, not a host.
_undotted = st.from_regex(r"[a-z0-9]+(?:[_-][a-z0-9]+)*", fullmatch=True).filter(
    lambda part: part != "localhost"
)
_hostless = st.builds(
    lambda first, rest: "/".join([first, *rest]), _undotted, st.lists(_component, max_size=2)
)
_suffix = st.sampled_from(["", ":16", ":3.13-slim-bookworm", "@sha256:" + "a" * 64])
_label = st.from_regex(r"[a-z0-9](?:[a-z0-9-]{0,10}[a-z0-9])?", fullmatch=True)
_host = st.one_of(
    st.lists(_label, min_size=2, max_size=4).map(".".join),
    st.builds(lambda name, port: f"{name}:{port}", _label, st.integers(1, 65535)),
    st.just("localhost"),
).filter(lambda host: host not in {"docker.io", "index.docker.io"})


@given(_host, _path, _suffix)
def test_a_name_whose_first_part_is_a_host_is_pulled_from_that_host(
    host: str, path: str, suffix: str
) -> None:
    assert registry(f"{host}/{path}{suffix}") == host


@given(_hostless, _suffix)
def test_a_name_without_a_host_is_pulled_from_docker_hub(path: str, suffix: str) -> None:
    assert registry(f"{path}{suffix}") is None


@pytest.mark.parametrize(
    ("image", "expected"),
    [
        ("python:3.13", None),
        ("library/python:3.13", None),
        ("docker.io/library/python:3.13", None),
        ("index.docker.io/library/python:3.13", None),
        ("localhost/python", "localhost"),
        ("localhost:5000/python", "localhost:5000"),
        ("Mirror/postgres:16", "Mirror"),
        ("registry.example/team/python:3.13", "registry.example"),
    ],
)
def test_names_docker_reads_the_same_way(image: str, expected: str | None) -> None:
    assert registry(image) == expected


def test_without_credentials_the_image_is_pulled_as_the_runner_is_signed_in() -> None:
    docker = Docker()
    pull("registry.example/python:3.13", "", "", run=docker)
    assert [call["argv"] for call in docker.calls] == [
        ["docker", "pull", "registry.example/python:3.13"]
    ]
    assert docker.calls[0]["config"] is None


def test_with_credentials_the_password_goes_on_stdin_in_a_configuration_of_its_own() -> None:
    docker = Docker()
    pull("mirror.example/postgres:16", "puller", PASSWORD, run=docker)
    login, fetched = docker.calls
    assert login["argv"] == [
        "docker", "login", "mirror.example", "--username", "puller", "--password-stdin"
    ]  # fmt: skip
    assert login["input"] == PASSWORD
    assert fetched["argv"] == ["docker", "pull", "mirror.example/postgres:16"]
    assert login["config"] == fetched["config"]
    assert login["config"] != os.environ.get("DOCKER_CONFIG")
    assert login["config_existed"]
    assert fetched["config_existed"]
    assert all(PASSWORD not in " ".join(call["argv"]) for call in docker.calls)
    assert not Path(login["config"]).exists()


def test_docker_hub_is_signed_in_to_without_naming_a_host() -> None:
    docker = Docker()
    pull("python:3.13", "puller", PASSWORD, run=docker)
    assert docker.calls[0]["argv"] == [
        "docker", "login", "--username", "puller", "--password-stdin"
    ]  # fmt: skip


def test_a_refused_sign_in_pulls_nothing_and_leaves_no_configuration() -> None:
    docker = Docker(refuse="login")
    with pytest.raises(subprocess.CalledProcessError):
        pull("mirror.example/postgres:16", "puller", PASSWORD, run=docker)
    assert [call["argv"][1] for call in docker.calls] == ["login"]
    assert not Path(docker.calls[0]["config"]).exists()


def test_a_failed_pull_leaves_no_configuration() -> None:
    docker = Docker(refuse="pull")
    with pytest.raises(subprocess.CalledProcessError):
        pull("mirror.example/postgres:16", "puller", PASSWORD, run=docker)
    assert not Path(docker.calls[0]["config"]).exists()


@pytest.mark.parametrize(("username", "password"), [("puller", ""), ("", PASSWORD)])
def test_half_a_pair_is_refused_before_docker_is_asked(username: str, password: str) -> None:
    docker = Docker()
    with pytest.raises(SystemExit, match="both"):
        pull("mirror.example/postgres:16", username, password, run=docker)
    assert docker.calls == []
