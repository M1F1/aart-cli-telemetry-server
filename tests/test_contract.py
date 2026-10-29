"""The version 1 request is read exactly as docs/telemetry-api-v1.md says."""

from __future__ import annotations

import copy
import json
import string
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from returns.pipeline import is_successful
from returns.result import Failure, Result, Success

from aart_cli_telemetry_server.domain.contract import (
    MAX_BODY_BYTES,
    MAX_EVENTS,
    Refusal,
    RefusalKind,
    read_batch,
)
from aart_cli_telemetry_server.domain.events import SOURCES_FOR, Batch, Event, EventType, Source

from .strategies import events, request

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = json.loads((ROOT / "contract" / "telemetry-api-v1.examples.json").read_text("utf-8"))
EVENT_FIELDS = {
    "event_id",
    "occurred_at",
    "event_type",
    "source",
    "artifact_id",
    "artifact_type",
    "artifact_version",
}


def _valid() -> dict[str, Any]:
    return copy.deepcopy(EXAMPLES["valid"][0]["request"])


def _read(document: object) -> Result[Batch, Refusal]:
    return read_batch(json.dumps(document).encode())


def _refusal(document: object) -> Refusal:
    result = _read(document)
    assert isinstance(result, Failure), result
    refusal = result.failure()
    assert isinstance(refusal, Refusal)
    return refusal


def test_every_valid_example_is_accepted_whole() -> None:
    for example in EXAMPLES["valid"]:
        batch = _read(example["request"]).unwrap()
        assert [event.event_id for event in batch.events] == [
            event["event_id"] for event in example["request"]["events"]
        ], example["name"]


def test_every_invalid_example_is_refused_and_named_by_index() -> None:
    for example in EXAMPLES["invalid"]:
        document = _valid()
        document["events"].append(example["event"])
        refused = _refusal(document)
        assert refused.kind is RefusalKind.INVALID
        assert [item.index for item in refused.rejected_events] == [1], example["name"]


@given(items=st.lists(events(), min_size=1, max_size=20))
def test_every_event_the_contract_allows_reads_back_as_it_was(items: list[Event]) -> None:
    result = read_batch(request(items))
    assert isinstance(result, Success)
    assert result.unwrap().events == tuple(items)
    assert result.unwrap().client_version == "0.22.0"


def test_a_reason_never_repeats_a_submitted_value_or_key() -> None:
    document = _valid()
    document["events"][0]["private-work-key"] = "/Users/someone/private-work"
    document["events"][0]["artifact_id"] = "/Users/someone/private-work"
    refused = _refusal(document)
    said = json.dumps([refused.error, [item.reason for item in refused.rejected_events]])
    assert "private-work" not in said
    assert "someone" not in said


_ENVELOPE_CHANGES: tuple[Callable[[dict[str, Any]], object], ...] = (
    lambda r: r.update(schema_version=2),
    lambda r: r.update(schema_version=True),
    lambda r: r.update(schema_version=1.0),
    lambda r: r.update(schema_version="1"),
    lambda r: r.update(client_id="not-a-uuid"),
    lambda r: r.update(client_id=r["client_id"].upper()),
    lambda r: r.update(client_version=""),
    lambda r: r.update(events=[]),
    lambda r: r.update(events="nope"),
    lambda r: r.update(events=r["events"] * (MAX_EVENTS + 1)),
    lambda r: r.update(hostname="laptop"),
    lambda r: r.pop("client_version"),
)


def test_the_envelope_is_exact() -> None:
    for change in _ENVELOPE_CHANGES:
        document = _valid()
        change(document)
        refused = _refusal(document)
        assert refused.kind is RefusalKind.INVALID
        assert refused.rejected_events == ()


def test_a_request_that_is_not_an_object_or_not_json_or_too_large() -> None:
    assert _refusal([1, 2]).error == "the request must be a JSON object"
    for raw in (b"{not json", b"\xff\xfe", b"[" * 100_000):
        result = read_batch(raw)
        assert isinstance(result, Failure)
        assert result.failure().kind is RefusalKind.NOT_JSON
    too_big = read_batch(b" " * (MAX_BODY_BYTES + 1))
    assert isinstance(too_big, Failure)
    assert too_big.failure().kind is RefusalKind.TOO_LARGE


def test_a_moment_that_does_not_exist_or_is_not_utc_is_refused() -> None:
    for moment in (
        "2026-02-30T10:00:00Z",
        "2026-10-29T25:00:00Z",
        "2026-10-29T10:00:00+01:00",
        "2026-10-29T10:00:00",
        "2026-10-29T10:00:00.1234567Z",
        "\N{FULLWIDTH DIGIT TWO}026-10-29T10:00:00Z",
    ):
        document = _valid()
        document["events"][0]["occurred_at"] = moment
        assert [item.index for item in _refusal(document).rejected_events] == [0], moment


def test_an_event_id_seen_twice_in_one_batch_is_accepted() -> None:
    document = _valid()
    document["events"].append(copy.deepcopy(document["events"][0]))
    assert is_successful(_read(document))


def test_every_invalid_event_is_named_once_with_its_id_when_it_has_one() -> None:
    document = _valid()
    good = document["events"][0]
    document["events"] = [
        good,
        {**good, "artifact_id": "a b"},
        "not an object",
        {**good, "event_id": "NOT-A-UUID", "artifact_type": "MCP"},
    ]
    refused = _refusal(document)
    assert [(item.index, item.event_id) for item in refused.rejected_events] == [
        (1, good["event_id"]),
        (2, None),
        (3, None),
    ]
    assert refused.rejected_events[1].reason == "an event must be an object"


@given(
    field=st.text(alphabet=string.ascii_lowercase + "_", min_size=1, max_size=20),
    value=st.one_of(st.text(max_size=20), st.integers(), st.none()),
)
def test_any_field_outside_the_contract_refuses_the_event(field: str, value: object) -> None:
    if field in EVENT_FIELDS:
        return
    document = _valid()
    document["events"][0][field] = value
    refused = _refusal(document)
    assert [item.index for item in refused.rejected_events] == [0]
    assert refused.rejected_events[0].reason == "a field the contract does not have"


@given(field=st.sampled_from(sorted(EVENT_FIELDS - {"artifact_version"})))
def test_every_required_field_is_required(field: str) -> None:
    document = _valid()
    del document["events"][0][field]
    assert _refusal(document).rejected_events[0].reason == f"missing {field}"


@given(event_type=st.sampled_from(list(EventType)), source=st.sampled_from(list(Source)))
def test_a_source_reports_only_its_own_event_types(event_type: EventType, source: Source) -> None:
    document = _valid()
    document["events"][0].update(event_type=event_type.value, source=source.value)
    assert is_successful(_read(document)) == (source in SOURCES_FOR[event_type])


@given(name=st.text(min_size=1, max_size=40))
def test_an_artifact_name_with_a_slash_space_or_non_ascii_is_refused(name: str) -> None:
    document = _valid()
    document["events"][0]["artifact_id"] = name
    allowed = set(string.ascii_letters + string.digits + "._@:+-")
    assert is_successful(_read(document)) == (set(name) <= allowed)


@given(
    item=events(),
    value=st.one_of(st.integers(), st.booleans(), st.floats(allow_nan=False), st.lists(st.none())),
)
def test_a_value_of_the_wrong_type_is_refused_never_coerced(item: Event, value: object) -> None:
    for field in ("event_id", "artifact_id", "artifact_type", "occurred_at", "artifact_version"):
        document = json.loads(request([item]))
        document["events"][0][field] = value
        assert [entry.index for entry in _refusal(document).rejected_events] == [0], field


_ENVELOPE = "the request must hold exactly schema_version, client_id, client_version and events"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda r: r.update(schema_version=2), "schema_version must be 1"),
        (lambda r: r.update(client_id="not-a-uuid"), "client_id must be a lowercase UUID"),
        (
            lambda r: r.update(client_version="a b"),
            "client_version must be 1-64 characters of [0-9A-Za-z.+-]",
        ),
        (lambda r: r.update(events=[]), f"events must be a list of 1 to {MAX_EVENTS} events"),
        (lambda r: r.update(hostname="laptop"), _ENVELOPE),
        (lambda r: r.pop("client_version"), _ENVELOPE),
    ],
)
def test_a_refused_envelope_says_what_the_contract_wants(
    change: Callable[[dict[str, Any]], object], reason: str
) -> None:
    document = _valid()
    change(document)
    assert _refusal(document).error == reason


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("event_id", "NOT-A-UUID", "event_id must be a lowercase UUID"),
        ("occurred_at", "yesterday", "occurred_at must be UTC as YYYY-MM-DDTHH:MM:SS[.ffffff]Z"),
        (
            "occurred_at",
            "2026-02-30T10:00:00Z",
            "occurred_at must be UTC as YYYY-MM-DDTHH:MM:SS[.ffffff]Z",
        ),
        ("event_type", "artifact.exploded", "unknown event_type"),
        ("source", "cron", "unknown source"),
        ("source", "aart_cli", "this source cannot report this event_type"),
        ("artifact_id", "a b", "artifact_id must be 1-200 characters of [0-9A-Za-z._@:+-]"),
        ("artifact_type", "MCP", "artifact_type must be 1-64 characters of [a-z0-9-]"),
        ("artifact_version", "a b", "artifact_version must be 1-64 characters of [0-9A-Za-z.+-]"),
    ],
)
def test_a_refused_event_says_which_field_and_what_it_must_be(
    field: str, value: str, reason: str
) -> None:
    document = _valid()
    document["events"][0][field] = value
    refused = _refusal(document)
    assert refused.error == "1 event(s) do not follow the contract"
    assert [item.reason for item in refused.rejected_events] == [reason]


def test_a_refusal_as_a_whole_says_why() -> None:
    def refused(raw: bytes) -> Refusal:
        result = read_batch(raw)
        assert isinstance(result, Failure)
        refusal = result.failure()
        assert isinstance(refusal, Refusal)
        return refusal

    assert refused(b"{not json").error == "the request body is not JSON"
    too_big = refused(b" " * (MAX_BODY_BYTES + 1))
    assert too_big.error == f"the request body is larger than {MAX_BODY_BYTES} bytes"
    assert refused(b" " * MAX_BODY_BYTES).kind is RefusalKind.NOT_JSON
