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
    "docs/telemetry-api-v1.md": "7bded26a8b5f79ec4168a2fb71bb0d98e9728c15973197135c94baa1d8032f25",
    "contract/telemetry-api-v1.examples.json": (
        "e699865e780d310761d4e34aa280de57da474c9be8fc6f43de338e6f86421fe9"
    ),
}


@pytest.mark.parametrize(("relative", "digest"), sorted(CONTRACT_SHA256.items()))
def test_the_contract_is_the_copy_aart_cli_holds(relative: str, digest: str) -> None:
    assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == digest
