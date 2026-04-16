"""Per-stage time budgets for the planning flows (pp07).

Spec §6 defines a default per-stage budget for every node in
``plan_project`` and ``critique_flow``. Defaults live in the YAML
``budget_seconds`` field. Users override in ``pollypm.toml``:

```toml
[planner.budgets]
research = 600
discover = 300
decompose = 900     # lifts the cap from the 600s default
test_strategy = 300
magic = 600
critic = 300        # applies to every critic subtask
synthesize = 600
```

This module:

1. Exposes ``DEFAULT_BUDGETS`` — the spec §6 table.
2. Reads ``[planner.budgets]`` out of the loaded config (a ``raw``
   dict or a ``PollyPMConfig`` instance with a ``raw_toml`` hook —
   callers handle both).
3. Provides ``effective_budget(stage, config=None, node=None)`` that
   applies the precedence chain: ``pollypm.toml`` > flow node YAML
   default > hard-coded DEFAULT_BUDGETS.

Budgets are wall-clock per session, not cumulative across the run.
Parallel critic sessions each get their own budget. A ``None`` result
means "no enforcement at the flow level" — the session runtime's own
caps take over.
"""

from __future__ import annotations

from typing import Any


DEFAULT_BUDGETS: dict[str, int] = {
    "research": 600,        # 10 min
    "discover": 300,        # 5 min
    "decompose": 600,       # 10 min (per candidate × 2-3 candidates)
    "test_strategy": 300,   # 5 min
    "magic": 600,           # 10 min
    "critic": 300,          # 5 min per critic
    "synthesize": 600,      # 10 min
    # emit + user_approval intentionally omitted: emit is quick I/O and
    # user_approval waits indefinitely for the human touchpoint.
}
"""Spec §6 default budgets keyed by stage name.

Stage names match the ``plan_project`` node names exactly except
``critic``, which is the umbrella knob for every critic subtask in the
panel (there is no single ``critic`` node — the knob applies to the
critique_flow.critique node uniformly).
"""


def _read_budget_overrides(config: Any) -> dict[str, int]:
    """Pull ``[planner.budgets]`` out of a ``PollyPMConfig`` or dict.

    The work-service config layer doesn't have a typed section for
    planner budgets (yet), so we probe a few common shapes in order
    of preference:

    1. ``config.raw_toml`` — if the loader preserves the original TOML.
    2. ``config.planner`` — if a future typed section appears.
    3. ``config`` as a plain ``dict`` — direct-dict callers (tests).

    Unknown shapes silently produce ``{}`` so a missing config never
    crashes the planner.
    """
    if config is None:
        return {}

    # Direct-dict path — tests pass a dict.
    if isinstance(config, dict):
        section = config.get("planner", {}).get("budgets", {})
        return _normalise(section)

    # Attribute path — PollyPMConfig with a future .planner.budgets.
    planner = getattr(config, "planner", None)
    if planner is not None:
        budgets = getattr(planner, "budgets", None)
        if isinstance(budgets, dict):
            return _normalise(budgets)

    # Fallback: raw_toml.
    raw = getattr(config, "raw_toml", None)
    if isinstance(raw, dict):
        section = raw.get("planner", {}).get("budgets", {})
        return _normalise(section)

    return {}


def _normalise(section: Any) -> dict[str, int]:
    """Coerce a raw ``[planner.budgets]`` table into a safe dict."""
    out: dict[str, int] = {}
    if not isinstance(section, dict):
        return out
    for key, value in section.items():
        if not isinstance(key, str):
            continue
        if isinstance(value, int) and value > 0:
            out[key] = value
        elif isinstance(value, float) and value > 0:
            out[key] = int(value)
    return out


def effective_budget(
    stage: str,
    *,
    config: Any = None,
    node_default: int | None = None,
) -> int | None:
    """Return the effective wall-clock budget (seconds) for a stage.

    Precedence (highest first):

    1. ``[planner.budgets].<stage>`` in ``pollypm.toml`` (via config).
    2. ``node_default`` — the ``budget_seconds`` on the flow node.
    3. ``DEFAULT_BUDGETS[stage]`` — the spec §6 hard-coded default.
    4. ``None`` — stage has no default and no override.
    """
    overrides = _read_budget_overrides(config)
    if stage in overrides:
        return overrides[stage]
    if node_default is not None and node_default > 0:
        return node_default
    return DEFAULT_BUDGETS.get(stage)


def all_effective_budgets(config: Any = None) -> dict[str, int | None]:
    """Return every known stage's effective budget in one snapshot.

    Useful for the session log: at run start, record the effective
    budgets so replays show which stages had config overrides.
    """
    overrides = _read_budget_overrides(config)
    merged: dict[str, int | None] = dict(DEFAULT_BUDGETS)
    for stage, value in overrides.items():
        merged[stage] = value
    return merged
