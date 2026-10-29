"""The HTTP application: the ingest endpoint, health, the dashboard and its aggregates.

Imperative shell. Each route reads the request, calls one use case with the repository and clock
it was built with, and maps the `returns` container to a status and a body. No route decides what
an event is or how anything is counted.
"""

from __future__ import annotations

from datetime import UTC, datetime
from importlib import resources
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from returns.io import IOResult, IOSuccess
from returns.result import Failure, Success

from aart_cli_telemetry_server.application import use_cases
from aart_cli_telemetry_server.application.ports import Clock, EventRepository, StorageError
from aart_cli_telemetry_server.domain.contract import (
    MAX_BODY_BYTES,
    Refusal,
    RefusalKind,
    too_large,
)
from aart_cli_telemetry_server.domain.events import stamp
from aart_cli_telemetry_server.domain.stats import MAX_DAYS, MAX_TOP
from aart_cli_telemetry_server.effects import perform

__all__ = ["create_app"]

_STATIC = resources.files("aart_cli_telemetry_server.web") / "static"
_STATIC_FILES = {
    "dashboard.js": "text/javascript",
    "dashboard.css": "text/css",
    "chart.umd.min.js": "text/javascript",
}

#: The dashboard's own headers. Everything it loads comes from this server.
_DASHBOARD_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

_REFUSAL_STATUS = {RefusalKind.TOO_LARGE: 413, RefusalKind.NOT_JSON: 400, RefusalKind.INVALID: 422}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _unavailable(_: StorageError) -> Response:
    return JSONResponse({"error": "the database is unavailable"}, status_code=503)


def _refused(refusal: Refusal) -> Response:
    body: dict[str, Any] = {"error": refusal.error}
    if refusal.kind is RefusalKind.INVALID:
        body["rejected_events"] = [
            {"index": item.index, "event_id": item.event_id, "reason": item.reason}
            for item in refusal.rejected_events
        ]
    return JSONResponse(body, status_code=_REFUSAL_STATUS[refusal.kind])


def _answer(result: IOResult[Any, Any], body: Any) -> Response:
    """200 with `body(value)`, or the failure's own response."""

    # The shell is where effects happen: this is the one place a result is taken out of IO.
    match perform(result):
        case Success(value):
            return JSONResponse(body(value))
        case Failure(failure):
            return _refused(failure) if isinstance(failure, Refusal) else _unavailable(failure)
    raise AssertionError("a result is a success or a failure")  # pragma: no cover


async def _body(request: Request) -> bytes | None:
    """The request body, or `None` once it is larger than the contract allows."""

    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > MAX_BODY_BYTES):
        return None
    received = bytearray()
    async for chunk in request.stream():
        received.extend(chunk)
        if len(received) > MAX_BODY_BYTES:
            return None
    return bytes(received)


def create_app(repository: EventRepository, clock: Clock = _utc_now) -> FastAPI:
    app = FastAPI(
        title="aart-cli-telemetry-server", docs_url=None, redoc_url=None, openapi_url=None
    )
    days_query = Query(default=30, ge=1, le=MAX_DAYS)

    @app.post("/api/v1/events/batch")
    async def ingest(request: Request) -> Response:
        raw = await _body(request)
        if raw is None:
            return _refused(too_large())
        return _answer(
            use_cases.ingest(raw, repository, clock),
            lambda acknowledgement: {"acknowledged_event_ids": list(acknowledgement.event_ids)},
        )

    @app.get("/healthz")
    def healthz() -> Response:
        if isinstance(repository.ping(), IOSuccess):
            return JSONResponse({"status": "ok"})
        return JSONResponse({"status": "unavailable"}, status_code=503)

    @app.get("/api/v1/stats/summary")
    def summary(days: int = days_query) -> Response:
        return _answer(
            use_cases.summary(days, repository, clock),
            lambda found: {
                "days": days,
                "uses": found.uses,
                "installs": found.installs,
                "updates": found.updates,
                "removals": found.removals,
                "active_clients": found.active_clients,
                "using_clients": found.using_clients,
                "used_artifacts": found.used_artifacts,
            },
        )

    @app.get("/api/v1/stats/top-artifacts")
    def top_artifacts(
        days: int = days_query, limit: int = Query(default=20, ge=1, le=MAX_TOP)
    ) -> Response:
        return _answer(
            use_cases.top_artifacts(days, limit, repository, clock),
            lambda found: {
                "days": days,
                "artifacts": [
                    {
                        "artifact_type": item.artifact_type,
                        "artifact_id": item.artifact_id,
                        "uses": item.uses,
                        "clients": item.clients,
                        "installs": item.installs,
                        "last_used": stamp(item.last_used),
                    }
                    for item in found
                ],
            },
        )

    @app.get("/api/v1/stats/daily")
    def daily(days: int = days_query) -> Response:
        return _answer(
            use_cases.daily_counts(days, repository, clock),
            lambda found: {
                "days": days,
                "daily": [
                    {
                        "day": item.day.isoformat(),
                        "uses": item.uses,
                        "installs": item.installs,
                        "updates": item.updates,
                        "removals": item.removals,
                        "active_clients": item.active_clients,
                    }
                    for item in found
                ],
            },
        )

    @app.get("/api/v1/stats/by-source")
    def by_source(days: int = days_query) -> Response:
        return _answer(
            use_cases.artifacts_by_source(days, repository, clock),
            lambda found: {
                "days": days,
                "by_source": [
                    {
                        "source": item.source.value,
                        "artifact_type": item.artifact_type,
                        "uses": item.uses,
                    }
                    for item in found
                ],
            },
        )

    @app.get("/", include_in_schema=False)
    def root() -> Response:
        return RedirectResponse("/dashboard")

    @app.get("/dashboard", include_in_schema=False)
    def dashboard() -> Response:
        page = (_STATIC / "dashboard.html").read_bytes()
        return Response(page, media_type="text/html; charset=utf-8", headers=_DASHBOARD_HEADERS)

    @app.get("/static/{name}", include_in_schema=False)
    def static(name: str) -> Response:
        media = _STATIC_FILES.get(name)
        if media is None:
            return Response(status_code=404)
        with resources.as_file(_STATIC / name) as path:
            return FileResponse(path, media_type=media, headers=_DASHBOARD_HEADERS)

    return app
