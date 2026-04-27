"""Tests for the idempotent launch state machine (#884).

Covers every :class:`LaunchState` plus the inventory reconciliation
helper (#871). The state machine is pure, so every test is a
synthesized :class:`LaunchProbe` rather than a live tmux fork.
"""

from __future__ import annotations

import pytest

from pollypm.launch_state import (
    InventoryDisagreement,
    LaunchAction,
    LaunchContext,
    LaunchPlan,
    LaunchProbe,
    LaunchState,
    plan_launch,
    reconcile_session_inventory,
)


# ---------------------------------------------------------------------------
# Probe builder — keeps tests tidy
# ---------------------------------------------------------------------------


def _probe(
    *,
    main_alive: bool = False,
    closet_alive: bool = False,
    console_alive: bool = True,
    rail_alive: bool = True,
    rail_running_non_shell: bool = True,
    current_tmux: str | None = None,
    upgrade_marker: bool = False,
    persisted: frozenset[str] = frozenset(),
    tmux_windows: frozenset[str] = frozenset(),
    main_name: str = "pollypm",
    closet_name: str = "pollypm-storage-closet",
) -> LaunchProbe:
    return LaunchProbe(
        main_session_name=main_name,
        closet_session_name=closet_name,
        main_session_alive=main_alive,
        closet_session_alive=closet_alive,
        console_pane_alive=console_alive,
        rail_pane_alive=rail_alive,
        rail_pane_running_non_shell=rail_running_non_shell,
        current_tmux_session=current_tmux,
        upgrade_marker_present=upgrade_marker,
        persisted_sessions=persisted,
        tmux_windows=tmux_windows,
    )


# ---------------------------------------------------------------------------
# FIRST_LAUNCH
# ---------------------------------------------------------------------------


def test_first_launch_outside_tmux() -> None:
    """No main, no closet, outside tmux → bootstrap + attach."""
    plan = plan_launch(_probe())
    assert plan.state is LaunchState.FIRST_LAUNCH
    assert plan.context is LaunchContext.OUTSIDE_TMUX
    assert LaunchAction.BOOTSTRAP_LAUNCHES in plan.actions
    assert LaunchAction.ENSURE_STORAGE_CLOSET in plan.actions
    assert LaunchAction.ENSURE_MAIN_SESSION in plan.actions
    assert plan.actions[-1] is LaunchAction.ATTACH_SESSION


def test_first_launch_inside_unrelated_tmux() -> None:
    """First launch inside someone else's tmux is allowed when the
    Polly main session does not exist — we'll still bootstrap, but
    the final action is a switch_client because the user already
    has a tmux client."""
    plan = plan_launch(_probe(current_tmux="otherwork"))
    assert plan.state is LaunchState.FIRST_LAUNCH
    assert plan.context is LaunchContext.INSIDE_UNRELATED_TMUX
    assert plan.actions[-1] is LaunchAction.SWITCH_CLIENT


def test_attach_existing_inside_unrelated_tmux_switches_client() -> None:
    """Inside an unrelated tmux while Polly is alive: ``tmux
    switch-client -t pollypm`` is the canonical handoff. Not
    unsupported — the existing CLI does this today and this
    test guards the behavior."""
    plan = plan_launch(
        _probe(
            main_alive=True,
            closet_alive=True,
            current_tmux="otherwork",
        )
    )
    assert plan.state is LaunchState.ATTACH_EXISTING
    assert plan.actions[-1] is LaunchAction.SWITCH_CLIENT


# ---------------------------------------------------------------------------
# ATTACH_EXISTING
# ---------------------------------------------------------------------------


def test_attach_existing_outside_tmux() -> None:
    """Healthy cockpit, outside tmux → attach without respawning rail."""
    plan = plan_launch(
        _probe(main_alive=True, closet_alive=True)
    )
    assert plan.state is LaunchState.ATTACH_EXISTING
    assert LaunchAction.RESPAWN_RAIL not in plan.actions
    assert LaunchAction.BOOTSTRAP_LAUNCHES not in plan.actions
    assert plan.actions[-1] is LaunchAction.ATTACH_SESSION


def test_attach_existing_inside_same_session_focuses_console() -> None:
    """Already inside the main session → just focus, do not attach."""
    plan = plan_launch(
        _probe(
            main_alive=True,
            closet_alive=True,
            current_tmux="pollypm",
        )
    )
    assert plan.context is LaunchContext.INSIDE_POLLY_TMUX_SAME_SESSION
    assert plan.actions[-1] is LaunchAction.FOCUS_CONSOLE


def test_attach_inside_storage_closet_switches_client() -> None:
    """Inside the closet (not main) → switch_client to main."""
    plan = plan_launch(
        _probe(
            main_alive=True,
            closet_alive=True,
            current_tmux="pollypm-storage-closet",
        )
    )
    assert plan.context is LaunchContext.INSIDE_POLLY_TMUX_DIFFERENT_SESSION
    assert plan.actions[-1] is LaunchAction.SWITCH_CLIENT


def test_attach_existing_does_not_respawn_live_rail() -> None:
    """The #841 contract: live non-shell rail must never be
    respawned during normal attach. The segfault path was tmux
    crashing on respawn-pane while the rail was still running."""
    plan = plan_launch(
        _probe(
            main_alive=True,
            closet_alive=True,
            rail_alive=True,
            rail_running_non_shell=True,
        )
    )
    assert plan.state is LaunchState.ATTACH_EXISTING
    assert LaunchAction.RESPAWN_RAIL not in plan.actions


# ---------------------------------------------------------------------------
# RESTORE_FROM_CLOSET
# ---------------------------------------------------------------------------


def test_restore_when_main_gone_but_closet_alive() -> None:
    """Closet survived a cockpit crash; main session vanished."""
    plan = plan_launch(_probe(main_alive=False, closet_alive=True))
    assert plan.state is LaunchState.RESTORE_FROM_CLOSET
    assert LaunchAction.BOOTSTRAP_LAUNCHES not in plan.actions
    assert LaunchAction.ENSURE_MAIN_SESSION in plan.actions


# ---------------------------------------------------------------------------
# RECOVER_DEAD_SHELL
# ---------------------------------------------------------------------------


def test_recover_dead_shell_respawns_shell_not_rail() -> None:
    """Console pane dead → respawn shell. Never the rail."""
    plan = plan_launch(
        _probe(
            main_alive=True,
            closet_alive=True,
            console_alive=False,
        )
    )
    assert plan.state is LaunchState.RECOVER_DEAD_SHELL
    assert LaunchAction.RESPAWN_SHELL in plan.actions
    assert LaunchAction.RESPAWN_RAIL not in plan.actions


# ---------------------------------------------------------------------------
# RECOVER_DEAD_RAIL
# ---------------------------------------------------------------------------


def test_recover_dead_rail_when_rail_genuinely_dead() -> None:
    """Rail pane is dead (not just non-shell) → respawn rail."""
    plan = plan_launch(
        _probe(
            main_alive=True,
            closet_alive=True,
            console_alive=True,
            rail_alive=False,
            rail_running_non_shell=False,
        )
    )
    assert plan.state is LaunchState.RECOVER_DEAD_RAIL
    assert LaunchAction.RESPAWN_RAIL in plan.actions


# ---------------------------------------------------------------------------
# RECOVER_MISSING_CLOSET
# ---------------------------------------------------------------------------


def test_recover_missing_closet_rebuilds_and_reconciles() -> None:
    """Main alive but closet gone → rebuild closet + bootstrap."""
    plan = plan_launch(
        _probe(main_alive=True, closet_alive=False)
    )
    assert plan.state is LaunchState.RECOVER_MISSING_CLOSET
    assert LaunchAction.ENSURE_STORAGE_CLOSET in plan.actions
    assert LaunchAction.BOOTSTRAP_LAUNCHES in plan.actions


# ---------------------------------------------------------------------------
# UPGRADE_RESTART
# ---------------------------------------------------------------------------


def test_upgrade_restart_attaches_without_bootstrap() -> None:
    """Upgrade marker + main alive → controlled relaunch without
    re-bootstrap. The marker says workers were intentionally
    killed and will be reclaimed by the heartbeat."""
    plan = plan_launch(
        _probe(
            main_alive=True,
            closet_alive=True,
            upgrade_marker=True,
        )
    )
    assert plan.state is LaunchState.UPGRADE_RESTART
    assert LaunchAction.BOOTSTRAP_LAUNCHES not in plan.actions
    assert LaunchAction.SCHEDULE_HEARTBEAT in plan.actions


def test_upgrade_marker_ignored_when_main_dead() -> None:
    """If main is dead, the upgrade-restart path is meaningless;
    fall back to first-launch / restore-from-closet."""
    plan = plan_launch(
        _probe(
            main_alive=False,
            closet_alive=True,
            upgrade_marker=True,
        )
    )
    assert plan.state is LaunchState.RESTORE_FROM_CLOSET


# ---------------------------------------------------------------------------
# UNSUPPORTED — fail closed
# ---------------------------------------------------------------------------


def test_unsupported_fails_closed_when_session_names_missing() -> None:
    """A probe whose own session names are empty (config did not
    supply `project.tmux_session`) is the genuinely unsupported
    case. Refusing here keeps the launcher from speculatively
    mutating ambient tmux state."""
    plan = plan_launch(_probe(main_name="", closet_name=""))
    assert plan.state is LaunchState.UNSUPPORTED
    assert plan.actions == (LaunchAction.FAIL_CLOSED,)
    assert "tmux_session" in plan.reason or "names missing" in plan.reason


def test_fail_closed_plan_is_terminal() -> None:
    """A fail-closed plan must be a single FAIL_CLOSED action so
    the runtime cannot accidentally run repair steps before the
    refuse-to-act decision."""
    plan = plan_launch(_probe(main_name="", closet_name=""))
    assert len(plan.actions) == 1


# ---------------------------------------------------------------------------
# Probe context classification
# ---------------------------------------------------------------------------


def test_context_outside_tmux() -> None:
    assert _probe().context is LaunchContext.OUTSIDE_TMUX


def test_context_inside_main() -> None:
    assert _probe(current_tmux="pollypm").context is (
        LaunchContext.INSIDE_POLLY_TMUX_SAME_SESSION
    )


def test_context_inside_closet() -> None:
    assert _probe(
        current_tmux="pollypm-storage-closet"
    ).context is LaunchContext.INSIDE_POLLY_TMUX_DIFFERENT_SESSION


def test_context_inside_unrelated() -> None:
    assert _probe(
        current_tmux="otherwork"
    ).context is LaunchContext.INSIDE_UNRELATED_TMUX


# ---------------------------------------------------------------------------
# Inventory reconciliation (#871)
# ---------------------------------------------------------------------------


def test_reconcile_inventory_clean_when_sets_agree() -> None:
    """Same names in both reads → no disagreements."""
    out = reconcile_session_inventory(
        persisted={"operator", "reviewer", "heartbeat"},
        live={"operator", "reviewer", "heartbeat"},
    )
    assert out == ()


def test_reconcile_inventory_flags_persisted_only() -> None:
    """Persisted but not in tmux → ``missing_in_tmux``.

    The audit's #871 inverse: the cockpit thinks five sessions
    are alive but tmux only has three. The reconcile helper
    surfaces each one so the cockpit can render an accurate
    "your sessions table is out of date" warning."""
    out = reconcile_session_inventory(
        persisted={"operator", "reviewer", "heartbeat"},
        live={"operator"},
    )
    kinds = {(d.kind, d.name) for d in out}
    assert ("missing_in_tmux", "reviewer") in kinds
    assert ("missing_in_tmux", "heartbeat") in kinds


def test_reconcile_inventory_flags_live_only() -> None:
    """Live but not persisted → ``missing_in_persisted``.

    The original #871 shape: tmux had live worker/architect/
    reviewer/operator/heartbeat windows, the persisted sessions
    table reported zero. The reconcile helper emits one entry
    per orphan."""
    out = reconcile_session_inventory(
        persisted=set(),
        live={"operator", "reviewer", "heartbeat"},
    )
    assert len(out) == 3
    kinds = {d.kind for d in out}
    assert kinds == {"missing_in_persisted"}


def test_reconcile_inventory_results_are_sorted_for_stable_diff() -> None:
    """Stable ordering keeps test diffs and log lines diff-clean."""
    out = reconcile_session_inventory(
        persisted={"zeta", "alpha"},
        live=set(),
    )
    names = [d.name for d in out]
    assert names == sorted(names)


# ---------------------------------------------------------------------------
# Plan immutability + structure
# ---------------------------------------------------------------------------


def test_plan_is_frozen() -> None:
    """``LaunchPlan`` is frozen so a caller cannot mutate the
    canonical answer it received."""
    plan = plan_launch(_probe())
    with pytest.raises((AttributeError, TypeError)):
        plan.state = LaunchState.UNSUPPORTED  # type: ignore[misc]


def test_every_state_is_reachable() -> None:
    """Each :class:`LaunchState` value must be reachable from
    *some* probe — protects against a state being added to the
    enum but never assigned by the planner."""
    expected = {state for state in LaunchState}
    seen: set[LaunchState] = set()

    seen.add(plan_launch(_probe()).state)  # FIRST_LAUNCH
    seen.add(plan_launch(_probe(main_alive=True, closet_alive=True)).state)  # ATTACH
    seen.add(plan_launch(_probe(closet_alive=True)).state)  # RESTORE
    seen.add(
        plan_launch(_probe(main_alive=True, closet_alive=True, console_alive=False)).state
    )  # RECOVER_DEAD_SHELL
    seen.add(
        plan_launch(
            _probe(
                main_alive=True,
                closet_alive=True,
                rail_alive=False,
                rail_running_non_shell=False,
            )
        ).state
    )  # RECOVER_DEAD_RAIL
    seen.add(plan_launch(_probe(main_alive=True)).state)  # RECOVER_MISSING_CLOSET
    seen.add(
        plan_launch(
            _probe(main_alive=True, closet_alive=True, upgrade_marker=True)
        ).state
    )  # UPGRADE_RESTART
    seen.add(
        plan_launch(_probe(main_name="", closet_name="")).state
    )  # UNSUPPORTED

    assert seen == expected, (
        f"unreachable states: {expected - seen}"
    )
