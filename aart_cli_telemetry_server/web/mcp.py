"""A read-only MCP server at `/mcp`: an agent asks what the read API and the dashboard answer.

MCP's Streamable HTTP transport, answering each request with one JSON response and never opening a
stream. The JSON-RPC is written here rather than taken from an SDK: four methods are needed --
`initialize`, `ping`, `tools/list` and `tools/call` -- and every tool only reads, through the same
use cases and documents the HTTP routes use. Nothing here can write an event.

A browser page on another site must not reach a server on this machine (DNS rebinding), so a
request whose `Origin` names another host than the one it was sent to is refused.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from returns.io import IOResult
from returns.result import Failure, Success

from aart_cli_telemetry_server.application import use_cases
from aart_cli_telemetry_server.application.ports import Clock, EventRepository
from aart_cli_telemetry_server.domain.artifacts import (
    MAX_DIMENSIONS,
    MAX_EVENTS,
    MAX_GROUPS,
    Dimension,
    Order,
    Selection,
)
from aart_cli_telemetry_server.domain.events import EventType, Interface, Scope, Source
from aart_cli_telemetry_server.domain.stats import MAX_DAYS, MAX_TOP
from aart_cli_telemetry_server.effects import perform

from . import documents
from .read import MAX_ARTIFACTS, cursor, next_before, period

__all__ = ["MAX_REQUEST_BYTES", "PROTOCOL_VERSIONS", "mcp_router"]

#: The protocol versions this server speaks, newest first; a client asking for another is told
#: the newest.
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_REQUEST_BYTES = 65_536

_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602

_INSTRUCTIONS = (
    "Read-only statistics of the artifacts aart-cli installs: which were installed, updated, "
    "removed and used, by how many installations, on which versions, and the failures aart-cli "
    "met. An installation is a pseudonymous id, never a person. A plugin's skill is the artifact "
    "`skill` named `<plugin>:<skill>`. Every tool can leave out CI with automated=false. Start "
    "with list_artifacts; get_artifact gives one in detail; breakdown groups by harness, scope, "
    "Registry, interface and more."
)


class _Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Period(_Arguments):
    days: int = Field(30, ge=1, le=MAX_DAYS, description="The last this many days, unless since.")
    since: datetime | None = Field(None, description="From this moment; UTC without an offset.")
    until: datetime | None = Field(None, description="Before this moment.")
    source: Source | None = Field(None, description="Who sent the events.")
    client_version: str | None = Field(
        None, max_length=64, description="Only events sent by this aart-cli version."
    )
    automated: bool | None = Field(
        None, description="Only automation such as CI (true), only people (false)."
    )
    harness: str | None = Field(
        None, max_length=64, description="Only this harness: claude, opencode, ..."
    )
    scope: Scope | None = Field(None, description="Only user or project scope.")
    registry: str | None = Field(None, max_length=64, description="Only this Registry alias.")
    interface: Interface | None = Field(
        None, description="Only what was asked for in the TUI or the CLI."
    )

    def selection(self, now: datetime) -> Selection:
        return period(
            now,
            days=self.days,
            since=self.since,
            until=self.until,
            source=self.source,
            client_version=self.client_version,
            automated=self.automated,
            harness=self.harness,
            scope=self.scope,
            registry=self.registry,
            interface=self.interface,
        )


class _Kinded(_Period):
    artifact_type: str | None = Field(
        None, max_length=64, description="Only artifacts of this kind, such as plugin or skill."
    )

    def selection(self, now: datetime) -> Selection:
        return replace(super().selection(now), artifact_type=self.artifact_type)


class _ListArtifacts(_Kinded):
    sort: Order = Field(Order.INSTALLS, description="Most first; ties by name.")
    limit: int = Field(50, ge=1, le=MAX_ARTIFACTS)
    q: str | None = Field(None, max_length=200, description="Names holding this, any case.")


class _GetArtifact(_Period):
    artifact_type: str = Field(max_length=64, description="Its kind, such as plugin or mcp.")
    artifact_id: str = Field(max_length=200, description="Its name, such as superpowers.")


class _ListEvents(_Kinded):
    event_type: EventType | None = None
    artifact_id: str | None = Field(None, max_length=200)
    members: bool = Field(False, description="With artifact_id, its plugin's skills too.")
    limit: int = Field(50, ge=1, le=MAX_EVENTS)
    before: str | None = Field(None, description="The next_before of the previous page.")


class _Breakdown(_Kinded):
    by: list[Dimension] = Field(
        min_length=1,
        max_length=MAX_DIMENSIONS,
        description="What to group by, 1 to 3 distinct dimensions.",
    )
    event_type: EventType | None = None
    artifact_id: str | None = Field(None, max_length=200)
    members: bool = Field(False, description="With artifact_id, its plugin's skills too.")
    limit: int = Field(100, ge=1, le=MAX_GROUPS)


class _Days(_Arguments):
    days: int = Field(30, ge=1, le=MAX_DAYS)


class _Top(_Days):
    limit: int = Field(20, ge=1, le=MAX_TOP)


Run = Callable[[Any, EventRepository, Clock], IOResult[dict[str, Any], Any]]


@dataclass(frozen=True, slots=True)
class _Tool:
    name: str
    description: str
    arguments: type[_Arguments]
    run: Run

    def listed(self) -> dict[str, Any]:
        schema = self.arguments.model_json_schema()
        schema.pop("title", None)
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": schema,
            "annotations": {"readOnlyHint": True, "openWorldHint": False},
        }


def _events(arguments: _ListEvents, repository: EventRepository, clock: Clock) -> Any:
    if arguments.members and arguments.artifact_id is None:
        raise ValueError("members needs artifact_id")
    chosen = replace(
        arguments.selection(clock()),
        event_type=arguments.event_type,
        artifact_id=arguments.artifact_id,
        members=arguments.members,
    )
    return use_cases.events(chosen, arguments.limit, cursor(arguments.before), repository).map(
        lambda found: documents.events_document(found, next_before(found, arguments.limit))
    )


def _breakdown(arguments: _Breakdown, repository: EventRepository, clock: Clock) -> Any:
    if arguments.members and arguments.artifact_id is None:
        raise ValueError("members needs artifact_id")
    if len(set(arguments.by)) != len(arguments.by):
        raise ValueError("each dimension once")
    chosen = replace(
        arguments.selection(clock()),
        event_type=arguments.event_type,
        artifact_id=arguments.artifact_id,
        members=arguments.members,
    )
    by = tuple(arguments.by)
    return use_cases.breakdown(chosen, by, arguments.limit, repository).map(
        lambda found: documents.breakdown_document(by, found)
    )


def _artifact(arguments: _GetArtifact, repository: EventRepository, clock: Clock) -> Any:
    def document(found: Any) -> dict[str, Any]:
        if found is None:
            raise LookupError(
                f"nothing selected is about {arguments.artifact_type} {arguments.artifact_id}"
            )
        return documents.artifact_detail_document(found)

    return use_cases.artifact(
        arguments.selection(clock()), arguments.artifact_type, arguments.artifact_id, repository
    ).map(document)


_TOOLS = (
    _Tool(
        "summary",
        "Totals over a period: uses, installs, updates, removals, active and automated "
        "installations, uses of tools aart-cli did not install, and failures.",
        _Days,
        lambda a, r, c: use_cases.summary(a.days, r, c).map(
            lambda found: documents.summary_document(a.days, found)
        ),
    ),
    _Tool(
        "list_artifacts",
        "Every artifact aart-cli installed, updated, removed or a harness used, with its counts, "
        "how many installations hold it now, its latest version and, for a plugin, its skills' "
        "uses. Filter by kind, name, period, sender and aart-cli version; order by installs, "
        "uses, holders, recent or name.",
        _ListArtifacts,
        lambda a, r, c: use_cases.artifacts(a.selection(c()), a.sort, a.limit, a.q, r).map(
            lambda found: {"artifacts": [documents.artifact_document(item) for item in found]}
        ),
    ),
    _Tool(
        "get_artifact",
        "One artifact in detail: counts, holders, versions, daily counts, sources and, for a "
        "plugin, a row for each of its skills.",
        _GetArtifact,
        _artifact,
    ),
    _Tool(
        "list_events",
        "The events kept, newest first, a page at a time: what happened to which artifact, when, "
        "from which installation and aart-cli version. Pass next_before as before for the next "
        "page.",
        _ListEvents,
        _events,
    ),
    _Tool(
        "breakdown",
        "Events grouped by up to three dimensions -- harness, scope (user or project), Registry, "
        "interface (TUI or CLI), via (direct, collection, plugin), collection, tool, automated, "
        "event_type, artifact, version, previous_version, client_version, day, os, arch, "
        "python -- with how many installations sent each group. Use it for questions such as "
        "which harness installs what, or what people rather than CI install.",
        _Breakdown,
        _breakdown,
    ),
    _Tool(
        "client_versions",
        "The aart-cli versions events were sent from: how many installations, how many events, "
        "and when last.",
        _Kinded,
        lambda a, r, c: use_cases.client_versions(a.selection(c()), r).map(
            documents.client_versions_document
        ),
    ),
    _Tool(
        "failures",
        "Defects aart-cli met, grouped by fingerprint: where, the exception and its causes, "
        "aart-cli's own frames, how often, on which versions and platforms.",
        _Top,
        lambda a, r, c: use_cases.top_failures(a.days, a.limit, r, c).map(
            lambda found: documents.failures_document(a.days, found)
        ),
    ),
    _Tool(
        "unmanaged_tools",
        "Skills and MCPs used that aart-cli did not install, by the name the harness gave them.",
        _Top,
        lambda a, r, c: use_cases.top_unmanaged(a.days, a.limit, r, c).map(
            lambda found: documents.top_unmanaged_document(a.days, found)
        ),
    ),
    _Tool(
        "daily",
        "Per UTC day: uses, installs, updates, removals, and active people's and automated "
        "installations.",
        _Days,
        lambda a, r, c: use_cases.daily_counts(a.days, r, c).map(
            lambda found: documents.daily_document(a.days, found)
        ),
    ),
)
_BY_NAME = {tool.name: tool for tool in _TOOLS}


def _error(identifier: object, code: int, message: str, status: int = 200) -> Response:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": identifier, "error": {"code": code, "message": message}},
        status_code=status,
    )


def _text(document: dict[str, Any], *, failed: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {
        "content": [{"type": "text", "text": json.dumps(document, ensure_ascii=False)}],
        "isError": failed,
    }
    if not failed:
        result["structuredContent"] = document
    return result


def _call(params: object, repository: EventRepository, clock: Clock) -> dict[str, Any] | None:
    """A tool's result; `None` when no such tool exists. A tool's own failure is a result too."""

    if not isinstance(params, dict) or not isinstance(params.get("name"), str):
        return None
    tool = _BY_NAME.get(params["name"])
    if tool is None:
        return None
    try:
        arguments = tool.arguments.model_validate(params.get("arguments") or {})
        outcome = perform(tool.run(arguments, repository, clock))
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in item['loc']) or 'arguments'}: {item['msg']}"
            for item in error.errors()
        )
        return _text({"error": problems}, failed=True)
    except (ValueError, LookupError) as error:
        return _text({"error": str(error)}, failed=True)
    match outcome:
        case Success(document):
            return _text(document)
        case Failure(_):
            return _text({"error": "the database is unavailable"}, failed=True)
    raise AssertionError("a result is a success or a failure")  # pragma: no cover


def _foreign(request: Request) -> bool:
    origin = request.headers.get("origin")
    if origin is None:
        return False
    return urlsplit(origin).netloc.lower() != request.headers.get("host", "").lower()


def mcp_router(repository: EventRepository, clock: Clock) -> APIRouter:
    router = APIRouter()

    @router.post("/mcp", tags=["mcp"])
    async def mcp(request: Request) -> Response:
        """MCP over Streamable HTTP: one JSON-RPC request in, one JSON response out."""

        if _foreign(request):
            return _error(None, _INVALID_REQUEST, "this origin may not call this server", 403)
        raw = await request.body()
        if len(raw) > MAX_REQUEST_BYTES:
            return _error(None, _INVALID_REQUEST, "the request is too large", 413)
        try:
            message = json.loads(raw)
        except ValueError:
            return _error(None, _PARSE_ERROR, "the request is not JSON", 400)
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, _INVALID_REQUEST, "one JSON-RPC 2.0 message per request", 400)
        if "id" not in message or "method" not in message:
            # A notification, or a client's answer to something: nothing to answer.
            return Response(status_code=202)
        identifier, method, params = message["id"], message["method"], message.get("params")
        result: dict[str, Any] | None
        if method == "initialize":
            asked = params.get("protocolVersion") if isinstance(params, dict) else None
            result = {
                "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "aart-cli-telemetry", "version": "1"},
                "instructions": _INSTRUCTIONS,
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [tool.listed() for tool in _TOOLS]}
        elif method == "tools/call":
            result = _call(params, repository, clock)
            if result is None:
                return _error(identifier, _INVALID_PARAMS, "no such tool")
        else:
            return _error(identifier, _METHOD_NOT_FOUND, f"no method {method!r}")
        return JSONResponse({"jsonrpc": "2.0", "id": identifier, "result": result})

    @router.get("/mcp", include_in_schema=False)
    def no_stream() -> Response:
        # This server never sends a request or notification of its own, so it offers no stream.
        return Response(status_code=405, headers={"Allow": "POST"})

    return router
