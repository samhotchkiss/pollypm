"""PollyPM Service API — the public surface TUIs and CLIs consume.

The versioned implementation lives in :mod:`pollypm.service_api.v1`; this
package re-exports it so existing callers (``from pollypm.service_api
import PollyPMService``) keep working while new callers can pin to a
version (``from pollypm.service_api.v1 import PollyPMService``).

Direct ``from pollypm.supervisor import Supervisor`` outside
:mod:`pollypm.core` is deprecated — see ``docs/architecture.md`` and the
import-boundary test in ``tests/test_import_boundary.py``.

Callers that already hold a loaded :class:`pollypm.config.PollyPMConfig`
(e.g. the web API endpoints, which receive it via the FastAPI dependency
:class:`pollypm.web_api.routes._deps.ConfigDep`) construct a Supervisor
through :func:`build_supervisor`. The function lives in
:mod:`pollypm.service_api.v1` (the version-pinned facade module) so the
web-API layer never needs ``from pollypm.supervisor import Supervisor``
— the import-boundary test in ``tests/test_import_boundary.py``
enforces this.
"""

from pollypm.service_api.v1 import (
    PollyPMService,
    StatusSnapshot,
    build_supervisor,
    collect_plugin_load_errors,
    plan_launches_readonly,
    render_json,
)

__all__ = [
    "PollyPMService",
    "StatusSnapshot",
    "build_supervisor",
    "collect_plugin_load_errors",
    "plan_launches_readonly",
    "render_json",
]
