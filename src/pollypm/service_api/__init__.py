"""PollyPM Service API — the public surface TUIs and CLIs consume.

The versioned implementation lives in :mod:`pollypm.service_api.v1`; this
package re-exports it so existing callers (``from pollypm.service_api
import PollyPMService``) keep working while new callers can pin to a
version (``from pollypm.service_api.v1 import PollyPMService``).

Direct ``from pollypm.supervisor import Supervisor`` outside
:mod:`pollypm.core` is deprecated — see ``docs/architecture.md`` and the
import-boundary test in ``tests/test_import_boundary.py``.
"""

from pollypm.service_api.v1 import (
    InboxRouteDecision,
    PollyPMService,
    StatusSnapshot,
    render_json,
)

__all__ = [
    "InboxRouteDecision",
    "PollyPMService",
    "StatusSnapshot",
    "render_json",
]
