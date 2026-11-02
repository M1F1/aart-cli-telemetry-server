"""`python scripts/locked_requirements.py GROUP...`: what poetry.lock pins for those groups, as pip
requirements with every hash the lock records and every extra the project declares.

Poetry decides what the dependencies are and pip installs them, as in aart-cli: `poetry install`
takes an index only from a source in pyproject.toml, and adding one changes the hash the lock is
checked against, while pip reads `PIP_INDEX_URL`. So a copy that installs from its own index sets
that and edits no file. The image installs `main` with `pip install --require-hashes --no-deps`,
and CI installs `main dev` the same way, so neither needs Poetry, nor anything but the lock to
choose versions.
"""

from __future__ import annotations

import sys
import tomllib
from collections.abc import Mapping, Set
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
#: A marker every interpreter satisfies, put where the lock says `extra == "NAME"`: every extra the
#: project declares is installed.
ALWAYS = 'python_version >= "0"'


def _markers(package: Mapping[str, Any], chosen: Set[str]) -> str:
    """The package's markers for the chosen groups; a group the lock gives none is unconditional."""

    markers = package.get("markers", "")
    if isinstance(markers, str):
        return markers
    wanted = sorted(chosen & set(package["groups"]))
    if any(group not in markers for group in wanted):
        return ""
    return " or ".join(f"({markers[group]})" for group in wanted)


def requirements(
    lock: Mapping[str, Any], project: Mapping[str, Any], chosen: Set[str]
) -> list[str]:
    extras = sorted(project.get("project", {}).get("optional-dependencies", {}))
    lines: list[str] = []
    for package in lock["package"]:
        if not chosen & set(package["groups"]):
            continue
        markers = _markers(package, chosen)
        for extra in extras:
            markers = markers.replace(f'extra == "{extra}"', ALWAYS)
        line = f"{package['name']}=={package['version']}" + (f" ; {markers}" if markers else "")
        lines.append(" --hash=".join([line, *(file["hash"] for file in package["files"])]))
    return lines


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: python scripts/locked_requirements.py GROUP...", file=sys.stderr)
        return 2
    lock = tomllib.loads((ROOT / "poetry.lock").read_text("utf-8"))
    project = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    print("\n".join(requirements(lock, project, set(argv))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
