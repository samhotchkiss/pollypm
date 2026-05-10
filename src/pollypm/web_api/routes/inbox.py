"""Inbox read endpoints (Phase 1).

Implements ``GET /api/v1/inbox`` and ``GET /api/v1/inbox/{id}``.
Reply / archive ship in Phase 2 (#1548).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from pollypm.web_api.errors import not_found
from pollypm.web_api.models import InboxItemDetail, InboxListResponse
from pollypm.web_api.routes._deps import ConfigDep
from pollypm.web_api.service import get_inbox_item, list_inbox

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
    type: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query()] = None,
) -> InboxListResponse:
    items, next_cursor = list_inbox(
        config,
        project=project,
        type_filter=type,
        state_filter=state,
        limit=limit,
        cursor=cursor,
    )
    return InboxListResponse(items=items, next_cursor=next_cursor)


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
