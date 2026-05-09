"""PollyPM Web API server (Phase 1).

Implements the read endpoints, bearer-token auth, and Server-Sent Events
stream documented in :mod:`docs/web-api-spec.md`. The OpenAPI 3.1
contract at :mod:`docs/api/openapi.yaml` is authoritative — every
response shape and path here conforms to that document.

The package is a peer to the cockpit, not a wrapper. It composes
against :class:`pollypm.service_api.v1.PollyPMService` and
:func:`pollypm.work.factory.create_work_service` (#1389) so reads land
on the same backing store the cockpit uses; there is no second source
of truth.

Phase 1 ships:
- ``pm serve --port N`` CLI command.
- ``pm api regen-token`` token rotation.
- Bearer-token auth (``~/.pollypm/api-token``).
- All read endpoints from the §8 endpoint table marked Phase 1.
- ``GET /api/v1/events`` SSE stream with replay + filters + keep-alive.

Phase 2 (#1548) adds write endpoints; Phase 3 (#1549) adds doctor +
admin features. Both are out of scope here.
"""

from __future__ import annotations

from pollypm.web_api.app import create_app
from pollypm.web_api.token import (
    DEFAULT_TOKEN_PATH,
    ensure_token,
    load_token,
    regenerate_token,
)

__all__ = [
    "DEFAULT_TOKEN_PATH",
    "create_app",
    "ensure_token",
    "load_token",
    "regenerate_token",
]
