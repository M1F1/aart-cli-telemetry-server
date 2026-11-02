"""Credential-shaped fixtures, assembled at run time rather than written down, as in aart-cli.

A secret scanner matches the shape of a credential wherever it finds one, and push protection at
an organisation that turns it on refuses a push carrying one, even a redaction test's made-up
value whose purpose is to prove a credential never escapes. So the shape is assembled here, and
`scripts/secret_shape_check.py` keeps any file from writing it down.
"""

from __future__ import annotations

import secrets

_AT = "@"
_QUOTE = '"'
_TOKEN_PREFIX = "ghp" + "_"


def held() -> str:
    """A made-up secret value, fresh for each test, so no file holds one."""

    return "held-" + secrets.token_hex(8)


def credential_url(
    host: str, path: str = "", *, scheme: str = "https", user: str = "user", held: str = "made-up"
) -> str:
    """A URL carrying a user name and a secret, the shape a database URL or an index URL has.

    The value is named `held`, not `password`: a credential word followed by a quoted value is
    itself a shape the check refuses.
    """

    return f"{scheme}://{user}:{held}{_AT}{host}{path}"


def secret_field(key: str, value: str) -> str:
    """`"key":"value"`, a JSON member whose key names a credential, without writing the quotes."""

    return f"{_QUOTE}{key}{_QUOTE}:{_QUOTE}{value}{_QUOTE}"


def access_token(body: str = "0123456789abcdefghijklmnopqrstuvwx") -> str:
    """A literal in the shape of a GitHub personal access token: its prefix and length."""

    return _TOKEN_PREFIX + body


def assignment(name: str, value: str) -> str:
    """`NAME=value`, a credential named by an assignment, as a transcript prints it."""

    return name + "=" + value
