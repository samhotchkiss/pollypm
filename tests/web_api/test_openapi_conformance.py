"""OpenAPI conformance tests.

Two checks:

1. ``docs/api/openapi.yaml`` is itself a valid OpenAPI 3.1 document
   (per ``openapi_spec_validator``).
2. The implementation's auto-generated OpenAPI document
   (``GET /api/v1/openapi.json``) covers every Phase 1 path declared
   in the on-disk contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from openapi_spec_validator import validate as validate_openapi


CONTRACT_PATH = Path(__file__).resolve().parents[2] / "docs" / "api" / "openapi.yaml"


# Paths Phase 1 implements. Phase 2/3 paths exist in the contract but
# the FastAPI app is not yet expected to serve them; we assert
# implementation-side coverage only for the Phase 1 surface and leave
# the remainder to the later phases.
PHASE_1_PATHS: set[tuple[str, str]] = {
    ("GET", "/health"),
    ("GET", "/projects"),
    ("GET", "/projects/{key}"),
    ("GET", "/projects/{key}/tasks"),
    ("GET", "/projects/{key}/plan"),
    ("GET", "/tasks/{project}/{n}"),
    ("GET", "/inbox"),
    ("GET", "/inbox/{id}"),
    ("GET", "/events"),
}


def _load_contract() -> dict:
    return yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))


def test_contract_yaml_is_valid_openapi_31() -> None:
    contract = _load_contract()
    # ``validate`` raises on schema violations; passing means the
    # YAML is conformant 3.1.
    validate_openapi(contract)


def test_contract_lists_phase_1_paths() -> None:
    contract = _load_contract()
    paths = contract.get("paths", {})
    for method, path in PHASE_1_PATHS:
        assert path in paths, f"Spec missing path: {path}"
        ops = paths[path]
        assert method.lower() in {k.lower() for k in ops.keys()}, (
            f"Spec missing {method} {path}"
        )


def test_implementation_serves_phase_1_paths(client, auth_headers) -> None:
    # Spec §3 lists ``/health`` as the only auth-exempt endpoint; the
    # OpenAPI document must require the same bearer auth as the rest
    # of the API.
    unauth_response = client.get("/api/v1/openapi.json")
    assert unauth_response.status_code == 401

    response = client.get("/api/v1/openapi.json", headers=auth_headers)
    assert response.status_code == 200
    schema = response.json()
    paths = schema.get("paths", {})
    # The implementation paths are mounted under ``/api/v1`` so the
    # spec-relative path (``/projects``) becomes ``/api/v1/projects``.
    for method, path in PHASE_1_PATHS:
        full = f"/api/v1{path}"
        assert full in paths, f"Implementation missing path: {full}"
        op = paths[full]
        assert method.lower() in {k.lower() for k in op.keys()}, (
            f"Implementation missing {method} {full}"
        )


def test_implementation_openapi_validates_as_31() -> None:
    """The auto-generated doc must itself be a valid OpenAPI 3.x doc."""
    from fastapi.testclient import TestClient

    # Re-read straight off the FastAPI app so we don't depend on the
    # `client` fixture's auth wiring.
    from pollypm.config import (
        AccountConfig,
        MemorySettings,
        PollyPMConfig,
        PollyPMSettings,
        ProjectSettings,
    )
    from pollypm.models import KnownProject, ProjectKind, ProviderKind, RuntimeKind
    from pollypm.web_api import create_app

    base = Path(__file__).resolve().parent
    config = PollyPMConfig(
        project=ProjectSettings(name="P", root_dir=base, tmux_session="t",
                                workspace_root=base, base_dir=base / ".pollypm",
                                logs_dir=base / ".pollypm/logs",
                                snapshots_dir=base / ".pollypm/snapshots",
                                state_db=base / ".pollypm/state.db"),
        pollypm=PollyPMSettings(controller_account="codex_primary",
                                open_permissions_by_default=False,
                                failover_enabled=False,
                                failover_accounts=[],
                                heartbeat_backend="local",
                                scheduler_backend="inline",
                                lease_timeout_minutes=30),
        accounts={"codex_primary": AccountConfig(
            name="codex_primary", provider=ProviderKind.CODEX,
            email="codex@example.com", runtime=RuntimeKind.LOCAL,
            home=base / ".pollypm/homes/codex_primary",
        )},
        sessions={},
        projects={},
        memory=MemorySettings(backend="file"),
    )
    app = create_app(config=config, token_path=base / "tmp-token")
    # OpenAPI doc is now bearer-gated; pull it via the app helper
    # directly to keep this test independent of token wiring.
    raw = app.openapi()
    # FastAPI emits 3.1.0 by default for Pydantic v2; the validator
    # accepts 3.0 / 3.1 alike.
    assert raw.get("openapi", "").startswith("3.")
    validate_openapi(raw)
