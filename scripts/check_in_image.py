"""Run one of CI's checks in a CI image, on a copy of this checkout.

`python3 scripts/check_in_image.py IMAGE [--postgres POSTGRES_IMAGE] -- ARGUMENT...` installs
what poetry.lock pins with the image's Python (`AART_CLI_TELEMETRY_PYTHON`, `python` by default)
from the index `PIP_INDEX_URL` names, then runs that Python with the arguments. The images are
pulled beforehand, each with its own credentials (scripts/registry_image.py), so this signs in to
nothing and pulls nothing.

The files git tracks are copied into the container, as they are in the working tree, rather than
mounted, so nothing the check writes reaches the runner, it works where the runner's paths are not
the Docker host's, and no untracked file or cache is checked. Without git, the whole checkout is
copied but for git's own directory. With
`--postgres`, a PostgreSQL of that image runs beside it on a network of its own, trusting that
network, and `AART_CLI_TELEMETRY_TEST_POSTGRES_URL` names it. Every container and network this
makes is named after the run and removed at the end. It runs on the runner itself, under whatever
python3 it has, so it is held to Python 3.8.
"""

from __future__ import annotations

import argparse
import io
import os
import subprocess
import tarfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
#: Passed to the check by name, so no value, the index's credentials among them, is an argument.
PASSED = (
    "AART_CLI_TELEMETRY_PYTHON",
    "PIP_INDEX_URL",
    "PIP_DISABLE_PIP_VERSION_CHECK",
    "PIP_ROOT_USER_ACTION",
)
INSTALL_AND_RUN = (
    'set -eu; python="${AART_CLI_TELEMETRY_PYTHON:-python}"; '
    '"$python" scripts/locked_requirements.py main dev > /tmp/requirements.txt; '
    '"$python" -m pip install --require-hashes --no-deps --requirement /tmp/requirements.txt; '
    '"$python" -m pip install --no-deps --editable .; '
    'exec "$python" "$@"'
)


def docker(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *arguments], capture_output=True, text=True, check=check)


def tracked(root: Path) -> list[str] | None:
    """The files git tracks under `root` that the working tree has; `None` where git cannot say."""

    try:
        listing = subprocess.run(
            ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=False
        )
    except FileNotFoundError:
        return None
    if listing.returncode:
        return None
    names = listing.stdout.decode("utf-8").split("\0")
    return [name for name in names if name and os.path.lexists(root / name)]


def archive(root: Path, files: list[str] | None) -> bytes:
    """A tar of `files`, or of all of `root` but `.git`, under `work/`, for `docker cp -`."""

    def outside_git(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
        inside = member.name == "work/.git" or member.name.startswith("work/.git/")
        return None if inside else member

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        if files is None:
            tar.add(root, arcname="work", filter=outside_git)
        for name in files or []:
            tar.add(root / name, arcname=f"work/{name}", recursive=False)
    return buffer.getvalue()


def postgresql(image: str, name: str, network: str) -> str:
    """A PostgreSQL named `name` on `network`, once it answers; the URL the check reaches it by."""

    docker(
        "run", "--detach", "--name", name, "--network", network,
        "--env", "POSTGRES_USER=telemetry", "--env", "POSTGRES_DB=telemetry",
        "--env", "POSTGRES_HOST_AUTH_METHOD=trust",
        "--env", "POSTGRES_INITDB_ARGS=--locale-provider=icu --icu-locale=en-US",
        image,
    )  # fmt: skip
    for _ in range(120):
        # Over TCP: the image's first, initialising server listens only on its socket.
        ready = docker("exec", name, "pg_isready", "-h", "127.0.0.1", check=False)
        if ready.returncode == 0:
            return f"postgresql+psycopg://telemetry@{name}:5432/telemetry"
        time.sleep(0.5)
    raise SystemExit(f"PostgreSQL never ready:\n{docker('logs', name, check=False).stderr}")


def check(image: str, postgres: str | None, arguments: list[str]) -> int:
    run = f"aart-cli-telemetry-check-{uuid.uuid4().hex[:8]}"
    created = ["--name", run, "--workdir", "/work"]
    for name in PASSED:
        created += ["--env", name]
    try:
        if postgres:
            docker("network", "create", run)
            url = postgresql(postgres, f"{run}-postgres", run)
            created += ["--network", run, "--env", f"AART_CLI_TELEMETRY_TEST_POSTGRES_URL={url}"]
        docker("create", *created, image, "sh", "-c", INSTALL_AND_RUN, "sh", *arguments)
        copy = archive(ROOT, tracked(ROOT))
        subprocess.run(
            ["docker", "cp", "-", f"{run}:/"], input=copy, capture_output=True, check=True
        )
        return subprocess.run(["docker", "start", "--attach", run], check=False).returncode
    finally:
        docker("rm", "--force", run, f"{run}-postgres", check=False)
        docker("network", "rm", run, check=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("image")
    parser.add_argument("--postgres", help="the PostgreSQL image to run beside the check")
    parser.add_argument("arguments", nargs="+", help="what the image's Python runs, after --")
    options = parser.parse_args()
    return check(options.image, options.postgres, options.arguments)


if __name__ == "__main__":
    raise SystemExit(main())
