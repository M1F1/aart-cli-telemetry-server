"""`python scripts/quality.py`: every gate of `make quality`, in order, stopping at the first that
fails. Each runs as `python -m TOOL` under this interpreter, so CI needs neither make nor a tool on
`PATH`, only the packages poetry.lock pins, in whatever image a copy runs its checks in.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Sequence

GATES: tuple[tuple[str, ...], ...] = (
    ("scripts.secret_shape_check",),
    ("ruff", "check", "."),
    ("ruff", "format", "--check", "."),
    ("mypy", "aart_cli_telemetry_server", "deploy", "tests", "scripts"),
    ("tach", "check"),
    ("tach", "check-external"),
    ("coverage", "run", "-m", "pytest", "-q"),
    ("coverage", "report"),
)

Runner = Callable[[Sequence[str]], int]


def _run(argv: Sequence[str]) -> int:
    # The gates above, under this interpreter, never through a shell.
    return subprocess.run(list(argv), check=False).returncode


def main(run: Runner = _run, say: Callable[[str], None] = print) -> int:
    for gate in GATES:
        say(f"$ python -m {' '.join(gate)}")
        if run([sys.executable, "-m", *gate]) != 0:
            say(f"failed: python -m {' '.join(gate)}")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
