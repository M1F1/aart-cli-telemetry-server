"""The telemetry API's version 1 request, read strictly (docs/telemetry-api-v1.md).

A request is accepted whole, as a `Batch`, or refused with the reason and every event that is
itself invalid, so the client can set those aside and send the rest. The wire shape is a pydantic
model in strict mode; what the rest of the server sees is `domain.events`. Nothing a client
submitted is repeated in a reason beyond an event's id and index: not a value, and not the name of
a field the contract does not have.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)
from pydantic_core import ErrorDetails, PydanticCustomError
from returns.result import Failure, Result, Success

from .events import SOURCES_FOR, Batch, Event, EventType, Source

__all__ = [
    "MAX_BODY_BYTES",
    "MAX_EVENTS",
    "Refusal",
    "RefusalKind",
    "RejectedEvent",
    "read_batch",
    "too_large",
]

MAX_BODY_BYTES = 1024 * 1024
MAX_EVENTS = 500

_UUID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
_MOMENT = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z$"
_NAME = r"^[0-9A-Za-z._@:+-]{1,200}$"
_KIND = r"^[a-z0-9-]{1,64}$"
_VERSION = r"^[0-9A-Za-z.+-]{1,64}$"
_UUID_PATTERN = re.compile(_UUID)


def _moment(value: str) -> datetime:
    """The UTC moment `value` names; the pattern has already held its shape."""

    return datetime.fromisoformat(value).astimezone(UTC)


def _a_moment_that_exists(value: str) -> str:
    try:
        _moment(value)
    except ValueError:
        raise PydanticCustomError("no_such_moment", "no such moment") from None
    return value


Uuid = Annotated[str, StringConstraints(pattern=_UUID)]
Moment = Annotated[str, StringConstraints(pattern=_MOMENT), AfterValidator(_a_moment_that_exists)]


class _EventDocument(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    event_id: Uuid
    occurred_at: Moment
    event_type: Literal[
        "artifact.installed",
        "artifact.updated",
        "artifact.removed",
        "artifact.used",
        "unmanaged.used",
    ]
    source: Literal["aart_cli", "claude_hook", "opencode_plugin"]
    artifact_id: Annotated[str, StringConstraints(pattern=_NAME)]
    artifact_type: Annotated[str, StringConstraints(pattern=_KIND)]
    artifact_version: Annotated[str, StringConstraints(pattern=_VERSION)] | None = None

    @model_validator(mode="after")
    def _source_reports_its_own_types(self) -> Self:
        if Source(self.source) not in SOURCES_FOR[EventType(self.event_type)]:
            raise PydanticCustomError(
                "source_event_type", "this source cannot report this event_type"
            )
        if self.event_type == EventType.UNMANAGED_USED.value and self.artifact_version is not None:
            raise PydanticCustomError(
                "unmanaged_version", "an unmanaged.used event has no artifact_version"
            )
        return self

    def event(self) -> Event:
        return Event(
            self.event_id,
            _moment(self.occurred_at),
            EventType(self.event_type),
            Source(self.source),
            self.artifact_id,
            self.artifact_type,
            self.artifact_version,
        )


class _RequestDocument(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Annotated[int, Field(strict=True, ge=1, le=1)]
    client_id: Uuid
    client_version: Annotated[str, StringConstraints(pattern=_VERSION)]
    events: Annotated[list[Any], Field(min_length=1, max_length=MAX_EVENTS)]


_EVENTS = Annotated[list[_EventDocument], Field(min_length=1, max_length=MAX_EVENTS)]


class _Events(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True)

    events: _EVENTS


class RefusalKind(StrEnum):
    TOO_LARGE = "too_large"
    NOT_JSON = "not_json"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class RejectedEvent:
    index: int
    event_id: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class Refusal:
    kind: RefusalKind
    error: str
    rejected_events: tuple[RejectedEvent, ...] = ()


_ENVELOPE = "the request must hold exactly schema_version, client_id, client_version and events"
_ENVELOPE_REASONS = {
    "schema_version": "schema_version must be 1",
    "client_id": "client_id must be a lowercase UUID",
    "client_version": "client_version must be 1-64 characters of [0-9A-Za-z.+-]",
    "events": f"events must be a list of 1 to {MAX_EVENTS} events",
}
_EVENT_REASONS = {
    "event_id": "event_id must be a lowercase UUID",
    "occurred_at": "occurred_at must be UTC as YYYY-MM-DDTHH:MM:SS[.ffffff]Z",
    "event_type": "unknown event_type",
    "source": "unknown source",
    "artifact_id": "artifact_id must be 1-200 characters of [0-9A-Za-z._@:+-]",
    "artifact_type": "artifact_type must be 1-64 characters of [a-z0-9-]",
    "artifact_version": "artifact_version must be 1-64 characters of [0-9A-Za-z.+-]",
}


def _envelope_reason(error: ErrorDetails) -> str:
    location = error["loc"]
    if not location:
        return "the request must be a JSON object"
    if error["type"] == "missing":
        return _ENVELOPE
    # A key the contract does not have is not in the table either, so it reads as the envelope.
    return _ENVELOPE_REASONS.get(str(location[0]), _ENVELOPE)


def _event_reason(error: ErrorDetails) -> str:
    """Why one event is refused, in the contract's words; never a submitted value or key."""

    kind, location = error["type"], error["loc"]
    if kind == "extra_forbidden":
        return "a field the contract does not have"
    if kind == "model_type":
        return "an event must be an object"
    field = str(location[2]) if len(location) > 2 else ""
    if kind == "missing":
        return f"missing {field}"
    if kind == "source_event_type":
        return "this source cannot report this event_type"
    if kind == "unmanaged_version":
        return "an unmanaged.used event has no artifact_version"
    return _EVENT_REASONS.get(field, "an event does not follow the contract")


def _named(raw: object) -> str | None:
    """A refused event's id, when it has one the contract would accept; else nothing."""

    identifier = raw.get("event_id") if isinstance(raw, dict) else None
    if isinstance(identifier, str) and _UUID_PATTERN.fullmatch(identifier) is not None:
        return identifier
    return None


def too_large() -> Refusal:
    return Refusal(RefusalKind.TOO_LARGE, f"the request body is larger than {MAX_BODY_BYTES} bytes")


def _refused(error: str, rejected: tuple[RejectedEvent, ...] = ()) -> Result[Batch, Refusal]:
    return Failure(Refusal(RefusalKind.INVALID, error, rejected))


def read_batch(raw: bytes) -> Result[Batch, Refusal]:
    """The batch `raw` is, or why it is refused. Accepts all or nothing."""

    if len(raw) > MAX_BODY_BYTES:
        return Failure(too_large())
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return Failure(Refusal(RefusalKind.NOT_JSON, "the request body is not JSON"))

    try:
        request = _RequestDocument.model_validate(document)
    except ValidationError as invalid:
        first = invalid.errors(include_url=False, include_input=False)[0]
        return _refused(_envelope_reason(first))

    try:
        events = _Events.model_validate({"events": request.events}).events
    except ValidationError as invalid:
        reasons: dict[int, str] = {}
        for error in invalid.errors(include_url=False, include_input=False):
            location = error["loc"]
            if len(location) >= 2 and isinstance(location[1], int):
                reasons.setdefault(location[1], _event_reason(error))
        rejected = tuple(
            RejectedEvent(index, _named(request.events[index]), reason)
            for index, reason in sorted(reasons.items())
        )
        return _refused(f"{len(rejected)} event(s) do not follow the contract", rejected)

    return Success(
        Batch(request.client_id, request.client_version, tuple(item.event() for item in events))
    )
