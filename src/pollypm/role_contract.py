"""Canonical role/persona invariant for kickoff and live sessions (#885).

One contract — one dataclass, one registry, one validator —
covering every role-identity check that was previously scattered
across :mod:`pollypm.role_routing`, :mod:`pollypm.role_banner`,
:mod:`pollypm.heartbeats.local`,
:mod:`pollypm.architect_lifecycle`,
:mod:`pollypm.project_guides`, and
:mod:`pollypm.work.sqlite_service`.

The pre-launch audit (``docs/launch-issue-audit-2026-04-27.md``
§4) cites the recurring shape:

* `#757` — Polly session identified as Russell after kickoff-time
  persona defense had already passed.
* `#758` — wrong-role kickoff clobbered the operator session.
* `#762` / `#755` — update / guide paths can be wrong or rejected
  as prompt injection.
* `#869`/`#868` — persona / role launch surfaces remain brittle.

The contract is the structural fix. Every consumer (kickoff
launcher, role banner, heartbeat drift detector, remediation
message builder, work-service actor validator) reads from one
:class:`RoleContract` per role, so adding / renaming / changing
a role is one edit instead of seven.

Architecture:

* :class:`RoleContract` — frozen dataclass with the per-role
  invariant: canonical key, persona display name, guide path,
  required identity markers, and remediation policy.
* :data:`ROLE_REGISTRY` — module-level mapping from canonical
  role key to :class:`RoleContract`.
* :func:`canonical_role` — normalise an input string to the
  canonical key, raising on unknown roles.
* :func:`persona_for` / :func:`guide_path_for` /
  :func:`identity_markers_for` — typed accessors.
* :func:`validate_identity` — given a role and a pane snapshot,
  return ``None`` (identity matches) or the persona name the
  session drifted into. Pure; tested in isolation.
* :func:`build_remediation_message` — the canonical wording the
  heartbeat sends on drift. Centralised so #755 / #757 fixes do
  not have to be re-invented in each emitter.

Migration: existing readers continue to work — the legacy module-
local ``_ROLE_KEYS`` / ``_ROLE_PERSONA_NAMES`` / ``_ROLE_GUIDE_PATHS``
dicts are valid for the moment. New code must consult
:data:`ROLE_REGISTRY`. The launch-hardening release gate (#889)
will assert that every legacy table agrees with the registry
before tagging v1.
"""

from __future__ import annotations

import importlib.resources
import re
from dataclasses import dataclass, field
from typing import Mapping


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RoleContract:
    """Per-role invariant declaration.

    The frozen + slots dataclass is intentional — once
    instantiated and registered, a contract is the canonical
    answer for that role. Mutation would let one consumer change
    the contract for everyone else, which is exactly the bug
    shape this module exists to prevent.

    Fields:

    * ``key`` — canonical role identifier (snake_case). The same
      string the work service stores in ``actor`` columns and
      the supervisor uses to plan launches.
    * ``persona_name`` — the human-facing name the session
      should identify as (e.g., ``"Polly"`` for ``operator_pm``).
    * ``guide_path`` — absolute on-disk path to the role's
      canonical operating guide. Resolved through
      :func:`importlib.resources.files` so the value is valid
      regardless of whether PollyPM is running from a source
      checkout or an editable / wheel install (#913).
    * ``identity_markers`` — substrings that, when present in a
      pane snapshot, prove the session is operating as this
      role. Heartbeat drift detection scans for them.
    * ``conflicting_personas`` — explicit list of *other* persona
      names that, if they appear, mean this session has drifted.
      Disjoint from ``identity_markers`` in every test.
    * ``can_write_session_state`` — whether this role is allowed
      to mutate persisted session state through the heartbeat
      API. Mirrors the existing ``_MUTATING_SESSION_ROLES``
      frozenset in :mod:`pollypm.heartbeats.local`.
    * ``has_project_guide`` — whether the role gets per-project
      override guides under ``<project>/.pollypm/project-guides/``.
      The operator does not (its guide is global).
    """

    key: str
    persona_name: str
    guide_path: str | None
    identity_markers: tuple[str, ...] = field(default_factory=tuple)
    conflicting_personas: tuple[str, ...] = field(default_factory=tuple)
    can_write_session_state: bool = True
    has_project_guide: bool = True

    def __post_init__(self) -> None:
        # Identity markers and conflicting personas must be
        # disjoint — otherwise a marker check would also report
        # drift, which is incoherent.
        markers = {m.lower() for m in self.identity_markers}
        conflicts = {c.lower() for c in self.conflicting_personas}
        overlap = markers & conflicts
        if overlap:
            raise ValueError(
                f"role {self.key!r}: identity_markers and "
                f"conflicting_personas overlap on {sorted(overlap)!r}"
            )


# ---------------------------------------------------------------------------
# Canonical registry
# ---------------------------------------------------------------------------


def _packaged_guide_path(filename: str) -> str:
    """Return the absolute on-disk path of a packaged role-guide.

    #913 — the guide-path constants previously hard-coded the
    repo-relative ``src/pollypm/plugins_builtin/...`` form. That
    string is the source location, not the install location: an
    editable install at ``~/.local/share/uv/tools/pollypm/lib/...``
    resolves the same module under a different prefix, and a
    drifted session running with ``cwd != <repo>`` cannot reach
    the file by following ``src/...``. Resolving through
    :func:`importlib.resources.files` returns the path actually
    valid on the running interpreter, regardless of layout.

    The returned string is an absolute path. ``importlib.resources``
    handles both source layouts (``src/pollypm/...``) and installed
    distributions transparently — every consumer (heartbeat
    remediation, contract audit, security checklist) gets a path
    that resolves from any cwd.
    """
    base = importlib.resources.files("pollypm.plugins_builtin.core_agent_profiles")
    return str(base / "profiles" / filename)


_OPERATOR_GUIDE = _packaged_guide_path("polly-operator-guide.md")
_REVIEWER_GUIDE = _packaged_guide_path("russell.md")
# The architect role's persona is built inline in
# ``pollypm.plugins_builtin.core_agent_profiles.profiles`` rather than
# from a standalone Markdown file. The legacy heartbeat persona-drift
# table named ``architect.md`` but that file does not exist on disk —
# the contract audit (#888) caught the drift. Setting guide_path=None
# matches reality; the remediation message just omits the guide line.
_ARCHITECT_GUIDE: str | None = None
# Worker has no canonical operating guide because its identity is
# defined per-task by the launcher prompt, not a free-floating
# profile. Heartbeat drift on a worker pane falls back to a
# generic re-assertion.


_OPERATOR_PM_CONTRACT = RoleContract(
    key="operator_pm",
    persona_name="Polly",
    guide_path=_OPERATOR_GUIDE,
    identity_markers=("polly", "operator-pm", "operator_pm"),
    conflicting_personas=("russell", "archie", "heartbeat"),
    can_write_session_state=True,
    has_project_guide=False,
)


_ARCHITECT_CONTRACT = RoleContract(
    key="architect",
    persona_name="Archie",
    guide_path=_ARCHITECT_GUIDE,
    identity_markers=("archie", "architect"),
    conflicting_personas=("polly", "russell", "heartbeat"),
    can_write_session_state=False,
    has_project_guide=True,
)


_REVIEWER_CONTRACT = RoleContract(
    key="reviewer",
    persona_name="Russell",
    guide_path=_REVIEWER_GUIDE,
    identity_markers=("russell", "reviewer"),
    conflicting_personas=("polly", "archie", "heartbeat"),
    can_write_session_state=False,
    has_project_guide=True,
)


_WORKER_CONTRACT = RoleContract(
    key="worker",
    persona_name="Worker",
    guide_path=None,
    identity_markers=("worker",),
    conflicting_personas=("polly", "russell", "archie"),
    can_write_session_state=True,
    has_project_guide=True,
)


_HEARTBEAT_CONTRACT = RoleContract(
    key="heartbeat_supervisor",
    persona_name="Heartbeat",
    guide_path=None,
    identity_markers=("heartbeat",),
    conflicting_personas=("polly", "russell", "archie"),
    can_write_session_state=True,
    has_project_guide=False,
)


ROLE_REGISTRY: Mapping[str, RoleContract] = {
    "operator_pm": _OPERATOR_PM_CONTRACT,
    "architect": _ARCHITECT_CONTRACT,
    "reviewer": _REVIEWER_CONTRACT,
    "worker": _WORKER_CONTRACT,
    "heartbeat_supervisor": _HEARTBEAT_CONTRACT,
}
"""Canonical mapping from role key to its :class:`RoleContract`."""


# Legacy / display-form alternate names that callers may pass in.
# Mapping these to the canonical key keeps every consumer happy
# without forcing a one-shot rename across the whole codebase.
_ALIAS_TO_CANONICAL: Mapping[str, str] = {
    # The display "operator-pm" form appears in tmux session names
    # and in some legacy persona maps; canonical is "operator_pm".
    "operator-pm": "operator_pm",
    "operator": "operator_pm",
    "polly": "operator_pm",
    # Reviewer / architect aliases.
    "russell": "reviewer",
    "archie": "architect",
    # Worker aliases — empty for now; per-project worker prompts
    # already use "worker".
    # Heartbeat aliases.
    "heartbeat-supervisor": "heartbeat_supervisor",
    "heartbeat": "heartbeat_supervisor",
}


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def canonical_role(role: str | None) -> str:
    """Normalize ``role`` to a canonical key.

    Accepts the canonical form (``"operator_pm"``), the
    display/tmux form (``"operator-pm"``), or a persona name
    alias (``"Polly"``, ``"Archie"``). Raises :class:`ValueError`
    on an unknown role so the caller cannot silently fall through
    on a typo.
    """
    if role is None:
        raise ValueError("role is required, got None")
    raw = str(role).strip()
    if not raw:
        raise ValueError("role is required, got empty string")
    lower = raw.lower()
    canonical = _ALIAS_TO_CANONICAL.get(lower) or _ALIAS_TO_CANONICAL.get(
        raw.replace("-", "_").lower()
    )
    if canonical is not None:
        return canonical
    normalized = raw.replace("-", "_").lower()
    if normalized in ROLE_REGISTRY:
        return normalized
    valid = sorted(ROLE_REGISTRY.keys())
    raise ValueError(
        f"Unknown role {role!r}. Expected one of: {', '.join(valid)}"
    )


def get_contract(role: str) -> RoleContract:
    """Return the :class:`RoleContract` for ``role``.

    Aliases are accepted (see :func:`canonical_role`). Raises
    :class:`ValueError` on an unknown role.
    """
    return ROLE_REGISTRY[canonical_role(role)]


def persona_for(role: str) -> str:
    """Return the persona display name for ``role``."""
    return get_contract(role).persona_name


def guide_path_for(role: str) -> str | None:
    """Return the canonical guide path for ``role`` or ``None``."""
    return get_contract(role).guide_path


def identity_markers_for(role: str) -> tuple[str, ...]:
    """Return identity markers tested by drift detection."""
    return get_contract(role).identity_markers


def can_write_session_state(role: str) -> bool:
    """Whether ``role`` is allowed to mutate persisted session state.

    Mirrors :mod:`pollypm.heartbeats.local`'s
    ``_MUTATING_SESSION_ROLES`` set so the heartbeat write API
    and the persona contract agree.
    """
    return get_contract(role).can_write_session_state


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


_IDENTITY_CLAIM_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bI(?:'m| am)\s+([A-Z][a-zA-Z\-]+)\b"),
    re.compile(r"\bThis (?:is|session is)\s+([A-Z][a-zA-Z\-]+)\b"),
    re.compile(r"\bidentif(?:y|ied) as\s+([A-Z][a-zA-Z\-]+)\b"),
)
"""Phrasings that indicate a session is *claiming* an identity.

Used by :func:`validate_identity` to extract the asserted persona
from a pane snapshot. The patterns are intentionally narrow:
``I'm Polly`` matches; ``users wonder what Russell would say``
does not.
"""


def validate_identity(role: str, pane_text: str) -> str | None:
    """Return the drifted-to persona name, or ``None`` if identity holds.

    The heartbeat calls this once per snapshot. ``role`` is the
    canonical role for the pane; ``pane_text`` is the captured
    last N lines of the tmux pane. The function:

    1. Extracts every identity claim using
       :data:`_IDENTITY_CLAIM_PATTERNS`.
    2. For each claim, asks: is it one of *this role's* identity
       markers? Yes → ignore. No → is it one of this role's
       conflicting personas, or another known persona? Yes →
       return that name as the drift target.
    3. Returns ``None`` if no claim survived as drift.

    The function is pure and total — heartbeat drift detection
    is on the hot path, so swallowing errors is by design.
    """
    if not pane_text:
        return None
    try:
        contract = get_contract(role)
    except ValueError:
        return None

    markers_lc = {m.lower() for m in contract.identity_markers}
    conflicts_lc = {c.lower() for c in contract.conflicting_personas}

    for pattern in _IDENTITY_CLAIM_PATTERNS:
        for match in pattern.finditer(pane_text):
            claimed = match.group(1).strip()
            if not claimed:
                continue
            claimed_lc = claimed.lower()
            if claimed_lc in markers_lc:
                continue
            if claimed_lc in conflicts_lc:
                return claimed
            # Cross-check the global registry — a claim that names
            # a known persona for *another* role is drift even if
            # this role's conflicts list doesn't list it explicitly.
            for other in ROLE_REGISTRY.values():
                if other.key == contract.key:
                    continue
                if claimed_lc == other.persona_name.lower():
                    return claimed
    return None


# ---------------------------------------------------------------------------
# Remediation
# ---------------------------------------------------------------------------


def build_remediation_message(role: str, drifted_to: str) -> str:
    """Build the canonical heartbeat persona-drift remediation.

    The audit (#755 / #757) cites the canonical wording
    requirement: avoid the ``<system-update>`` tag (which prompt-
    injection defenses learned to reject) and re-anchor the
    session by naming the operating guide path explicitly. This
    helper centralises that wording so future fixes don't have
    to be re-invented in each emitter.

    Returns plain text suitable to send through the heartbeat's
    ``persona-drift-remediation`` owner channel.
    """
    contract = get_contract(role)
    persona = contract.persona_name
    guide = contract.guide_path
    lines = [
        "PollyPM persona-drift correction (heartbeat-issued).",
        "",
        (
            f"This session is configured as role={contract.key!r}; "
            f"canonical persona is {persona}. The pane just "
            f"identified itself as {drifted_to!r}, which doesn't match."
        ),
    ]
    if guide:
        # #913 — emit a *layout-stable* reference rather than the
        # raw on-disk path. The contract's ``guide_path`` is now an
        # absolute path resolved through ``importlib.resources``,
        # which means it embeds ``src/pollypm/...`` on a source
        # checkout but ``site-packages/pollypm/...`` on a wheel
        # install — neither prefix is a string the recipient agent
        # can paste back as a working path. Emitting the canonical
        # filename plus its package qualifier gives the agent a
        # form it can hand back to ``pm`` / Python without
        # depending on the operator's install layout.
        guide_filename = guide.rsplit("/", 1)[-1]
        lines += [
            "",
            (
                f"Operating guide: {guide_filename} "
                f"(packaged under "
                f"pollypm.plugins_builtin.core_agent_profiles.profiles)"
            ),
        ]
    lines += [
        "",
        (
            f"Re-anchor: stop, re-read your operating guide, then "
            f"continue under your canonical persona. Acknowledge with "
            f'"OK {persona}" before your next action so the operator '
            f"can confirm the drift cleared."
        ),
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Cross-table drift detection (release-gate adjacent)
# ---------------------------------------------------------------------------


def legacy_persona_table_disagreements(
    legacy_persona_names: Mapping[str, str],
) -> tuple[str, ...]:
    """Return rows where legacy persona table disagrees with registry.

    Some existing modules (notably :mod:`pollypm.heartbeats.local`)
    keep their own ``role -> persona`` dict because the registry
    didn't exist before #885. This helper compares such a dict
    to the canonical registry and returns any disagreement so a
    test (and the release gate, #889) can fail loudly when the
    two diverge.

    Each disagreement is a single human-readable line.
    """
    out: list[str] = []
    for legacy_role, legacy_persona in legacy_persona_names.items():
        try:
            canonical = canonical_role(legacy_role)
        except ValueError:
            out.append(
                f"legacy persona table names unknown role "
                f"{legacy_role!r}: not in the canonical registry"
            )
            continue
        canonical_persona = ROLE_REGISTRY[canonical].persona_name
        if legacy_persona != canonical_persona:
            out.append(
                f"role {legacy_role!r}: legacy={legacy_persona!r}, "
                f"canonical={canonical_persona!r}"
            )
    return tuple(out)
