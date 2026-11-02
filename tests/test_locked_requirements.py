"""What poetry.lock pins is what pip installs: every package of the chosen groups, once for each
interpreter, at its locked version, with every hash the lock records, and every extra declared.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

from hypothesis import given
from hypothesis import strategies as st
from packaging.requirements import Requirement

from scripts.locked_requirements import requirements

ROOT = Path(__file__).resolve().parent.parent
LOCK = tomllib.loads((ROOT / "poetry.lock").read_text("utf-8"))
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
LINUX = {"sys_platform": "linux", "platform_python_implementation": "CPython"}


def _parsed(line: str) -> tuple[Requirement, list[str]]:
    requirement, *hashes = line.split(" --hash=")
    return Requirement(requirement), hashes


def _by_name(lines: list[str]) -> dict[str, tuple[Requirement, list[str]]]:
    parsed = [_parsed(line) for line in lines]
    found = {requirement.name: (requirement, hashes) for requirement, hashes in parsed}
    assert len(found) == len(parsed)
    return found


def _installed(requirement: Requirement, python: str) -> bool:
    marker = requirement.marker
    return marker is None or marker.evaluate({**LINUX, "python_version": python})


def test_every_runtime_package_is_pinned_with_every_hash_the_lock_records() -> None:
    found = _by_name(requirements(LOCK, PROJECT, {"main"}))
    runtime = [package for package in LOCK["package"] if "main" in package["groups"]]
    assert set(found) == {package["name"] for package in runtime}
    for package in runtime:
        requirement, hashes = found[package["name"]]
        assert str(requirement.specifier) == f"=={package['version']}"
        assert hashes == [file["hash"] for file in package["files"]]


def test_each_interpreter_is_given_one_version_of_each_package() -> None:
    # The lock may pin one package at a version per interpreter, under markers that never overlap.
    parsed = [_parsed(line)[0] for line in requirements(LOCK, PROJECT, {"main", "dev"})]
    assert {(r.name, str(r.specifier)) for r in parsed} == {
        (package["name"], f"=={package['version']}") for package in LOCK["package"]
    }
    for python in ("3.11", "3.12", "3.13", "3.14", "3.15"):
        names = [r.name for r in parsed if _installed(r, python)]
        assert len(names) == len(set(names))


def test_the_extras_are_installed_and_markers_still_choose_the_platform() -> None:
    found = _by_name(requirements(LOCK, PROJECT, {"main"}))
    for python in ("3.11", "3.13"):
        assert _installed(found["psycopg"][0], python)
        assert _installed(found["psycopg-binary"][0], python)
        assert not _installed(found["tzdata"][0], python)


def test_development_tools_come_only_with_their_group() -> None:
    assert "pytest" not in _by_name(requirements(LOCK, PROJECT, {"main"}))
    both = {_parsed(line)[0].name for line in requirements(LOCK, PROJECT, {"main", "dev"})}
    assert {"pytest", "fastapi"} <= both


def test_it_prints_the_groups_it_is_given() -> None:
    printed = subprocess.run(  # noqa: S603 -- this repository's own script
        [sys.executable, str(ROOT / "scripts" / "locked_requirements.py"), "main"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert printed == requirements(LOCK, PROJECT, {"main"})


_GROUPS = st.sets(st.sampled_from(["main", "dev", "docs"]), min_size=1)
_EXTRAS = ["one", "two"]
#: Each marker the generated lock uses, and whether it holds on Linux under Python 3.11 and 3.13.
_HOLDS = {
    'sys_platform == "win32"': {"3.11": False, "3.13": False},
    'python_version < "3.13"': {"3.11": True, "3.13": False},
    'python_version >= "3.13"': {"3.11": False, "3.13": True},
}


@st.composite
def _marker(draw: st.DrawFn) -> tuple[str, dict[str, bool]]:
    """A marker as a lock writes one, perhaps behind an extra, and where it holds, extras on."""

    base = draw(st.sampled_from(sorted(_HOLDS)))
    if draw(st.booleans()):
        return f'extra == "{draw(st.sampled_from(_EXTRAS))}" and {base}', _HOLDS[base]
    return base, _HOLDS[base]


@st.composite
def _packages(draw: st.DrawFn) -> list[tuple[dict[str, Any], dict[str, dict[str, bool]]]]:
    """Lock entries, each with where it holds for each of its groups (absent: always)."""

    names = draw(st.lists(st.from_regex(r"[a-z][a-z0-9]{0,8}", fullmatch=True), unique=True))
    packages = []
    for name in names:
        groups = sorted(draw(_GROUPS))
        holds: dict[str, dict[str, bool]] = {}
        markers: Any = ""
        if draw(st.booleans()):
            markers, where = draw(_marker())
            holds = dict.fromkeys(groups, where)
        elif draw(st.booleans()):
            # Poetry writes markers per group when groups differ; a group left out has none.
            markers = {}
            for group in draw(st.sets(st.sampled_from(groups))):
                markers[group], holds[group] = draw(_marker())
        entry = {
            "name": name,
            "version": f"{draw(st.integers(0, 9))}.{draw(st.integers(0, 9))}",
            "groups": groups,
            "markers": markers,
            "files": [{"file": f"{name}.whl", "hash": f"sha256:{name}"}],
        }
        packages.append((entry, holds))
    return packages


@given(packages=_packages(), chosen=_GROUPS)
def test_a_package_is_installed_exactly_where_a_chosen_group_wants_it_with_every_extra(
    packages: list[tuple[dict[str, Any], dict[str, dict[str, bool]]]], chosen: set[str]
) -> None:
    project: dict[str, Any] = {
        "project": {"optional-dependencies": {extra: [] for extra in _EXTRAS}}
    }
    lock = {"package": [entry for entry, _ in packages]}
    found = _by_name(requirements(lock, project, chosen))
    assert set(found) == {entry["name"] for entry, _ in packages if chosen & set(entry["groups"])}
    for entry, holds in packages:
        if entry["name"] not in found:
            continue
        requirement = found[entry["name"]][0]
        assert "extra" not in str(requirement.marker or "")
        for python in ("3.11", "3.13"):
            wanted = any(
                group not in holds or holds[group][python]
                for group in chosen & set(entry["groups"])
            )
            assert _installed(requirement, python) == wanted
