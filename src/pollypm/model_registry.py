from __future__ import annotations

from dataclasses import dataclass, field
from importlib import resources
import logging
from pathlib import Path
import tomllib

from pollypm.models import ModelAssignment


_log = logging.getLogger(__name__)
_DEFAULT_OVERLAY_PATH = Path.home() / ".pollypm" / "model_registry.toml"


@dataclass(slots=True, frozen=True)
class AliasRecord:
    provider: str
    model: str
    capabilities: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class RoleRequirements:
    preferred: tuple[str, ...] = ()
    discouraged: tuple[str, ...] = ()


@dataclass(slots=True)
class Registry:
    aliases: dict[str, AliasRecord] = field(default_factory=dict)
    role_requirements: dict[str, RoleRequirements] = field(default_factory=dict)


def _fallback_registry() -> Registry:
    return Registry(
        aliases={
            "opus-4.7": AliasRecord(
                provider="claude",
                model="claude-opus-4-7",
                capabilities=(
                    "reasoning",
                    "tool_use",
                    "long_context",
                    "strong_planning",
                ),
            ),
            "sonnet-4.6": AliasRecord(
                provider="claude",
                model="claude-sonnet-4-6",
                capabilities=(
                    "reasoning",
                    "tool_use",
                    "long_context",
                    "balanced_planning",
                ),
            ),
            "haiku-4.5": AliasRecord(
                provider="claude",
                model="claude-haiku-4-5-20251001",
                capabilities=(
                    "tool_use",
                    "fast",
                    "weak_planning",
                ),
            ),
            "codex-gpt-5.4": AliasRecord(
                provider="codex",
                model="gpt-5.4",
                capabilities=(
                    "reasoning",
                    "tool_use",
                    "long_context",
                    "strong_planning",
                ),
            ),
            "codex-gpt-5.5": AliasRecord(
                provider="codex",
                model="gpt-5.5",
                capabilities=(
                    "reasoning",
                    "tool_use",
                    "long_context",
                    "strong_planning",
                ),
            ),
        },
        role_requirements={
            "operator_pm": RoleRequirements(
                preferred=("reasoning", "long_context"),
                discouraged=("weak_planning",),
            ),
            "architect": RoleRequirements(
                preferred=("strong_planning", "reasoning"),
                discouraged=("weak_planning",),
            ),
            "worker": RoleRequirements(
                preferred=("tool_use", "reasoning"),
                discouraged=(),
            ),
            "reviewer": RoleRequirements(
                preferred=("reasoning", "long_context"),
                discouraged=("weak_planning",),
            ),
        },
    )


def _clean_str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    cleaned: list[str] = []
    for item in value:
        entry = _clean_str(item)
        if entry is not None:
            cleaned.append(entry)
    return tuple(dict.fromkeys(cleaned))


def _iter_alias_tables(
    raw_aliases: dict[str, object],
    *,
    prefix: str = "",
):
    for alias, alias_raw in raw_aliases.items():
        if not isinstance(alias, str) or not isinstance(alias_raw, dict):
            continue
        full_alias = f"{prefix}.{alias}" if prefix else alias
        provider = _clean_str(alias_raw.get("provider"))
        model = _clean_str(alias_raw.get("model"))
        if provider is not None and model is not None:
            yield full_alias, alias_raw
            continue
        nested_tables = any(isinstance(value, dict) for value in alias_raw.values())
        if nested_tables:
            yield from _iter_alias_tables(alias_raw, prefix=full_alias)
            continue
        yield full_alias, alias_raw


def _read_shipped_registry_text() -> str:
    ref = resources.files("pollypm").joinpath("model_registry.toml")
    return ref.read_text(encoding="utf-8")


def _load_toml_text(text: str, *, source: str) -> dict[str, object] | None:
    try:
        raw = tomllib.loads(text)
    except Exception as exc:  # noqa: BLE001
        _log.warning("Failed to parse %s: %s. Falling back to minimal registry.", source, exc)
        return None
    if not isinstance(raw, dict):
        _log.warning("Failed to parse %s: top-level document must be a table.", source)
        return None
    return raw


def _registry_from_raw(raw: dict[str, object], *, source: str) -> Registry:
    registry = Registry()
    aliases_raw = raw.get("aliases", {})
    if isinstance(aliases_raw, dict):
        for alias, alias_raw in _iter_alias_tables(aliases_raw):
            provider = _clean_str(alias_raw.get("provider"))
            model = _clean_str(alias_raw.get("model"))
            if provider is None or model is None:
                _log.warning(
                    "Ignoring invalid alias %r in %s: provider/model required.",
                    alias,
                    source,
                )
                continue
            registry.aliases[alias] = AliasRecord(
                provider=provider,
                model=model,
                capabilities=_string_tuple(alias_raw.get("capabilities", [])),
            )
    requirements_raw = raw.get("role_requirements", {})
    if isinstance(requirements_raw, dict):
        for role, role_raw in requirements_raw.items():
            if not isinstance(role, str) or not isinstance(role_raw, dict):
                continue
            registry.role_requirements[role] = RoleRequirements(
                preferred=_string_tuple(role_raw.get("preferred", [])),
                discouraged=_string_tuple(role_raw.get("discouraged", [])),
            )
    return registry


def _merge_registry(base: Registry, overlay: Registry) -> Registry:
    aliases = dict(base.aliases)
    aliases.update(overlay.aliases)
    role_requirements = dict(base.role_requirements)
    role_requirements.update(overlay.role_requirements)
    return Registry(aliases=aliases, role_requirements=role_requirements)


def load_registry(overlay_path: Path | None = None) -> Registry:
    registry = _fallback_registry()
    shipped_raw = _load_toml_text(
        _read_shipped_registry_text(),
        source="shipped model registry",
    )
    if shipped_raw is not None:
        registry = _merge_registry(
            registry,
            _registry_from_raw(shipped_raw, source="shipped model registry"),
        )

    candidate_overlay = _DEFAULT_OVERLAY_PATH if overlay_path is None else overlay_path
    if candidate_overlay is None or not candidate_overlay.exists():
        return registry

    overlay_raw = _load_toml_text(
        candidate_overlay.read_text(encoding="utf-8"),
        source=str(candidate_overlay),
    )
    if overlay_raw is None:
        return registry
    return _merge_registry(
        registry,
        _registry_from_raw(overlay_raw, source=str(candidate_overlay)),
    )


def resolve_alias(
    alias: str,
    *,
    registry: Registry | None = None,
) -> ModelAssignment | None:
    entry = (registry or load_registry()).aliases.get(alias)
    if entry is None:
        return None
    return ModelAssignment(provider=entry.provider, model=entry.model)


def _record_for_assignment(
    assignment: ModelAssignment,
    *,
    registry: Registry,
) -> AliasRecord | None:
    if assignment.alias is not None:
        return registry.aliases.get(assignment.alias)
    for record in registry.aliases.values():
        if record.provider == assignment.provider and record.model == assignment.model:
            return record
    return None


def advisories_for(
    role: str,
    assignment: ModelAssignment,
    *,
    registry: Registry | None = None,
) -> list[str]:
    resolved_registry = registry or load_registry()
    requirements = resolved_registry.role_requirements.get(role)
    if requirements is None:
        return []
    record = _record_for_assignment(assignment, registry=resolved_registry)
    if record is None:
        return []

    capabilities = set(record.capabilities)
    advisories: list[str] = []
    for discouraged in requirements.discouraged:
        if discouraged in capabilities:
            advisories.append(
                f"{role} is not recommended for models tagged {discouraged}."
            )
    if advisories:
        return advisories

    missing = [
        preferred
        for preferred in requirements.preferred
        if preferred not in capabilities
    ]
    if missing:
        advisories.append(
            f"{role} works best with {', '.join(missing)}; current model may be a weak match."
        )
    return advisories


__all__ = [
    "AliasRecord",
    "Registry",
    "RoleRequirements",
    "advisories_for",
    "load_registry",
    "resolve_alias",
]
