"""Pull an image CI runs, or push the image it publishes, with credentials of its own or with none.

    python3 scripts/registry_image.py pull PREFIX IMAGE
    python3 scripts/registry_image.py push PREFIX IMAGE...

The user name and password are read from the environment variables `PREFIX_USERNAME` and
`PREFIX_PASSWORD`. With neither set, Docker works as the runner is already signed in, or
anonymously. With both, Docker signs in to the registry the image's name gives, with the password on
stdin, in a Docker configuration made under the job's temporary directory for this one pull or push
and removed after it. The runner's own configuration never holds the sign-in, so no other job on
the runner sees or replaces it, and a job stopped before it removes its configuration leaves it
only in the temporary directory the runner empties before the next job. It runs on the runner
itself, under whatever python3 it has, so it is held to Python 3.8.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Sequence

#: The names Docker Hub is reached by; `docker login` signs in to it when given no host.
DOCKER_HUB = frozenset({"docker.io", "index.docker.io"})

Run = Callable[..., Any]


def registry(image: str) -> str | None:
    """The registry host an image's name gives, as Docker reads it; `None` for Docker Hub.

    The first part of the name is a host when there is a part after it and it has a dot, a port
    or a capital letter, or is `localhost`; otherwise it is the start of a Docker Hub repository.
    """

    first, slash, _ = image.partition("/")
    hosted = "." in first or ":" in first or first == "localhost" or first != first.lower()
    if not slash or not hosted or first in DOCKER_HUB:
        return None
    return first


@contextmanager
def _signed_in(
    host: str | None, username: str, password: str, run: Run
) -> Iterator[dict[str, str] | None]:
    """The environment Docker runs in: the runner's own without credentials, else one whose
    configuration is signed in to `host` and exists only inside this block."""

    if not username:
        yield None
        return
    with tempfile.TemporaryDirectory(dir=os.environ.get("RUNNER_TEMP") or None) as config:
        env = dict(os.environ, DOCKER_CONFIG=config)
        login = ["docker", "login", *([host] if host else []), "--username", username]
        run([*login, "--password-stdin"], input=password, text=True, env=env, check=True)
        yield env


def _pair(what: str, username: str, password: str) -> None:
    if bool(username) != bool(password):
        raise SystemExit(f"{what}: name both a user name and a password, or neither")


def _docker(argv: list[str], env: dict[str, str] | None, run: Run) -> None:
    if env is None:
        run(argv, check=True)
    else:
        run(argv, env=env, check=True)


def pull(image: str, username: str, password: str, run: Run = subprocess.run) -> None:
    _pair(f"pulling {image}", username, password)
    with _signed_in(registry(image), username, password, run) as env:
        _docker(["docker", "pull", image], env, run)


def push(images: Sequence[str], username: str, password: str, run: Run = subprocess.run) -> None:
    """Push each of `images`, tags already made, signing in once to the one registry they share."""

    _pair("pushing", username, password)
    hosts = {registry(image) for image in images}
    if len(hosts) != 1:
        raise SystemExit("pushing: name one or more images in one registry")
    with _signed_in(hosts.pop(), username, password, run) as env:
        for image in images:
            _docker(["docker", "push", image], env, run)


def main(arguments: Sequence[str], run: Run = subprocess.run) -> None:
    if len(arguments) < 3 or arguments[0] not in ("pull", "push"):
        raise SystemExit("usage: registry_image.py pull PREFIX IMAGE | push PREFIX IMAGE...")
    action, prefix, images = arguments[0], arguments[1], arguments[2:]
    username = os.environ.get(f"{prefix}_USERNAME", "")
    password = os.environ.get(f"{prefix}_PASSWORD", "")
    if action == "push":
        push(images, username, password, run)
        return
    if len(images) != 1:
        raise SystemExit("usage: registry_image.py pull PREFIX IMAGE")
    pull(images[0], username, password, run)


if __name__ == "__main__":
    main(sys.argv[1:])
