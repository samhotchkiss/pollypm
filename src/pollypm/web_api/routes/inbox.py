"""Inbox endpoints (Phase 1 reads + Phase 2 writes).

Phase 1 (#1547) shipped ``GET /api/v1/inbox`` and
``GET /api/v1/inbox/{id}``. Phase 2 (#1548) layers in the write
surface — archive / snooze / promote-to-task / mark-read / reply —
all routed through the same canonical work-service writers the
cockpit uses (#1389). The Web API is never a second writer surface;
these handlers are thin adapters that translate work-service
exceptions into the typed error envelope (§6).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from pollypm.web_api.errors import not_found
from pollypm.web_api.models import (
    InboxArchiveRequest,
    InboxItemDetail,
    InboxListResponse,
    InboxMarkReadRequest,
    InboxPromoteRequest,
    InboxReplyRequest,
    InboxSnoozeRequest,
    TaskDetail,
)
from pollypm.web_api.routes._deps import ConfigDep
from pollypm.web_api.service import (
    archive_inbox_item,
    get_inbox_item,
    list_inbox,
    mark_read_inbox_item,
    promote_inbox_to_task,
    reply_inbox_item,
    snooze_inbox_item,
)

router = APIRouter(tags=["Inbox"])


@router.get(
    "/inbox",
    response_model=InboxListResponse,
    summary="List inbox items",
    operation_id="listInbox",
)
def list_inbox_endpoint(
    config: ConfigDep,
    project: Annotated[str | None, Query()] = None,
    type: Annotated[
        str | None,
        Query(
            description=(
                "Inbox item type or structured kind, e.g. message, "
                "plan_review, approval_request, manual_decision."
            )
        ),
    ] = None,
    state: Annotated[str | None, Query()] = None,
    include_drafts: Annotated[
        bool,
        Query(
            description=(
                "Include notify-only draft/FYI task rows. Defaults to false "
                "to match `pm inbox --project`; pass true for the wider "
                "legacy inbox view."
            )
        ),
    ] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query()] = None,
) -> InboxListResponse:
    page = list_inbox(
        config,
        project=project,
        type_filter=type,
        state_filter=state,
        include_drafts=include_drafts,
        limit=limit,
        cursor=cursor,
    )
    return InboxListResponse(
        items=page.items,
        total=page.total,
        has_more=page.has_more,
        unread_count=page.unread_count,
        next_cursor=page.next_cursor,
    )


@router.get(
    # ``{id:path}`` so an inbox ID like ``myproj/1`` (forward-slash
    # separated, mirrors task IDs) round-trips without the client
    # having to URL-encode the slash. The list endpoint returns those
    # IDs verbatim, so the detail route must accept them verbatim.
    "/inbox/{id:path}",
    response_model=InboxItemDetail,
    summary="Inbox item detail with full thread messages",
    operation_id="getInboxItem",
)
def get_inbox_item_endpoint(id: str, config: ConfigDep) -> InboxItemDetail:
    detail = get_inbox_item(config, id)
    if detail is None:
        raise not_found(f"Inbox item not found: {id}")
    return detail


# ---------------------------------------------------------------------------
# Phase 2 — inbox write endpoints
#
# Each POST handler:
#   * routes through a service-layer helper that opens
#     :func:`create_work_service` once per call (mirrors how queue_task
#     wedge works — same canonical writer the cockpit uses).
#   * returns the post-mutation task envelope so the client refreshes
#     UI without a follow-up GET.
#
# Idempotency: an earlier draft of these handlers advertised an
# optional ``Idempotency-Key`` request header on every POST. There is
# no idempotency store wired into the API today (grep for
# ``idempotency`` returns only docs/comments), so the header would
# have been silently ignored — a retry after a network drop could
# create duplicate promoted tasks, replies, and snooze rows while the
# client reasonably believed the header protected it. The header is
# intentionally NOT declared here; a real idempotency cache is
# deferred until a future PR can wire it through every non-idempotent
# verb (see #2060 review for context).
# ---------------------------------------------------------------------------


@router.post(
    "/inbox/{id:path}/archive",
    response_model=TaskDetail,
    summary="Archive (close) an inbox item",
    operation_id="archiveInboxItem",
    responses={
        "401": {"description": "Missing or invalid bearer token."},
        "404": {"description": "Project or inbox item not found."},
        "409": {"description": "Item is already in a terminal state."},
        "503": {"description": "Backing store unavailable."},
    },
)
def archive_inbox_item_endpoint(
    id: str,
    config: ConfigDep,
    body: InboxArchiveRequest | None = None,
) -> TaskDetail:
    reason = body.reason if body is not None else None
    return archive_inbox_item(config, id, reason=reason)


@router.post(
    "/inbox/{id:path}/snooze",
    response_model=TaskDetail,
    summary="Snooze an inbox item until a future time",
    operation_id="snoozeInboxItem",
    responses={
        "400": {"description": "Snooze window invalid (missing / past / >30d)."},
        "401": {"description": "Missing or invalid bearer token."},
        "404": {"description": "Project or inbox item not found."},
        "409": {"description": "Item is in a terminal state and cannot be snoozed."},
        "503": {"description": "Backing store unavailable."},
    },
)
def snooze_inbox_item_endpoint(
    id: str,
    body: InboxSnoozeRequest,
    config: ConfigDep,
) -> TaskDetail:
    return snooze_inbox_item(
        config,
        id,
        duration_seconds=body.duration_seconds,
        until=body.until,
        reason=body.reason,
    )


@router.post(
    "/inbox/{id:path}/promote-to-task",
    response_model=TaskDetail,
    summary="Promote an inbox item into a new actionable task",
    operation_id="promoteInboxItem",
    responses={
        "401": {"description": "Missing or invalid bearer token."},
        "404": {"description": "Source or destination project not found."},
        "503": {"description": "Backing store unavailable."},
    },
)
def promote_inbox_item_endpoint(
    id: str,
    config: ConfigDep,
    body: InboxPromoteRequest | None = None,
) -> TaskDetail:
    if body is None:
        body = InboxPromoteRequest()
    return promote_inbox_to_task(
        config,
        id,
        target_project=body.project,
        prompt=body.prompt,
        title=body.title,
    )


@router.post(
    "/inbox/{id:path}/mark-read",
    response_model=TaskDetail,
    summary="Record a read-marker on an inbox item",
    operation_id="markInboxItemRead",
    responses={
        "401": {"description": "Missing or invalid bearer token."},
        "404": {"description": "Project or inbox item not found."},
        "503": {"description": "Backing store unavailable."},
    },
)
def mark_read_inbox_item_endpoint(
    id: str,
    config: ConfigDep,
    body: InboxMarkReadRequest | None = None,
) -> TaskDetail:
    actor = (body.actor if body is not None else None) or "api"
    return mark_read_inbox_item(config, id, actor=actor)


@router.post(
    "/inbox/{id:path}/reply",
    response_model=TaskDetail,
    summary="Append a reply to an inbox thread",
    operation_id="replyInboxItem",
    responses={
        "401": {"description": "Missing or invalid bearer token."},
        "404": {"description": "Project or inbox item not found."},
        "422": {"description": "Reply body failed validation."},
        "503": {"description": "Backing store unavailable."},
    },
)
def reply_inbox_item_endpoint(
    id: str,
    body: InboxReplyRequest,
    config: ConfigDep,
) -> TaskDetail:
    return reply_inbox_item(
        config, id, body=body.body, owner=body.owner,
    )
