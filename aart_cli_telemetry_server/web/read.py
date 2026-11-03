"""The read API: artifacts, one artifact, the events behind them, and the aart-cli versions sending.

Imperative shell, as `app` is. The query parameters every route shares are read into one
`Selection`; each route calls one use case and writes what it returns.
"""

# No `from __future__ import annotations`: FastAPI reads each route's annotations, and the shared
# selection is a dependency defined inside `read_router`, which a string annotation cannot name.
import re
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi.responses import Response
from returns.io import IOResult

from aart_cli_telemetry_server.application import use_cases
from aart_cli_telemetry_server.application.ports import Clock, EventRepository
from aart_cli_telemetry_server.domain.artifacts import (
    MAX_EVENTS,
    ArtifactDetail,
    EventView,
    Order,
    Selection,
)
from aart_cli_telemetry_server.domain.events import EventType, Source, stamp
from aart_cli_telemetry_server.domain.stats import MAX_DAYS

from . import documents

__all__ = ["MAX_ARTIFACTS", "cursor", "next_before", "period", "read_router"]

Answer = Callable[[IOResult[Any, Any], Callable[[Any], Any]], Response]

_CURSOR = re.compile(r"(?P<moment>[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z),(?P<id>[0-9a-f-]{36})")
#: The most artifacts one list holds.
MAX_ARTIFACTS = 1000


def _utc(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def cursor(raw: str | None) -> tuple[datetime, str] | None:
    """A page's `next_before` as the moment and id it names; `ValueError` when it is not one."""

    if raw is None:
        return None
    matched = _CURSOR.fullmatch(raw)
    if matched is None:
        raise ValueError("before is the next_before a page returned")
    return datetime.fromisoformat(matched["moment"].replace("Z", "+00:00")), matched["id"]


def next_before(found: tuple[EventView, ...], limit: int) -> str | None:
    """Where the page after a full one starts; `None` after the last."""

    if len(found) < limit:
        return None
    last = found[-1]
    return f"{stamp(last.occurred_at)},{last.event_id}"


def period(
    now: datetime,
    *,
    days: int = 30,
    since: datetime | None = None,
    until: datetime | None = None,
    source: Source | None = None,
    client_version: str | None = None,
    automated: bool | None = None,
) -> Selection:
    """The events of the last `days` before `now`, or from `since`, and of who sent them."""

    return Selection(
        since=_utc(since) or now.astimezone(UTC) - timedelta(days=days),
        until=_utc(until),
        source=source,
        client_version=client_version,
        automated=automated,
    )


def read_router(repository: EventRepository, clock: Clock, answer: Answer) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["read"])

    def selection(
        *,
        days: Annotated[
            int, Query(ge=1, le=MAX_DAYS, description="The last this many days, unless since.")
        ] = 30,
        since: Annotated[
            datetime | None, Query(description="From this moment; UTC when no offset is given.")
        ] = None,
        until: Annotated[datetime | None, Query(description="Before this moment.")] = None,
        source: Annotated[Source | None, Query(description="Who sent the events.")] = None,
        client_version: Annotated[
            str | None, Query(max_length=64, description="Sent by this aart-cli version.")
        ] = None,
        automated: Annotated[
            bool | None,
            Query(description="Only automation such as CI (true), only people (false)."),
        ] = None,
    ) -> Selection:
        return period(
            clock(),
            days=days,
            since=since,
            until=until,
            source=source,
            client_version=client_version,
            automated=automated,
        )

    Period = Annotated[Selection, Depends(selection)]

    def of_kind(
        selected: Period,
        artifact_type: Annotated[
            str | None, Query(max_length=64, description="Only artifacts of this kind.")
        ] = None,
    ) -> Selection:
        return replace(selected, artifact_type=artifact_type)

    # The detail's kind is in its path, so only the lists take it as a query parameter.
    Selected = Annotated[Selection, Depends(of_kind)]

    @router.get("/artifacts")
    def artifacts(
        selected: Selected,
        sort: Annotated[Order, Query(description="Most first; ties by name.")] = Order.INSTALLS,
        limit: Annotated[int, Query(ge=1, le=MAX_ARTIFACTS)] = 100,
        q: Annotated[
            str | None, Query(max_length=200, description="Names holding this, any case.")
        ] = None,
    ) -> Response:
        """Every artifact aart-cli installed, updated, removed or a harness used: how often, how
        many installations hold it now, its latest version, and, for a plugin, its skills' uses.

        A plugin's skill is listed as a skill named `<plugin>:<skill>`, with `plugin` set.
        """

        return answer(
            use_cases.artifacts(selected, sort, limit, q, repository),
            lambda found: {"artifacts": [documents.artifact_document(item) for item in found]},
        )

    @router.get("/artifacts/{artifact_type}/{artifact_id}")
    def artifact(
        selected: Period,
        artifact_type: Annotated[str, Path(max_length=64, examples=["plugin"])],
        artifact_id: Annotated[str, Path(max_length=200, examples=["superpowers"])],
    ) -> Response:
        """One artifact: its counts, versions, days and sources, and, for a plugin, a row for
        each of its skills. 404 when nothing selected is about it or its members."""

        result = use_cases.artifact(selected, artifact_type, artifact_id, repository)

        def body(found: ArtifactDetail | None) -> Any:
            if found is None:
                raise HTTPException(404, "nothing selected is about this artifact")
            return documents.artifact_detail_document(found)

        return answer(result, body)

    @router.get("/events")
    def events(
        *,
        selected: Selected,
        event_type: Annotated[EventType | None, Query()] = None,
        artifact_id: Annotated[str | None, Query(max_length=200)] = None,
        members: Annotated[
            bool, Query(description="With artifact_id, its plugin's skills too.")
        ] = False,
        limit: Annotated[int, Query(ge=1, le=MAX_EVENTS)] = 100,
        before: Annotated[
            str | None, Query(description="The next_before of the previous page.")
        ] = None,
    ) -> Response:
        """The events kept, newest first, a page at a time."""

        if members and artifact_id is None:
            raise HTTPException(422, "members needs artifact_id")
        chosen = Selection(
            since=selected.since,
            until=selected.until,
            event_type=event_type,
            source=selected.source,
            client_version=selected.client_version,
            automated=selected.automated,
            artifact_type=selected.artifact_type,
            artifact_id=artifact_id,
            members=members,
        )

        try:
            page = cursor(before)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        return answer(
            use_cases.events(chosen, limit, page, repository),
            lambda found: documents.events_document(found, next_before(found, limit)),
        )

    @router.get("/client-versions")
    def client_versions(selected: Selected) -> Response:
        """The aart-cli versions the selected events were sent from: most installations first."""

        return answer(
            use_cases.client_versions(selected, repository),
            documents.client_versions_document,
        )

    return router
