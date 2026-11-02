"""What a copy of this repository sets is held: each image CI pulls or publishes gets the
credentials named for that image and no other's, no step signs in to Docker's own configuration,
and every variable the workflow reads is one README.md lists.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8"))
IMAGE_ACTION = yaml.safe_load((ROOT / ".github" / "actions" / "image" / "action.yml").read_text())
PULL = re.compile(r"scripts/registry_image\.py pull (\w+) ")
PUSH = re.compile(r"scripts/registry_image\.py push (\w+) ")


def _pulled_with(step: dict[str, Any]) -> list[str]:
    return PULL.findall(step.get("run", ""))


def _credentials(prefix: str) -> dict[str, str]:
    return {
        f"{prefix}_{part}": f"${{{{ secrets[vars.{prefix}_{part}_SECRET_NAME] }}}}"
        for part in ("USERNAME", "PASSWORD")
    }


def _steps() -> list[dict[str, Any]]:
    return [step for job in WORKFLOW["jobs"].values() for step in job["steps"]]


def test_each_pull_in_a_job_gets_the_credentials_named_for_its_image() -> None:
    pulls = [(step, prefix) for step in _steps() for prefix in _pulled_with(step)]
    assert {prefix for _, prefix in pulls} == {
        "AART_CLI_TELEMETRY_CI_IMAGES",
        "AART_CLI_TELEMETRY_POSTGRES_IMAGE",
    }
    for step, prefix in pulls:
        assert step["env"] == _credentials(prefix), step["name"]


def test_each_pull_in_the_image_action_gets_its_credentials_from_every_step_that_uses_it() -> None:
    prefixes = [prefix for step in IMAGE_ACTION["runs"]["steps"] for prefix in _pulled_with(step)]
    assert prefixes == ["AART_CLI_TELEMETRY_PYTHON_IMAGE", "AART_CLI_TELEMETRY_POSTGRES_IMAGE"]
    expected = {name: value for prefix in prefixes for name, value in _credentials(prefix).items()}
    callers = [step for step in _steps() if step.get("uses") == "./.github/actions/image"]
    assert len(callers) == 2
    for step in callers:
        assert step["env"] == expected


def test_no_job_pulls_an_image_through_a_credentials_block() -> None:
    # One `credentials` block per image cannot be left out where no credentials are named, so
    # every image is pulled by scripts/registry_image.py instead.
    for name, job in WORKFLOW["jobs"].items():
        assert "container" not in job, name
        assert "services" not in job, name


def test_every_variable_the_workflow_reads_is_listed_in_the_readme() -> None:
    read = set(re.findall(r"vars\.(\w+)", (ROOT / ".github" / "workflows" / "ci.yml").read_text()))
    listed = set(re.findall(r"^\| `(\w+)` \|", (ROOT / "README.md").read_text(), re.MULTILINE))
    assert read
    assert read <= listed, sorted(read - listed)


def test_the_image_is_pushed_with_its_own_credentials_and_nothing_signs_in_to_docker_itself() -> (
    None
):
    pushes = [(step, prefix) for step in _steps() for prefix in PUSH.findall(step.get("run", ""))]
    assert [prefix for _, prefix in pushes] == ["AART_CLI_TELEMETRY_IMAGE"]
    ((step, prefix),) = pushes
    assert step["env"] == _credentials(prefix)
    for each in [*_steps(), *IMAGE_ACTION["runs"]["steps"]]:
        assert "docker login" not in each.get("run", ""), each.get("name")
        assert "docker logout" not in each.get("run", ""), each.get("name")
