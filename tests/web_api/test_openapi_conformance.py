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

import yaml
from openapi_spec_validator import validate as validate_openapi


CONTRACT_PATH = Path(__file__).resolve().parents[2] / "docs" / "api" / "openapi.yaml"


# Paths Phase 1 (#1547) implements + the Phase 2 wedge (#1548). The
# remaining Phase 2 routes (approve / reject / register / plan / chat
# / inbox-reply / inbox-archive) exist in the contract but the
# FastAPI app is not yet expected to serve them; we assert
# implementation-side coverage for what's actually wired up and leave
# the remainder to the rest of Phase 2.
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
    # Phase 2 wedge — first write endpoint, see #1548.
    ("POST", "/tasks/{project}/{n}/queue"),
    # Phase 2 — chat GET endpoints (PR #2045).
    ("GET", "/chat/sessions"),
    ("GET", "/chat/{session_name}/messages"),
    # Chat-endpoints P3 (#2043) — POST send path.
    ("POST", "/chat/{session_name}/send"),
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


def test_chat_message_type_enum_matches_runtime() -> None:
    """``ChatMessageType`` in openapi.yaml must mirror ``MessageType``.

    PR #2045 v4 blocker 1: the static contract listed `notification`,
    `error`, `tool_call` (which the runtime never emits) and was
    missing `ask_user`, `file`, `subagent_spawn`, `system_event`,
    `tool_use` (which the runtime DOES emit). Generated clients
    branched on values that never appeared and crashed on values
    that did.

    This assertion pins the two enums to set-equality so future
    drift trips CI instead of a downstream client.
    """
    from pollypm.web_api.chat.envelope import MessageType

    contract = _load_contract()
    yaml_enum = set(
        contract["components"]["schemas"]["ChatMessageType"]["enum"]
    )
    runtime_enum = {member.value for member in MessageType}
    missing_in_yaml = runtime_enum - yaml_enum
    extra_in_yaml = yaml_enum - runtime_enum
    assert not missing_in_yaml, (
        f"ChatMessageType enum is missing runtime values: {missing_in_yaml}. "
        f"Update docs/api/openapi.yaml ChatMessageType enum to include them."
    )
    assert not extra_in_yaml, (
        f"ChatMessageType enum has values the runtime never emits: "
        f"{extra_in_yaml}. Remove them from docs/api/openapi.yaml."
    )


def test_static_yaml_does_not_advertise_idempotency_on_inbox_writes() -> None:
    """Static contract must match the implementation: no Idempotency-Key
    on inbox-write paths.

    Round 1 of #2060 stripped the header from the FastAPI handlers
    because no replay cache exists; round 2 (Codex blocker #1) caught
    that the static YAML still advertised it on every inbox-write
    path AND that the shared component description still claimed
    "server caches/replays responses for 24h". This test pins both
    fixes:

    - None of the 5 inbox-write paths reference
      ``#/components/parameters/IdempotencyKey``.
    - If the ``IdempotencyKey`` component still exists (the task
      endpoints — ``/approve``, ``/reject``, ``/queue`` — still
      accept the header for forward-compat with a future store) its
      description must be honest about NOT replaying responses
      today. The old "Server caches the response for 24h" string is
      the smoking gun and is forbidden.
    """
    contract = _load_contract()
    inbox_write_paths = [
        "/inbox/{id}/reply",
        "/inbox/{id}/archive",
        "/inbox/{id}/snooze",
        "/inbox/{id}/promote-to-task",
        "/inbox/{id}/mark-read",
    ]
    paths = contract.get("paths", {})
    for path in inbox_write_paths:
        ops = paths.get(path, {})
        post = ops.get("post", {})
        params = post.get("parameters", []) or []
        refs = [
            p.get("$ref", "") for p in params if isinstance(p, dict)
        ]
        assert not any(
            "IdempotencyKey" in ref for ref in refs
        ), (
            f"{path} static contract still advertises Idempotency-Key "
            "but the handler does not implement it (#2060). Strip the "
            "$ref or wire a real replay cache first."
        )

    # If the component still exists, its description must NOT claim
    # caching/replay (that promise was the actual contract bug).
    component = (
        contract.get("components", {}).get("parameters", {}).get(
            "IdempotencyKey"
        )
    )
    if component is not None:
        description = (component.get("description") or "").lower()
        assert "caches the response" not in description, (
            "IdempotencyKey component still promises a 24h replay "
            "cache that the server does not implement (#2060)."
        )
        assert "replays it on retry" not in description, (
            "IdempotencyKey component still promises retry replay "
            "that the server does not implement (#2060)."
        )


def test_static_yaml_declares_503_on_inbox_write_paths() -> None:
    """Static contract must declare 503 on every inbox-write path.

    Round 4 of #2060: the FastAPI route decorators in
    ``src/pollypm/web_api/routes/inbox.py`` list ``503`` for archive /
    snooze / promote-to-task / mark-read / reply, and the service
    helpers map ``_BACKING_STORE_ERRORS`` to the
    ``service_unavailable`` typed error envelope. The static
    ``docs/api/openapi.yaml`` was missing 503 entries on those paths,
    so a generated client would not know to handle a backing-store
    outage with the same error shape it sees from the live server.

    This test pins the contract: every inbox-write path must declare
    503 under its ``responses:`` block (either inline or via a shared
    ``$ref`` to ``#/components/responses/ServiceUnavailable``).
    """
    contract = _load_contract()
    inbox_write_paths = [
        "/inbox/{id}/reply",
        "/inbox/{id}/archive",
        "/inbox/{id}/snooze",
        "/inbox/{id}/promote-to-task",
        "/inbox/{id}/mark-read",
    ]
    paths = contract.get("paths", {})
    for path in inbox_write_paths:
        ops = paths.get(path, {})
        post = ops.get("post", {})
        responses = post.get("responses", {}) or {}
        # OpenAPI status codes are stringly-typed in YAML.
        assert "503" in responses, (
            f"{path} POST missing 503 response in static contract "
            "(#2060 round-4). The FastAPI handler maps backing-store "
            "errors to a 503 with the service_unavailable envelope; "
            "the YAML must say so too."
        )


def test_implementation_openapi_validates_as_31() -> None:
    """The auto-generated doc must itself be a valid OpenAPI 3.x doc."""
    # Re-read straight off the FastAPI app so we don't depend on the
    # `client` fixture's auth wiring.
    from pollypm.config import (
        AccountConfig,
        MemorySettings,
        PollyPMConfig,
        PollyPMSettings,
        ProjectSettings,
    )
    from pollypm.models import ProviderKind, RuntimeKind
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
