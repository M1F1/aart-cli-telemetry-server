"""Pull one image CI runs, with credentials of its own or with none.

`python3 scripts/pull_image.py IMAGE PREFIX` reads the user name and password from the
environment variables `PREFIX_USERNAME` and `PREFIX_PASSWORD`. With neither set, the image is
pulled as the runner is already signed in, or anonymously. With both, Docker signs in to the
registry the image's name gives, with the password on stdin, in a Docker configuration made for
this one pull and removed after it, so no other job on the runner sees or replaces the sign-in.
It runs on the runner itself, under whatever python3 it has, so it is held to Python 3.8.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from typing import Any, Callable

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


def pull(image: str, username: str, password: str, run: Run = subprocess.run) -> None:
    if bool(username) != bool(password):
        raise SystemExit(f"pulling {image}: name both a user name and a password, or neither")
    if not username:
        run(["docker", "pull", image], check=True)
        return
    host = registry(image)
    with tempfile.TemporaryDirectory(dir=os.environ.get("RUNNER_TEMP") or None) as config:
        env = dict(os.environ, DOCKER_CONFIG=config)
        login = ["docker", "login", *([host] if host else []), "--username", username]
        run([*login, "--password-stdin"], input=password, text=True, env=env, check=True)
        run(["docker", "pull", image], env=env, check=True)


if __name__ == "__main__":
    image, prefix = sys.argv[1:3]
    pull(
        image,
        os.environ.get(f"{prefix}_USERNAME", ""),
        os.environ.get(f"{prefix}_PASSWORD", ""),
    )
