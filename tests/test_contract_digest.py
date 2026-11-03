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
    "docs/telemetry-api-v1.md": "ba9eedcbfb62e636a28f064760b8cdf55c9b641c5b5b0fc5dad96f008d67b6ba",
    "contract/telemetry-api-v1.examples.json": (
        "c147eb499783599426b9a78c1f2b4a314ae838b1cb3133929720883d2ba947fd"
    ),
}


@pytest.mark.parametrize(("relative", "digest"), sorted(CONTRACT_SHA256.items()))
def test_the_contract_is_the_copy_aart_cli_holds(relative: str, digest: str) -> None:
    assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == digest
