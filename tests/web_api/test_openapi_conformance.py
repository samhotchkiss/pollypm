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
    # Sessions admin interrupt (web UI stop-agent button).
    ("POST", "/sessions/{name}/interrupt"),
    # Phase 6 P0 — cross-project flat task list (spec §5.1).
    ("GET", "/tasks"),
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


def test_static_yaml_does_not_advertise_idempotency_or_ifmatch_on_tasks() -> None:
    """Static contract must match the implementation: no Idempotency-Key
    or If-Match on task-state-mutation paths.

    #2064 round-1 stripped both headers from the FastAPI handlers for
    /queue, /claim, /cancel, /reassign, and PATCH /tasks/{p}/{n}
    (mirrors #2060 round-1) because no replay cache or version-token
    enforcement exists. Generated clients reading the static YAML
    must see the same contract — otherwise they'd send headers the
    server silently discards (Idempotency-Key) or thinks it honors
    (If-Match concurrency).
    """
    contract = _load_contract()
    paths = contract.get("paths", {})

    # POST /tasks/{p}/{n}/{claim,cancel,reassign,queue}
    task_post_paths = [
        "/tasks/{project}/{n}/queue",
        "/tasks/{project}/{n}/claim",
        "/tasks/{project}/{n}/cancel",
        "/tasks/{project}/{n}/reassign",
    ]
    for path in task_post_paths:
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
            "but the handler does not implement it (#2064). Strip the "
            "$ref or wire a real replay cache first."
        )
        # Inline If-Match (no shared component for it today).
        for param in params:
            if isinstance(param, dict) and param.get("name") == "If-Match":
                raise AssertionError(
                    f"{path} static contract still advertises If-Match "
                    "but the handler does not enforce it (#2064). Strip "
                    "the parameter or wire real version-token enforcement."
                )

    # PATCH /tasks/{project}/{n} lives on the GET path too — check the
    # patch op specifically.
    patch_op = paths.get("/tasks/{project}/{n}", {}).get("patch", {})
    patch_params = patch_op.get("parameters", []) or []
    patch_refs = [
        p.get("$ref", "") for p in patch_params if isinstance(p, dict)
    ]
    assert not any("IdempotencyKey" in ref for ref in patch_refs), (
        "PATCH /tasks/{project}/{n} static contract still advertises "
        "Idempotency-Key but the handler does not implement it (#2064)."
    )
    for param in patch_params:
        if isinstance(param, dict) and param.get("name") == "If-Match":
            raise AssertionError(
                "PATCH /tasks/{project}/{n} static contract still "
                "advertises If-Match but the handler does not enforce "
                "it (#2064)."
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


def test_static_yaml_declares_503_on_task_write_paths() -> None:
    """Static contract must declare 503 on every task-write path.

    Round 9 of #2064 — mirror of the inbox-write 503 conformance
    test above. The FastAPI handlers in
    ``src/pollypm/web_api/routes/tasks.py`` catch
    ``_BACKING_STORE_ERRORS`` (psycopg.OperationalError /
    psycopg_pool.PoolTimeout) and raise ``service_unavailable``
    (503), but ``docs/api/openapi.yaml`` was missing the 503 entry
    on ``/queue``, ``/claim``, ``/cancel``, ``/reassign``, and
    PATCH ``/tasks/{project}/{n}``. Generated clients would not
    know to handle a real pg outage with the same typed-error
    envelope they see from the live server.

    Pin coverage so future drift trips CI.
    """
    contract = _load_contract()
    task_write_paths_post = [
        "/tasks/{project}/{n}/queue",
        "/tasks/{project}/{n}/claim",
        "/tasks/{project}/{n}/cancel",
        "/tasks/{project}/{n}/reassign",
    ]
    paths = contract.get("paths", {})
    for path in task_write_paths_post:
        ops = paths.get(path, {})
        post = ops.get("post", {})
        responses = post.get("responses", {}) or {}
        assert "503" in responses, (
            f"{path} POST missing 503 response in static contract "
            "(#2064 round-9). The FastAPI handler maps backing-store "
            "errors to a 503 with the service_unavailable envelope; "
            "the YAML must say so too."
        )
    patch_responses = (
        paths.get("/tasks/{project}/{n}", {})
        .get("patch", {})
        .get("responses", {})
        or {}
    )
    assert "503" in patch_responses, (
        "PATCH /tasks/{project}/{n} missing 503 response in static "
        "contract (#2064 round-9). The FastAPI handler maps "
        "backing-store errors to a 503 with the service_unavailable "
        "envelope; the YAML must say so too."
    )


def test_task_write_endpoints_document_503() -> None:
    """Implementation-side conformance for task-write 503 coverage.

    Mirrors :func:`test_static_yaml_declares_503_on_inbox_write_paths`
    on the auto-generated FastAPI OpenAPI document. Every task-write
    route's ``responses=`` map must include 503 so generated clients
    branch on the same typed envelope they see at runtime
    (#2064 round-9 blocker #3).
    """
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
        project=ProjectSettings(
            name="P", root_dir=base, tmux_session="t",
            workspace_root=base, base_dir=base / ".pollypm",
            logs_dir=base / ".pollypm/logs",
            snapshots_dir=base / ".pollypm/snapshots",
            state_db=base / ".pollypm/state.db",
        ),
        pollypm=PollyPMSettings(
            controller_account="codex_primary",
            open_permissions_by_default=False,
            failover_enabled=False,
            failover_accounts=[],
            heartbeat_backend="local",
            scheduler_backend="inline",
            lease_timeout_minutes=30,
        ),
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
    raw = app.openapi()
    paths = raw["paths"]

    post_paths = [
        "/api/v1/tasks/{project}/{n}/queue",
        "/api/v1/tasks/{project}/{n}/claim",
        "/api/v1/tasks/{project}/{n}/cancel",
        "/api/v1/tasks/{project}/{n}/reassign",
    ]
    for path in post_paths:
        responses = paths[path]["post"]["responses"]
        assert "503" in responses, (
            f"Implementation OpenAPI for POST {path} is missing a 503 "
            "response — keep the route's ``responses=`` map in sync "
            "with the static contract (#2064 round-9)."
        )
    patch_responses = paths["/api/v1/tasks/{project}/{n}"]["patch"]["responses"]
    assert "503" in patch_responses, (
        "Implementation OpenAPI for PATCH /api/v1/tasks/{project}/{n} "
        "is missing a 503 response (#2064 round-9)."
    )


def test_inbox_archive_reason_documented_as_post_transition() -> None:
    """Pin the reason-note ordering contract.

    Round 4 of #2060 reordered ``archive_inbox_item`` to write the
    reason note ONLY after ``archive_task(strict=True)`` succeeds, so
    a losing concurrent archiver no longer leaves a stray
    ``archive reason:`` note on a task it never archived. Round 7
    (Codex) caught that ``docs/api/openapi.yaml`` still described the
    old pre-transition order in the ``InboxArchiveRequest.reason``
    schema, advertising a contract the implementation no longer
    honors.

    This test fails if anyone rewrites the schema description back to
    a pre-transition contract.
    """
    contract = _load_contract()
    description = (
        contract["components"]["schemas"]["InboxArchiveRequest"]
        ["properties"]["reason"]["description"]
    )
    lowered = description.lower()
    assert "after" in lowered, (
        "InboxArchiveRequest.reason description must say the note is "
        "recorded AFTER the archive transition (#2060 round-4 / "
        "round-7). Got: " + description
    )
    assert "transition" in lowered, (
        "InboxArchiveRequest.reason description must reference the "
        "archive transition explicitly so the ordering contract is "
        "unambiguous. Got: " + description
    )
    assert "before the state transition" not in lowered, (
        "InboxArchiveRequest.reason description reverted to the old "
        "pre-transition contract. The implementation writes the note "
        "AFTER archive_task(strict=True) succeeds — see "
        "src/pollypm/web_api/service.py archive_inbox_item."
    )


def test_archive_documents_409_session_reference_conflict() -> None:
    """Codex round 4 on #2063: archive's 409 must be pinned in BOTH surfaces.

    ``archive_project`` maps an enabled-session reference to ``409
    conflict`` (mirrors the CLI's ``pm projects remove`` guard) but the
    static contract and the implementation's auto-generated OpenAPI
    initially only documented 401/404/503. Generated clients would
    therefore branch on an unexpected response and crash on the
    user-visible "still referenced by ..." path.

    Pin coverage so future drift trips CI:

    * ``docs/api/openapi.yaml`` — ``POST /projects/{key}/archive``
      must list a ``409`` response.
    * The auto-generated FastAPI OpenAPI doc must also list ``409``
      under ``/api/v1/projects/{key}/archive``.
    """
    # Static contract.
    contract = _load_contract()
    archive_post = contract["paths"]["/projects/{key}/archive"]["post"]
    assert "409" in archive_post["responses"], (
        "docs/api/openapi.yaml POST /projects/{key}/archive is missing a "
        "409 response — enabled-session reference conflicts are user-visible "
        "and must be documented (Codex round 4 on #2063)."
    )

    # Implementation-side doc.
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
    raw = app.openapi()
    impl_archive = raw["paths"]["/api/v1/projects/{key}/archive"]["post"]
    assert "409" in impl_archive["responses"], (
        "Implementation OpenAPI for POST /api/v1/projects/{key}/archive is "
        "missing a 409 response — keep the route's ``responses=`` map in "
        "sync with the static contract (Codex round 4 on #2063)."
    )


def test_reassign_docs_dont_suggest_patch_assignee() -> None:
    """Pin out the stale PATCH-assignee guidance on the reassign endpoint.

    Round 7 (Codex) on #2064 flagged that the reassign endpoint
    description in ``docs/api/openapi.yaml`` told clients to use
    ``PATCH /tasks/{project}/{n}`` with ``assignee`` for low-level
    bookkeeping. ``TaskPatchRequest`` only accepts ``labels``,
    ``status``, and ``metadata`` (and now forbids extras), so a client
    following that prose would get a 422 instead of an assignee
    update. The description was rewritten to make clear that PATCH
    does NOT accept ``assignee`` and that low-level updates must go
    through the work service directly via the CLI.

    This test fails if anyone re-introduces the PATCH-assignee
    suggestion into the reassign description.
    """
    contract = _load_contract()
    reassign_op = (
        contract["paths"]["/tasks/{project}/{n}/reassign"]["post"]
    )
    description = reassign_op["description"]
    lowered = description.lower()
    assert "does not accept the assignee" in lowered, (
        "Reassign description must explicitly tell clients that "
        "PATCH /tasks/{p}/{n} does NOT accept ``assignee`` "
        "(#2064 round-7). Got: " + description
    )
    # The old guidance variant — pin it out so a doc-sync regression
    # surfaces here instead of in a generated-client bug report.
    assert "operator bookkeeping" not in lowered, (
        "Reassign description reverted to the old PATCH-assignee "
        "guidance (``... is for operator bookkeeping only``). PATCH "
        "/tasks/{p}/{n} rejects ``assignee`` with 422 — see "
        "TaskPatchRequest in src/pollypm/web_api/models.py."
    )


def test_task_patch_request_schema_forbids_extras() -> None:
    """Pin ``TaskPatchRequest`` extras=forbid in the static YAML.

    Round 8 (Codex) on #2064: the runtime model has
    ``model_config = {"extra": "forbid"}`` (see
    ``src/pollypm/web_api/models.py:350``), so the FastAPI
    request validator rejects unknown keys with 422. Without
    ``additionalProperties: false`` on the static
    ``TaskPatchRequest`` schema, generated clients reading
    ``docs/api/openapi.yaml`` treat extras as schema-valid and
    only discover the rejection at runtime — exactly the
    drift Codex flagged.

    This test pins the static schema. It also pins the
    runtime-vs-static parity by re-reading the Pydantic
    model's generated schema so a future ``extra="allow"``
    drift on either side trips here.
    """
    from pollypm.web_api.models import TaskPatchRequest

    contract = _load_contract()
    static_schema = contract["components"]["schemas"]["TaskPatchRequest"]
    assert static_schema.get("additionalProperties") is False, (
        "TaskPatchRequest in docs/api/openapi.yaml must declare "
        "``additionalProperties: false`` to mirror the runtime "
        "``extra='forbid'`` config — generated clients would "
        "otherwise treat unknown keys (typos like ``metdata`` or "
        "unsupported fields like ``priority`` / ``assignee``) as "
        "valid even though the server returns 422 (#2064 round-8)."
    )

    # Runtime-vs-static parity: if the Pydantic model ever
    # relaxes back to ``extra='allow'``/``ignore``, the
    # generated schema drops ``additionalProperties: false`` and
    # this assertion fires before the contract drifts again.
    runtime_schema = TaskPatchRequest.model_json_schema()
    assert runtime_schema.get("additionalProperties") is False, (
        "Runtime TaskPatchRequest no longer emits "
        "``additionalProperties: false``. Restore "
        "``model_config = {'extra': 'forbid'}`` on the Pydantic "
        "model so the static YAML and the request validator agree."
    )


def test_task_claim_request_schema_forbids_extras() -> None:
    """Pin ``TaskClaimRequest`` extras=forbid in runtime + static YAML.

    Round 12 (Codex) on #2064: the round-6 fix locked down
    ``TaskPatchRequest`` but the sibling request models
    (``TaskClaimRequest`` / ``TaskCancelRequest`` /
    ``TaskReassignRequest``) still defaulted to Pydantic
    ``extra='ignore'``. A client following spec §5.3 and
    POSTing ``{"actor": "alice", "assignee": "bob"}`` to
    ``/claim`` would get a silent 200 with the ``assignee``
    field dropped on the floor — the work-service derives
    assignee from the flow + roles. Forbidding extras turns
    that into a 422 instead.

    Mirrors ``test_task_patch_request_schema_forbids_extras``
    above; runtime + static parity so neither side can drift
    back without tripping CI.
    """
    from pollypm.web_api.models import TaskClaimRequest

    contract = _load_contract()
    static_schema = contract["components"]["schemas"]["TaskClaimRequest"]
    assert static_schema.get("additionalProperties") is False, (
        "TaskClaimRequest in docs/api/openapi.yaml must declare "
        "``additionalProperties: false`` to mirror the runtime "
        "``extra='forbid'`` config — generated clients would "
        "otherwise treat unknown keys (e.g. ``assignee`` from spec "
        "§5.3, which the work-service derives instead) as valid "
        "even though the server returns 422 (#2064 round-12)."
    )

    runtime_schema = TaskClaimRequest.model_json_schema()
    assert runtime_schema.get("additionalProperties") is False, (
        "Runtime TaskClaimRequest no longer emits "
        "``additionalProperties: false``. Restore "
        "``model_config = {'extra': 'forbid'}`` on the Pydantic "
        "model so the static YAML and the request validator agree."
    )


def test_claim_contract_exposes_session_identity() -> None:
    """Claim actor is documented as ``claimed_by_session`` identity."""
    from pollypm.web_api.models import TaskClaimRequest, TaskSummary

    contract = _load_contract()
    static_summary = contract["components"]["schemas"]["TaskSummary"]
    assert "claimed_by_session" in static_summary["properties"]

    runtime_summary = TaskSummary.model_json_schema()
    assert "claimed_by_session" in runtime_summary["properties"]

    static_actor = contract["components"]["schemas"]["TaskClaimRequest"][
        "properties"
    ]["actor"]
    runtime_actor = TaskClaimRequest.model_json_schema()["properties"]["actor"]
    assert "claimed_by_session" in static_actor["description"]
    assert "claimed_by_session" in runtime_actor["description"]


def test_task_cancel_request_schema_forbids_extras() -> None:
    """Pin ``TaskCancelRequest`` extras=forbid in runtime + static YAML.

    Round 12 (Codex) on #2064. See
    ``test_task_claim_request_schema_forbids_extras`` for the
    framing.
    """
    from pollypm.web_api.models import TaskCancelRequest

    contract = _load_contract()
    static_schema = contract["components"]["schemas"]["TaskCancelRequest"]
    assert static_schema.get("additionalProperties") is False, (
        "TaskCancelRequest in docs/api/openapi.yaml must declare "
        "``additionalProperties: false`` to mirror the runtime "
        "``extra='forbid'`` config — generated clients would "
        "otherwise treat unknown keys as valid even though the "
        "server returns 422 (#2064 round-12)."
    )

    runtime_schema = TaskCancelRequest.model_json_schema()
    assert runtime_schema.get("additionalProperties") is False, (
        "Runtime TaskCancelRequest no longer emits "
        "``additionalProperties: false``. Restore "
        "``model_config = {'extra': 'forbid'}`` on the Pydantic "
        "model so the static YAML and the request validator agree."
    )


def test_task_reassign_request_schema_forbids_extras() -> None:
    """Pin ``TaskReassignRequest`` extras=forbid in runtime + static YAML.

    Round 12 (Codex) on #2064. See
    ``test_task_claim_request_schema_forbids_extras`` for the
    framing.
    """
    from pollypm.web_api.models import TaskReassignRequest

    contract = _load_contract()
    static_schema = contract["components"]["schemas"]["TaskReassignRequest"]
    assert static_schema.get("additionalProperties") is False, (
        "TaskReassignRequest in docs/api/openapi.yaml must declare "
        "``additionalProperties: false`` to mirror the runtime "
        "``extra='forbid'`` config — generated clients would "
        "otherwise treat unknown keys as valid even though the "
        "server returns 422 (#2064 round-12)."
    )

    runtime_schema = TaskReassignRequest.model_json_schema()
    assert runtime_schema.get("additionalProperties") is False, (
        "Runtime TaskReassignRequest no longer emits "
        "``additionalProperties: false``. Restore "
        "``model_config = {'extra': 'forbid'}`` on the Pydantic "
        "model so the static YAML and the request validator agree."
    )
def test_audit_responses_document_corrupt_archives_skipped() -> None:
    """Pin the round-7 diagnostic field on both audit response schemas.

    Round 7 of #2062 added ``_corrupt_archives_skipped`` to the runtime
    ``AuditGrepResponse`` / ``AuditStatsResponse`` Pydantic models so
    the walker can skip truncated/corrupt ``.gz`` archives best-effort
    instead of raising a 500. Round 8 (Codex) caught that
    ``docs/api/openapi.yaml`` did not document the new field, so
    generated clients had no way to read the diagnostic.

    This test fails if either schema drops the field again.
    """
    contract = _load_contract()
    for schema_name in ("AuditGrepResponse", "AuditStatsResponse"):
        schema = contract["components"]["schemas"][schema_name]
        properties = schema.get("properties", {})
        assert "_corrupt_archives_skipped" in properties, (
            f"{schema_name} must document `_corrupt_archives_skipped` "
            "(round-7 best-effort gz skip, PR #2062)."
        )
        field = properties["_corrupt_archives_skipped"]
        assert field.get("type") == "integer", (
            f"{schema_name}._corrupt_archives_skipped must be an integer."
        )
        assert field.get("minimum", 0) >= 0, (
            f"{schema_name}._corrupt_archives_skipped must be non-negative."
        )


def test_openapi_documents_three_auth_modes() -> None:
    """Pin the round-8 (#2065) auth-contract fix.

    The static OpenAPI contract previously advertised the API as
    ``loopback by default`` with only ``bearerAuth`` declared at the
    document level. The shipped runtime accepts three credential
    modes: bearer header, ``pollypm-session`` cookie, and a
    credential-free tailnet-peer mode (only when the server was
    constructed with ``tailnet_trust_enabled=True``).

    This test fails if anyone reverts the contract back to a
    bearer-only / loopback-default story.
    """
    contract = _load_contract()

    # 1. cookieAuth security scheme exists and is an apiKey-in-cookie
    #    named pollypm-session (matching the runtime cookie name).
    schemes = (
        contract.get("components", {}).get("securitySchemes", {})
    )
    assert "bearerAuth" in schemes, "bearerAuth scheme missing"
    cookie_scheme = schemes.get("cookieAuth")
    assert cookie_scheme is not None, (
        "cookieAuth security scheme missing — round-8 #2065 fix."
    )
    assert cookie_scheme.get("type") == "apiKey", (
        f"cookieAuth must be type apiKey, got {cookie_scheme.get('type')!r}"
    )
    assert cookie_scheme.get("in") == "cookie", (
        f"cookieAuth must be in=cookie, got {cookie_scheme.get('in')!r}"
    )
    assert cookie_scheme.get("name") == "pollypm-session", (
        "cookieAuth name must match the runtime cookie "
        "(pollypm-session); got "
        f"{cookie_scheme.get('name')!r}"
    )

    # 2. Global security list allows ANY of: bearer, cookie, or
    #    credential-free (empty entry — OpenAPI's way of marking
    #    anonymous access for the tailnet-trust mode).
    security = contract.get("security", [])
    assert isinstance(security, list), "top-level security must be a list"
    requires_bearer = any(
        isinstance(entry, dict) and "bearerAuth" in entry
        for entry in security
    )
    requires_cookie = any(
        isinstance(entry, dict) and "cookieAuth" in entry
        for entry in security
    )
    allows_anonymous = any(
        isinstance(entry, dict) and len(entry) == 0
        for entry in security
    )
    assert requires_bearer, "security list must offer bearerAuth"
    assert requires_cookie, (
        "security list must offer cookieAuth (round-8 #2065 fix)."
    )
    assert allows_anonymous, (
        "security list must include an empty {} entry to model the "
        "credential-free tailnet-peer mode (round-8 #2065 fix)."
    )

    # 3. Top-level description must enumerate all three modes so
    #    generated-client readers see the full auth story.
    description = (contract.get("info", {}).get("description") or "").lower()
    assert "bearer" in description, (
        "info.description must mention the bearer mode."
    )
    assert "cookie" in description or "pollypm-session" in description, (
        "info.description must mention the session-cookie mode."
    )
    assert "tailnet" in description or "tailscale" in description, (
        "info.description must mention the tailnet-trust mode."
    )

    # 4. The old "loopback by default" / bearer-only framing must NOT
    #    survive — that was the round-7 → round-8 regression Codex
    #    flagged.
    servers = contract.get("servers", [])
    for server in servers:
        server_desc = (server.get("description") or "").lower()
        assert "loopback by default" not in server_desc, (
            "server description still says 'loopback by default' — "
            "the runtime auto-binds Tailscale when available "
            "(round-8 #2065 fix)."
        )


def test_runtime_openapi_documents_three_auth_modes() -> None:
    """Pin the round-11 (#2065) runtime-side auth-contract fix.

    Round 8 fixed the static ``docs/api/openapi.yaml`` to enumerate
    three credential modes (bearer / cookie / credential-free tailnet
    peer). Round 11 (Codex 03:48 UTC) caught that the runtime
    ``/openapi.json`` still only advertised ``bearerAuth`` because
    ``_attach_security_scheme()`` in ``src/pollypm/web_api/app.py``
    overwrote ``components.securitySchemes`` with a bearer-only
    block and ``security`` with ``[{"bearerAuth": []}]``.

    Generated clients that hit the live ``/api/v1/openapi.json`` for
    discovery would therefore not know the cookie or tailnet-trust
    modes existed — exactly the drift the round-8 fix was supposed
    to close.

    Mirrors :func:`test_openapi_documents_three_auth_modes` on the
    auto-generated FastAPI doc.
    """
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
        project=ProjectSettings(
            name="P", root_dir=base, tmux_session="t",
            workspace_root=base, base_dir=base / ".pollypm",
            logs_dir=base / ".pollypm/logs",
            snapshots_dir=base / ".pollypm/snapshots",
            state_db=base / ".pollypm/state.db",
        ),
        pollypm=PollyPMSettings(
            controller_account="codex_primary",
            open_permissions_by_default=False,
            failover_enabled=False,
            failover_accounts=[],
            heartbeat_backend="local",
            scheduler_backend="inline",
            lease_timeout_minutes=30,
        ),
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
    schema = app.openapi()

    # 1. Both bearerAuth AND cookieAuth must be declared on the
    #    runtime doc.
    schemes = schema.get("components", {}).get("securitySchemes", {})
    assert "bearerAuth" in schemes, (
        "Runtime /openapi.json missing bearerAuth scheme "
        "(#2065 round-11)."
    )
    cookie_scheme = schemes.get("cookieAuth")
    assert cookie_scheme is not None, (
        "Runtime /openapi.json missing cookieAuth scheme — "
        "_attach_security_scheme() must mirror the static YAML "
        "(#2065 round-11)."
    )
    assert cookie_scheme.get("type") == "apiKey", (
        "cookieAuth must be type apiKey, got "
        f"{cookie_scheme.get('type')!r}"
    )
    assert cookie_scheme.get("in") == "cookie", (
        f"cookieAuth must be in=cookie, got {cookie_scheme.get('in')!r}"
    )
    assert cookie_scheme.get("name") == "pollypm-session", (
        "cookieAuth name must match the runtime cookie "
        "(pollypm-session); got "
        f"{cookie_scheme.get('name')!r}"
    )

    # 2. Top-level security list must enumerate all three modes:
    #    bearer, cookie, AND a credential-free entry for the
    #    tailnet-trust path.
    security = schema.get("security", [])
    assert isinstance(security, list), (
        "Runtime security must be a list"
    )
    requires_bearer = any(
        isinstance(entry, dict) and "bearerAuth" in entry
        for entry in security
    )
    requires_cookie = any(
        isinstance(entry, dict) and "cookieAuth" in entry
        for entry in security
    )
    allows_anonymous = any(
        isinstance(entry, dict) and len(entry) == 0
        for entry in security
    )
    assert requires_bearer, (
        "Runtime security list must offer bearerAuth "
        "(#2065 round-11)."
    )
    assert requires_cookie, (
        "Runtime security list must offer cookieAuth — "
        "_attach_security_scheme() previously set bearer-only "
        "(#2065 round-11)."
    )
    assert allows_anonymous, (
        "Runtime security list must include an empty {} entry to "
        "model the credential-free tailnet-peer mode "
        "(#2065 round-11)."
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


def test_briefings_runtime_openapi_matches_static_error_codes() -> None:
    """Pin the briefings route ``responses=`` map against the static YAML.

    Round 8 (Codex) on #2059: the FastAPI route decorators in
    ``src/pollypm/web_api/routes/briefings.py`` had no ``responses=``
    map, so the auto-generated ``/openapi.json`` only advertised the
    success body + FastAPI's default ``422`` validation envelope.
    ``docs/api/openapi.yaml`` documented the full error surface
    (401/404/503 on GET, 400/401/404/409/503/504 on regenerate), but
    generated clients consuming the live spec would not branch on the
    typed envelopes the handlers actually raise.

    Pin both surfaces so future drift trips CI.
    """
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
        project=ProjectSettings(
            name="P", root_dir=base, tmux_session="t",
            workspace_root=base, base_dir=base / ".pollypm",
            logs_dir=base / ".pollypm/logs",
            snapshots_dir=base / ".pollypm/snapshots",
            state_db=base / ".pollypm/state.db",
        ),
        pollypm=PollyPMSettings(
            controller_account="codex_primary",
            open_permissions_by_default=False,
            failover_enabled=False,
            failover_accounts=[],
            heartbeat_backend="local",
            scheduler_backend="inline",
            lease_timeout_minutes=30,
        ),
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
    raw = app.openapi()
    paths = raw["paths"]

    # GET /briefings — 401 only (no typed 4xx / 5xx beyond auth).
    list_get = paths["/api/v1/briefings"]["get"]["responses"]
    assert "401" in list_get, (
        "Implementation OpenAPI for GET /api/v1/briefings is missing "
        "the 401 response advertised by docs/api/openapi.yaml — keep "
        "the route's ``responses=`` map in sync (#2059 round-8)."
    )

    # GET /briefings/{type_name} — 401 / 404 / 503.
    render_get = paths["/api/v1/briefings/{type_name}"]["get"]["responses"]
    for code in ("401", "404", "503"):
        assert code in render_get, (
            f"Implementation OpenAPI for GET /api/v1/briefings/"
            f"{{type_name}} is missing the {code} response advertised "
            "by docs/api/openapi.yaml — keep the route's ``responses=`` "
            "map in sync (#2059 round-8)."
        )

    # POST /briefings/{type_name}/regenerate — 400 / 401 / 404 / 409 /
    # 503 / 504. This is the full typed surface the handler raises
    # (see ``regenerate_briefing_endpoint`` and the morning adapter).
    regen_post = (
        paths["/api/v1/briefings/{type_name}/regenerate"]["post"]["responses"]
    )
    for code in ("400", "401", "404", "409", "503", "504"):
        assert code in regen_post, (
            f"Implementation OpenAPI for POST /api/v1/briefings/"
            f"{{type_name}}/regenerate is missing the {code} response "
            "advertised by docs/api/openapi.yaml — keep the route's "
            "``responses=`` map in sync (#2059 round-8)."
        )
