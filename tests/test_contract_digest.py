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
    "docs/telemetry-api-v1.md": "5cc32739e1dbdfac09af5b0f6ebfef23e04cf07fd1511128e35752ed5746c30b",
    "contract/telemetry-api-v1.examples.json": (
        "9fd38bb5fc586b9ca7c9b9c2bf83177ccc4769fa8c3bbe2772af40240902a037"
    ),
}


@pytest.mark.parametrize(("relative", "digest"), sorted(CONTRACT_SHA256.items()))
def test_the_contract_is_the_copy_aart_cli_holds(relative: str, digest: str) -> None:
    assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == digest
