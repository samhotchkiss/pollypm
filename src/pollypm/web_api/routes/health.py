"""``GET /api/v1/health`` — public liveness endpoint.

Per spec §3 health is exempt from auth; the Phase 1 app factory
applies bearer auth as a global dependency to every router *except*
this one.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter

from pollypm.audit.log import SCHEMA_VERSION
from pollypm.web_api.models import HealthResponse

router = APIRouter(tags=["Health"])


def _server_version() -> str:
    """Best-effort PollyPM package version.

    Falls back to ``"0.0.0"`` when the package metadata isn't
    queryable (e.g. running from a source tree without an installed
    distribution). Exposed in tests so they can assert the field is
    present without binding to a specific version string.
    """
    try:
        from importlib.metadata import version

        return version("pollypm")
    except Exception:  # noqa: BLE001
        return "0.0.0"


# Started timestamp captured at import. Pinned per-process so the
# field tracks "this server's uptime" rather than "now".
_STARTED_AT = datetime.now(timezone.utc)


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness + version info",
    operation_id="getHealth",
)
def get_health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        version=_server_version(),
        schema_version=SCHEMA_VERSION,
        started_at=_STARTED_AT,
    )
