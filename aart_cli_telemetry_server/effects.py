"""The one place a result leaves IO.

Everything that touches the database answers with an `IOResult`: the type says an effect happened.
The shell -- the web routes, the command line and the ASGI entry point -- has to act on the answer
at some point, and this is where it does, so `returns.unsafe` is imported here and nowhere else
(`tests/test_architecture.py` holds that).
"""

from __future__ import annotations

from typing import TypeVar

from returns.io import IOResult
from returns.pipeline import is_successful
from returns.result import Failure, Result, Success
from returns.unsafe import unsafe_perform_io

__all__ = ["perform"]

_V = TypeVar("_V")
_E = TypeVar("_E")


def perform(result: IOResult[_V, _E]) -> Result[_V, _E]:
    """The plain result of an effect that has already run."""

    if is_successful(result):
        return Success(unsafe_perform_io(result.unwrap()))
    return Failure(unsafe_perform_io(result.failure()))
