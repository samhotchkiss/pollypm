from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
from typing import Literal

from pollypm.config import DEFAULT_CONFIG_PATH, load_config
from pollypm.model_registry import Registry, load_registry, resolve_alias
from pollypm.models import ModelAssignment, PollyPMConfig, ProviderKind


_log = logging.getLogger(__name__)
_ROLE_KEYS = ("operator_pm", "architect", "worker", "reviewer", "advisor")

# Default role -> alias table when BOTH Claude and Codex accounts are
# configured. Opus drives the PM-style roles (operator, reviewer) where
# nuanced project judgment matters; Codex drives the heavy tool-use
# roles (worker, architect, advisor) where its tool dispatch shines.
# See #1737.
_DUAL_PROVIDER_DEFAULTS: dict[str, ModelAssignment] = {
    "operator_pm": ModelAssignment(alias="opus-4.7"),
    "architect": ModelAssignment(alias="codex-gpt-5.4"),
    "worker": ModelAssignment(alias="codex-gpt-5.4"),
    "reviewer": ModelAssignment(alias="opus-4.7"),
    "advisor": ModelAssignment(alias="codex-gpt-5.4"),
}

# Single-provider defaults — when only Claude (or only Codex) accounts
# are configured, every role falls back to that provider's strongest
# alias. Preserves the "config still works with one provider" invariant.
_CLAUDE_ONLY_DEFAULTS: dict[str, ModelAssignment] = {
    "operator_pm": ModelAssignment(alias="opus-4.7"),
    "architect": ModelAssignment(alias="opus-4.7"),
    "worker": ModelAssignment(alias="opus-4.7"),
    "reviewer": ModelAssignment(alias="opus-4.7"),
    "advisor": ModelAssignment(alias="opus-4.7"),
}
_CODEX_ONLY_DEFAULTS: dict[str, ModelAssignment] = {
    "operator_pm": ModelAssignment(alias="codex-gpt-5.4"),
    "architect": ModelAssignment(alias="codex-gpt-5.4"),
    "worker": ModelAssignment(alias="codex-gpt-5.4"),
    "reviewer": ModelAssignment(alias="codex-gpt-5.4"),
    "advisor": ModelAssignment(alias="codex-gpt-5.4"),
}

# Static safety net used when no PollyPMConfig is in hand or no
# accounts are configured (e.g. some unit tests). Matches the historical
# table that shipped before #1737 so anything reaching this branch
# behaves like the pre-#1737 system.
_FALLBACK_ASSIGNMENTS: dict[str, ModelAssignment] = {
    "operator_pm": ModelAssignment(alias="codex-gpt-5.4"),
    "architect": ModelAssignment(alias="opus-4.7"),
    "worker": ModelAssignment(alias="codex-gpt-5.4"),
    "reviewer": ModelAssignment(alias="sonnet-4.6"),
    "advisor": ModelAssignment(alias="opus-4.7"),
}


def _configured_providers(config: PollyPMConfig | None) -> frozenset[str]:
    """Return the set of provider names with at least one account configured."""
    if config is None:
        return frozenset()
    providers: set[str] = set()
    for account in getattr(config, "accounts", {}).values():
        provider = getattr(account, "provider", None)
        # ProviderKind enum has a .value attribute; coerce defensively
        # so tests that pass raw strings keep working.
        if provider is None:
            continue
        value = getattr(provider, "value", provider)
        if isinstance(value, str) and value:
            providers.add(value)
    return frozenset(providers)


def _select_fallback_assignment(
    canonical_role: str,
    config: PollyPMConfig | None,
) -> ModelAssignment:
    """Pick the fallback assignment for ``canonical_role``.

    Selection rule (#1737):

    * Both Claude and Codex accounts configured -> dual-provider table
      (Opus for PM/operator/reviewer, Codex for worker/architect/advisor).
    * Only Claude accounts -> Opus across the board.
    * Only Codex accounts -> Codex across the board.
    * Neither (no config or empty accounts) -> legacy static table.
    """
    providers = _configured_providers(config)
    has_claude = "claude" in providers
    has_codex = "codex" in providers
    if has_claude and has_codex:
        table = _DUAL_PROVIDER_DEFAULTS
    elif has_claude:
        table = _CLAUDE_ONLY_DEFAULTS
    elif has_codex:
        table = _CODEX_ONLY_DEFAULTS
    else:
        table = _FALLBACK_ASSIGNMENTS
    return table[canonical_role]


@dataclass(slots=True, frozen=True)
class ResolvedAssignment:
    provider: str
    model: str
    alias: str | None
    source: Literal["project", "global", "fallback"]


def _canonical_role(role: str) -> str:
    canonical = (role or "").strip().replace("-", "_")
    if canonical not in _ROLE_KEYS:
        raise ValueError(
            f"Unknown role {role!r}. Expected one of: {', '.join(_ROLE_KEYS)}"
        )
    return canonical


def _resolved_from_assignment(
    role: str,
    assignment: ModelAssignment,
    *,
    source: Literal["project", "global", "fallback"],
    registry: Registry,
) -> ResolvedAssignment | None:
    if assignment.alias is not None:
        resolved = resolve_alias(assignment.alias, registry=registry)
        if resolved is None:
            _log.warning(
                "Unknown model alias %r for %s from %s scope; falling through.",
                assignment.alias,
                role,
                source,
            )
            return None
        return ResolvedAssignment(
            provider=resolved.provider or "",
            model=resolved.model or "",
            alias=assignment.alias,
            source=source,
        )
    return ResolvedAssignment(
        provider=assignment.provider or "",
        model=assignment.model or "",
        alias=None,
        source=source,
    )


def resolve_role_assignment(
    role: str,
    project_key: str | None = None,
    *,
    config: PollyPMConfig | None = None,
    registry: Registry | None = None,
) -> ResolvedAssignment:
    canonical_role = _canonical_role(role)
    resolved_registry = registry or load_registry()
    current_config = config or load_config(DEFAULT_CONFIG_PATH)

    if project_key is not None:
        project = current_config.projects.get(project_key)
        if project is not None:
            project_assignment = project.role_assignments.get(canonical_role)
            if project_assignment is not None:
                resolved = _resolved_from_assignment(
                    canonical_role,
                    project_assignment,
                    source="project",
                    registry=resolved_registry,
                )
                if resolved is not None:
                    return resolved

    global_assignment = current_config.pollypm.role_assignments.get(canonical_role)
    if global_assignment is not None:
        resolved = _resolved_from_assignment(
            canonical_role,
            global_assignment,
            source="global",
            registry=resolved_registry,
        )
        if resolved is not None:
            return resolved

    fallback_assignment = _select_fallback_assignment(canonical_role, current_config)
    fallback = _resolved_from_assignment(
        canonical_role,
        fallback_assignment,
        source="fallback",
        registry=resolved_registry,
    )
    if fallback is None:
        # Resolved alias miss against the live registry — fall back to
        # the static legacy table, which is hand-aligned with the
        # baked-in registry in :mod:`pollypm.model_registry`.
        fallback = _resolved_from_assignment(
            canonical_role,
            _FALLBACK_ASSIGNMENTS[canonical_role],
            source="fallback",
            registry=resolved_registry,
        )
    if fallback is None:
        raise RuntimeError(f"Fallback role assignment for {canonical_role} is invalid")
    return fallback


def rewrite_assignment_for_provider(
    role: str,
    provider: ProviderKind | str,
    config: PollyPMConfig | None,
    *,
    registry: Registry | None = None,
) -> ResolvedAssignment | None:
    """Return a ``ResolvedAssignment`` whose provider matches ``provider``.

    Used by the launch planner (#1879) when a runtime account override
    has pinned the session's provider but the role-routing fallback
    table named a different provider. Picks the alias from the
    matching single-provider table so the launch argv still carries
    a sensible ``--model`` flag.
    """
    try:
        canonical = _canonical_role(role)
    except ValueError:
        return None
    provider_value = getattr(provider, "value", provider)
    if not isinstance(provider_value, str) or not provider_value:
        return None
    if provider_value == "claude":
        assignment = _CLAUDE_ONLY_DEFAULTS.get(canonical)
    elif provider_value == "codex":
        assignment = _CODEX_ONLY_DEFAULTS.get(canonical)
    else:
        assignment = None
    if assignment is None:
        return None
    resolved_registry = registry or load_registry()
    return _resolved_from_assignment(
        canonical,
        assignment,
        source="fallback",
        registry=resolved_registry,
    )


class RoleRoutingFacade:
    def __init__(self, config_path: Path) -> None:
        self._config_path = config_path

    def resolve(self, role: str, project_key: str | None = None) -> ResolvedAssignment:
        return resolve_role_assignment(
            role,
            project_key,
            config=load_config(self._config_path),
        )


def resolved_provider_kind(resolved: ResolvedAssignment) -> ProviderKind:
    return ProviderKind(resolved.provider)


__all__ = [
    "ResolvedAssignment",
    "RoleRoutingFacade",
    "resolved_provider_kind",
    "resolve_role_assignment",
    "rewrite_assignment_for_provider",
]
