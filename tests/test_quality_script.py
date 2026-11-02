"""`scripts/quality.py` runs every gate under its own interpreter and stops at the first failure."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from scripts.quality import GATES, main


@pytest.mark.parametrize("failing", range(len(GATES)))
def test_it_stops_at_the_first_gate_that_fails(failing: int) -> None:
    ran: list[tuple[str, ...]] = []

    def run(argv: Sequence[str]) -> int:
        ran.append(tuple(argv))
        return 1 if len(ran) == failing + 1 else 0

    said: list[str] = []
    assert main(run, said.append) == 1
    assert ran == [(sys.executable, "-m", *gate) for gate in GATES[: failing + 1]]
    assert said[-1] == f"failed: python -m {' '.join(GATES[failing])}"


def test_it_passes_when_every_gate_does() -> None:
    ran: list[Sequence[str]] = []

    def run(argv: Sequence[str]) -> int:
        ran.append(argv)
        return 0

    assert main(run, lambda _: None) == 0
    assert len(ran) == len(GATES)


def test_the_gates_are_what_the_makefile_s_own_targets_run() -> None:
    makefile = (Path(__file__).resolve().parent.parent / "Makefile").read_text("utf-8")
    commands: list[tuple[str, ...]] = []
    for target in ("lint", "format-check", "typecheck", "tach", "coverage"):
        recipe = makefile.split(f"\n{target}:\n", 1)[1].split("\n\n", 1)[0]
        commands += [tuple(line.removeprefix("\t$(RUN) ").split()) for line in recipe.splitlines()]
    assert commands == list(GATES)
