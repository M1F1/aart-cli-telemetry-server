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
    "docs/telemetry-api-v1.md": "28580de516ed19a0aee98643bad8fa8f4d44c1a74f5dd5bab659d4e17b09f088",
    "contract/telemetry-api-v1.examples.json": (
        "9dfefb930d5f0fbcf9740ca4d9fbeea672f159a6dd0f574f8cf397f2010a3011"
    ),
}


@pytest.mark.parametrize(("relative", "digest"), sorted(CONTRACT_SHA256.items()))
def test_the_contract_is_the_copy_aart_cli_holds(relative: str, digest: str) -> None:
    assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == digest
