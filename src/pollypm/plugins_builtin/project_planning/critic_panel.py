"""Critic panel provisioning + diversity resolver.

Spec §4: the architect emits 5 critic subtasks (simplicity,
maintainability, user, operational, security) as parallel short-lived
worker sessions. When more than one provider is registered (e.g.
Claude + Codex), the diversity resolver forces at least one critic
onto a provider *other than* the planner's. Model diversity ↔ less
correlated blind spots; this is the implementation hook that makes
the diversity promise real.

Book Ch 17 (Chain of Debates): multiple diverse critics > one critic,
and model diversity amplifies the effect.

This module is pure logic — no work-service I/O, no session spawning.
It takes the set of providers + the planner's provider + optional user
overrides from ``pollypm.toml`` and returns a mapping of
``critic_name -> provider_name`` that the caller applies when creating
the critic subtasks.
"""

from __future__ import annotations

from dataclasses import dataclass, field


CRITIC_NAMES: tuple[str, ...] = (
    "critic_simplicity",
    "critic_maintainability",
    "critic_user",
    "critic_operational",
    "critic_security",
)
"""The five critic personas assigned by the provisioner, in emission order."""

DEFAULT_DIVERSITY_CANDIDATE: str = "critic_simplicity"
"""When forcing cross-provider diversity, simplicity benefits most from
a different training-distribution view — the architect's reflex is to
add, the simplicity critic cuts, and cross-provider contrast sharpens
the cut. Override via ``pollypm.toml``."""


@dataclass(slots=True)
class CriticPanelAssignment:
    """Output of the diversity resolver.

    ``assignments`` — per-critic provider mapping.
    ``forced_cross_provider`` — the critic name that got bumped onto
    the non-planner provider (``None`` when single-provider).
    ``notes`` — short strings describing decisions; used for the session
    log so replays show why each critic landed where.
    """

    assignments: dict[str, str]
    forced_cross_provider: str | None = None
    notes: list[str] = field(default_factory=list)


def resolve_critic_providers(
    *,
    registered_providers: list[str],
    planner_provider: str,
    user_overrides: dict[str, str] | None = None,
    diversity_target: str = DEFAULT_DIVERSITY_CANDIDATE,
    critic_names: tuple[str, ...] = CRITIC_NAMES,
) -> CriticPanelAssignment:
    """Compute provider assignments for the 5-critic panel.

    Algorithm (spec §4 "Critic diversity rule"):

    1. **User overrides win absolutely.** If ``pollypm.toml`` says
       ``[planner.critics.critic_X] provider = "foo"`` we respect it
       regardless of diversity outcome.
    2. **Single-provider config:** every critic is assigned to that one
       provider. No cross-provider diversity is possible.
    3. **Multi-provider config:** start with every critic on the
       planner's provider. Then force ``diversity_target`` onto the
       first non-planner provider so the panel contains at least one
       cross-provider voice. If the diversity target is overridden by
       the user to be on the planner's provider (their right), pick
       another critic to be the forced diversity vote — specifically,
       the first critic from ``critic_names`` that isn't user-pinned to
       the planner's provider.

    Raises ``ValueError`` if no providers are registered, or if the
    planner's provider isn't in the registered list.
    """
    if not registered_providers:
        raise ValueError("resolve_critic_providers: no providers registered.")
    if planner_provider not in registered_providers:
        raise ValueError(
            f"Planner provider {planner_provider!r} not in registered "
            f"providers {registered_providers!r}."
        )

    overrides = dict(user_overrides or {})
    # Unknown critic names in overrides are passed through to notes so
    # users see typos surface; they are otherwise ignored.
    notes: list[str] = []
    for name in list(overrides.keys()):
        if name not in critic_names:
            notes.append(
                f"user override for unknown critic {name!r} ignored."
            )
            overrides.pop(name)
        elif overrides[name] not in registered_providers:
            notes.append(
                f"user override {name!r} -> {overrides[name]!r} ignored: "
                f"provider not registered."
            )
            overrides.pop(name)

    assignments: dict[str, str] = {}

    # Single-provider short-circuit.
    if len(set(registered_providers)) == 1:
        only = registered_providers[0]
        for critic in critic_names:
            assignments[critic] = overrides.get(critic, only)
        notes.append(
            f"single-provider config: all critics on {only!r}; "
            "no cross-provider diversity possible."
        )
        return CriticPanelAssignment(
            assignments=assignments, forced_cross_provider=None, notes=notes,
        )

    # Multi-provider path.
    non_planner = next(
        (p for p in registered_providers if p != planner_provider),
        None,
    )
    if non_planner is None:  # should be unreachable given len-check above
        non_planner = planner_provider

    # Default: every critic on planner's provider.
    for critic in critic_names:
        assignments[critic] = planner_provider

    # Apply user overrides (absolute precedence).
    for name, provider in overrides.items():
        assignments[name] = provider
        notes.append(f"user override: {name!r} -> {provider!r}")

    # Check: does at least one critic already sit on a non-planner provider?
    already_diverse = any(
        provider != planner_provider for provider in assignments.values()
    )

    forced_cross_provider: str | None = None
    if not already_diverse:
        # Pick the diversity target. If the user pinned the diversity
        # target to the planner's provider, fall through to the next
        # candidate that isn't pinned.
        target = diversity_target if diversity_target in critic_names else critic_names[0]
        for candidate in (target, *critic_names):
            if candidate in overrides:
                continue
            assignments[candidate] = non_planner
            forced_cross_provider = candidate
            notes.append(
                f"diversity resolver: forced {candidate!r} onto "
                f"{non_planner!r} (non-planner provider)."
            )
            break
        else:
            # Every critic was user-pinned to the planner's provider.
            notes.append(
                "diversity not enforced: every critic is user-pinned to "
                "the planner's provider."
            )

    return CriticPanelAssignment(
        assignments=assignments,
        forced_cross_provider=forced_cross_provider,
        notes=notes,
    )
