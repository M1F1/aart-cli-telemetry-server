"""The contract is one text, copied byte for byte into both repositories (D-504).

aart-cli's `tests/telemetry_domain_test.py` holds the same two digests. Changing the contract is a
change to both repositories in step: a new version of the document, new digests here and there.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

CONTRACT_SHA256 = {
    "docs/telemetry-api-v1.md": "23e6c8c249bbce7361d28e8df1cdd2124c0ac0b26d8ae9e15fcdede6ded499c0",
    "contract/telemetry-api-v1.examples.json": (
        "352e268b6c19cbade894db3bf07854502091259237a4e3b4625c2c86c3243344"
    ),
}


@pytest.mark.parametrize(("relative", "digest"), sorted(CONTRACT_SHA256.items()))
def test_the_contract_is_the_copy_aart_cli_holds(relative: str, digest: str) -> None:
    assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == digest
