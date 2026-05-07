"""Role-assignment validation for ``pm task create`` (savethenovel forensic).

The ``--role <role>=<agent>`` flag historically accepted any string as
``<agent>``. The savethenovel forensic exposed how badly this fails when
an LLM (or human) types a non-agent value: Polly typed
``--role worker=user --role reviewer=user`` while planning a project,
and the work service happily stored ``roles={"worker":"user","reviewer":"user"}``.
The resulting worker session then ran with ``Assignee: user`` for ~37
seconds before Polly self-cancelled.

The fix has two parts:

1. **Reject non-agent values for autonomous-agent roles.** A role is
   "autonomous-agent" when the flow declares a node with
   ``actor_type=role`` referencing it — i.e., the role drives a node
   that an autonomous agent must claim and execute. ``user`` (and
   friends like ``human`` / ``sam`` / ``nobody``) cannot autonomously
   claim work, so they are never legal values for those roles.

2. **Allow ``user`` for metadata-only roles.** ``requester=user`` and
   ``operator=user`` (when the flow doesn't actually drive an
   ``actor_type=role`` node off ``operator``) remain legal — that's
   the existing inbox-view convention for marking a task as
   user-facing (see ``pollypm.work.inbox_view._roles_match_user``).

Legal autonomous-agent identities are intentionally not enumerated as a
closed set. The codebase already uses a wide range of conventions:
agent-profile names (``polly``, ``russell``, ``worker``, ``triage``,
``heartbeat``), role-contract canonical keys (``architect``,
``reviewer``, ``operator_pm``), persona aliases (``archie``), opaque
worker IDs in test fixtures (``agent-1``, ``agent-2``), and per-project
worker handles (``worker-pollypm`` …). Whitelisting any one of these
families would break every other family. Instead we **blacklist** the
specific values that are demonstrably non-agent identities.
"""

from __future__ import annotations

from typing import Iterable

from pollypm.work.models import ActorType, FlowTemplate
from pollypm.work.service_support import ValidationError


# Values that are not autonomous-agent identities. The first two
# (``user`` / ``human``) are how the work service refers to "the
# human" everywhere else (see ``inbox_view._roles_match_user`` and
# ``approve_human_review``'s ``_HUMAN_ACTOR_NAMES``); they can never
# claim a work or review node. The placeholder values catch the
# common LLM/typo failure shape ("oh, it wants a value, I'll fill
# something in") that produced the savethenovel incident.
#
# Personal names like "sam" are intentionally NOT in this set:
# existing test fixtures use them as opaque worker IDs, and no
# project should bake the operator's name into validation. The
# canonical human-marker is "user".
NON_AGENT_VALUES: frozenset[str] = frozenset(
    {
        "user",
        "human",
        "nobody",
        "none",
        "tbd",
        "todo",
        "fixme",
        "?",
    }
)


def autonomous_agent_roles(template: FlowTemplate) -> set[str]:
    """Return roles that drive a *required* autonomous-agent execution node.

    A role is autonomous-agent when at least one node in ``template``
    has ``actor_type=ActorType.ROLE`` and ``actor_role`` equal to the
    role name. We further restrict to roles the template marks as
    *required* (non-optional) — optional roles are by definition
    metadata or fallback hints (e.g. ``chat``'s ``operator`` is
    optional and "defaults to Polly when omitted"), and historic
    fixtures use them as feedback markers like ``operator=user`` even
    though the node never actually fires. Tightening only the
    required-role lane catches the savethenovel shape (``worker=user``
    on the standard/bug/spike/user-review flows, all of which mark
    ``worker`` and ``reviewer`` as required) without breaking those
    fixtures.
    """
    roles: set[str] = set()
    for node in template.nodes.values():
        if node.actor_type != ActorType.ROLE:
            continue
        if not node.actor_role:
            continue
        role_def = template.roles.get(node.actor_role)
        is_optional = isinstance(role_def, dict) and role_def.get(
            "optional", False
        )
        if is_optional:
            continue
        roles.add(node.actor_role)
    return roles


def _legal_examples() -> str:
    """Render a short, stable hint of legal agent values."""
    # Pulled from ROLE_REGISTRY canonical keys + agent-profile names.
    # Kept inline rather than imported so this module can validate
    # before the heavier supervisor / plugin host imports run.
    return "architect, reviewer, worker, polly, russell, triage"


def validate_role_assignments(
    template: FlowTemplate, roles: dict[str, str]
) -> None:
    """Reject obviously-wrong agent values on autonomous-agent roles.

    Raises :class:`ValidationError` (a :class:`WorkServiceError` so the
    CLI renderer in :mod:`pollypm.work.cli` can surface it without a
    traceback). The error message lists the offending role/value, names
    legal-value examples, and points at ``pm project plan`` for the
    common case where the caller meant to draft a project plan rather
    than to assign autonomous workers.

    Idempotent and safe to call before any DB mutation: it does not
    consult the connection or the audit log.
    """
    auto_roles = autonomous_agent_roles(template)
    if not auto_roles:
        return
    bad: list[tuple[str, str]] = []
    for role_name, value in roles.items():
        if role_name not in auto_roles:
            continue  # metadata-only role (e.g. ``requester=user``).
        if value is None:
            continue
        normalized = str(value).strip().lower()
        if normalized in NON_AGENT_VALUES:
            bad.append((role_name, value))
    if not bad:
        return

    # Build a single ValidationError that describes every offence.
    # The CLI renderer matches on the message prefix to render with
    # the standard ✗/Why/Fix layout.
    pairs = ", ".join(f"{r}={v!r}" for r, v in bad)
    legal = _legal_examples()
    raise ValidationError(
        f"Invalid agent value(s) for role(s): {pairs}. "
        f"'{bad[0][1]}' is not an autonomous agent — workers and reviewers "
        f"must be agent identities (examples: {legal}). "
        f"Hint: for project planning, use `pm project plan <project>` "
        f"instead of creating a worker task."
    )


def is_non_agent_value(value: str | None) -> bool:
    """Convenience check used by tests and the CLI gate."""
    if value is None:
        return False
    return str(value).strip().lower() in NON_AGENT_VALUES


__all__: Iterable[str] = (
    "NON_AGENT_VALUES",
    "autonomous_agent_roles",
    "validate_role_assignments",
    "is_non_agent_value",
)
