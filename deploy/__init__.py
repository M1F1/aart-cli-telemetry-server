"""Deploying the image somewhere: a target turns an environment file into the commands that do it.

A target is a module in `deploy/targets/`. It names the settings it reads (`REQUIRED`, and
`OPTIONAL` with their defaults) and `plan(settings)` returns the steps: each a command and why it
runs. Nothing here knows any target by name, and the server's package imports nothing from here: a
platform is a parameter of the deployment, never a part of the server. A copy of this repository
that deploys somewhere of its own adds its module to `deploy/targets/` and its environment file to
`deploy/environments/`, two paths this repository keeps no files of a copy's names in, so taking
this repository's changes into the copy never conflicts with them.

An environment file holds names, addresses and references, never a secret: it is meant to be
committed. A key that names a secret, or a URL that carries a password, is refused, and no refusal
repeats a value. A secret reaches the server through what the platform keeps secrets in, by
reference, or through a file only the host reads.

`python -m deploy --target NAME --environment FILE` prints the plan; `--apply` runs it, step by
step, stopping at the first that fails unless it may.
"""

from __future__ import annotations

import argparse
import importlib
import pkgutil
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

__all__ = [
    "Refused",
    "Step",
    "apply",
    "describe",
    "known_targets",
    "plan_for",
    "read_environment",
    "settings_for",
]

_KEY = re.compile(r"[A-Z][A-Z0-9_]*")
#: A key whose name says it holds a secret, which an environment file never does.
_SECRET_KEY = re.compile(r"(PASSWORD|PASSWD|SECRET|TOKEN|CREDENTIAL|PRIVATE)", re.IGNORECASE)
#: `scheme://user:password@` anywhere in a value.
_URL_PASSWORD = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^/@\s:]*:[^/@\s]*@")


class Refused(Exception):
    """Why an environment or a plan cannot be used; it never repeats a value."""


@dataclass(frozen=True, slots=True)
class Step:
    """One command of a plan, and why it runs.

    `may_fail` lets the plan go on when the command fails, as removing what may not be there does.
    `until` reruns the command, a second apart, until what it prints is that, as waiting for a
    service to report healthy does.
    """

    argv: tuple[str, ...]
    why: str
    may_fail: bool = False
    until: str | None = None
    attempts: int = field(default=60)


def read_environment(text: str) -> dict[str, str]:
    """`KEY=VALUE` lines; blank lines and `#` comments are skipped. Values are taken as written."""

    settings: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or _KEY.fullmatch(key) is None:
            raise Refused(f"line {number} is not KEY=VALUE with an upper-case KEY")
        if key in settings:
            raise Refused(f"{key} is set twice")
        if _SECRET_KEY.search(key):
            raise Refused(
                f"{key} names a secret, which an environment file never holds: reference it in "
                "the platform's secret store instead"
            )
        if _URL_PASSWORD.search(value):
            raise Refused(
                f"{key} carries a password in a URL, which an environment file never holds: "
                "reference it in the platform's secret store instead"
            )
        settings[key] = value
    return settings


def known_targets(package: ModuleType | None = None) -> dict[str, ModuleType]:
    """Every target module in `deploy/targets/`, by its name."""

    if package is None:
        package = importlib.import_module("deploy.targets")
    found: dict[str, ModuleType] = {}
    for module in pkgutil.iter_modules(package.__path__):
        if module.name.startswith("_"):
            continue
        loaded = importlib.import_module(f"{package.__name__}.{module.name}")
        found[str(loaded.NAME)] = loaded
    return found


def settings_for(target: ModuleType, environment: Mapping[str, str]) -> dict[str, str]:
    """The environment with the target's defaults, refused when a setting is missing or unknown."""

    required: tuple[str, ...] = target.REQUIRED
    optional: Mapping[str, str] = target.OPTIONAL
    unknown = sorted(set(environment) - set(required) - set(optional))
    if unknown:
        raise Refused(f"{target.NAME} does not read {', '.join(unknown)}")
    missing = [key for key in required if not environment.get(key)]
    if missing:
        raise Refused(f"{target.NAME} needs {', '.join(missing)}")
    return {**optional, **environment}


def plan_for(target: ModuleType, environment: Mapping[str, str]) -> tuple[Step, ...]:
    steps: tuple[Step, ...] = target.plan(settings_for(target, environment))
    return steps


def describe(steps: Sequence[Step]) -> str:
    """The plan as a person reads it: why, then the command, for each step."""

    lines: list[str] = []
    for number, step in enumerate(steps, start=1):
        lines.append(f"# {number}. {step.why}")
        lines.append(shlex.join(step.argv))
    return "\n".join(lines)


Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- a plan's own commands, never a shell
        list(argv), capture_output=True, text=True, check=False
    )


def apply(
    steps: Sequence[Step],
    run: Runner = _run,
    say: Callable[[str], None] = print,
    wait: Callable[[float], None] = time.sleep,
) -> int:
    """Run each step in turn; 0 when the plan completed, 1 at the first step that failed."""

    for number, step in enumerate(steps, start=1):
        say(f"# {number}. {step.why}")
        say(shlex.join(step.argv))
        for _ in range(step.attempts if step.until is not None else 1):
            finished = run(step.argv)
            if step.until is None or finished.stdout.strip() == step.until:
                break
            wait(1.0)
        else:
            say(f"step {number} never printed {step.until!r}")
            return 1
        if finished.returncode != 0 and not step.may_fail:
            say(f"step {number} failed with exit code {finished.returncode}")
            if finished.stderr:
                say(finished.stderr.rstrip())
            return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m deploy")
    parser.add_argument("--target", required=True, help="a module in deploy/targets/")
    parser.add_argument("--environment", required=True, help="a KEY=VALUE file of settings")
    parser.add_argument("--apply", action="store_true", help="run the plan; without it, print it")
    arguments = parser.parse_args(argv)

    known = known_targets()
    if arguments.target not in known:
        print(f"no target {arguments.target}; known: {', '.join(sorted(known))}", file=sys.stderr)
        return 2
    try:
        text = Path(arguments.environment).read_text("utf-8")
    except OSError as error:
        print(f"the environment file cannot be read: {error.strerror}", file=sys.stderr)
        return 2
    try:
        steps = plan_for(known[arguments.target], read_environment(text))
    except Refused as refused:
        print(f"refused: {refused}", file=sys.stderr)
        return 2
    if not arguments.apply:
        print(describe(steps))
        return 0
    return apply(steps)
