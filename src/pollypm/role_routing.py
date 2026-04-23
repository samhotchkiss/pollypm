from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
from typing import Literal

from pollypm.config import DEFAULT_CONFIG_PATH, load_config
from pollypm.model_registry import Registry, load_registry, resolve_alias
from pollypm.models import ModelAssignment, PollyPMConfig


_log = logging.getLogger(__name__)
_ROLE_KEYS = ("operator_pm", "architect", "worker", "reviewer")
_FALLBACK_ASSIGNMENTS: dict[str, ModelAssignment] = {
    "operator_pm": ModelAssignment(alias="codex-gpt-5.4"),
    "architect": ModelAssignment(alias="opus-4.7"),
    "worker": ModelAssignment(alias="codex-gpt-5.4"),
    "reviewer": ModelAssignment(alias="sonnet-4.6"),
}


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

    fallback = _resolved_from_assignment(
        canonical_role,
        _FALLBACK_ASSIGNMENTS[canonical_role],
        source="fallback",
        registry=resolved_registry,
    )
    if fallback is None:
        raise RuntimeError(f"Fallback role assignment for {canonical_role} is invalid")
    return fallback


class RoleRoutingFacade:
    def __init__(self, config_path: Path) -> None:
        self._config_path = config_path

    def resolve(self, role: str, project_key: str | None = None) -> ResolvedAssignment:
        return resolve_role_assignment(
            role,
            project_key,
            config=load_config(self._config_path),
        )


__all__ = [
    "ResolvedAssignment",
    "RoleRoutingFacade",
    "resolve_role_assignment",
]
