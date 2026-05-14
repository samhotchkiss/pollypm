"""SSE stream endpoint (Phase 1).

Implements ``GET /api/v1/events`` per `docs/web-api-spec.md` §4.

The route delegates to :func:`pollypm.web_api.sse.stream_audit_events`
for the actual stream construction; this module just adapts FastAPI's
request / streaming-response objects.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse

from pollypm.web_api.routes._deps import ConfigDep
from pollypm.web_api.sse import stream_audit_events

router = APIRouter(tags=["Events"])


@router.get(
    "/events",
    summary="Server-Sent Events stream of audit-log events",
    operation_id="streamEvents",
    responses={
        200: {
            "description": "SSE stream",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        }
    },
)
def stream_events_endpoint(
    request: Request,
    config: ConfigDep,
    since: Annotated[str | None, Query(description="ISO-8601 timestamp; replay events with ts > since.")] = None,
    project: Annotated[str | None, Query()] = None,
    event: Annotated[str | None, Query(description="Wildcard pattern matched against event name.")] = None,
    last_event_id: Annotated[
        str | None, Header(alias="Last-Event-ID", description="Reconnect cursor; takes precedence over since.")
    ] = None,
) -> StreamingResponse:
    generator = stream_audit_events(
        config,
        project=project,
        event_glob=event,
        since=since,
        last_event_id=last_event_id,
        # ``is_disconnected`` lets the producer bail when the
        # client goes away — important under TestClient where
        # ``StreamingResponse`` won't raise on its own.
        is_disconnected=request.is_disconnected,
    )
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            # SSE-friendly headers — disable buffering, allow long-lived
            # connection, and tell intermediaries we don't want chunk
            # rewriting.
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
