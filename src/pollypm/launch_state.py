"""Idempotent tmux/session launch state machine (#884).

Models ``pm up`` as an explicit state machine instead of the
sequence of nested if/else branches in :mod:`pollypm.cli`. The
state machine is pure: from a read-only :class:`LaunchProbe`
(snapshot of tmux state), it returns a :class:`LaunchPlan` that
names the state, names the actions to take, and explains the
reasoning.

The pre-launch audit (``docs/launch-issue-audit-2026-04-27.md``
§4) cites the recurring shape: launch / reattach / upgrade
restart / stale-pane recovery were handled as overlapping branches,
not separate states. Symptoms:

* ``#841`` — relaunch / respawn hit a tmux segfault and dropped
  the cockpit session into raw Claude Code without a Polly
  return affordance.
* ``#871`` — cockpit session inventory reported zero sessions
  while tmux had live operator/reviewer/architect/heartbeat
  windows.
* ``#817`` — ``pm up`` created an idle shell where it should
  have attached an existing rail.
* ``#808`` — upgrade restart cycle dropped persistent state.

The state machine is the structural fix. ``pm up`` calls
:func:`plan_launch` to decide *what* to do, then performs the
actions. The test suite exercises every state by constructing a
synthetic probe — no live tmux required.

Migration policy: the existing call sites in :mod:`pollypm.cli`
keep their concrete behavior but consult the state machine for
classification. New launch paths must go through the state
machine; the contract is a launch test (#884 acceptance criteria
4) covering outside tmux, inside unrelated tmux, inside existing
Polly tmux, after upgrade, with stale/dead panes, with storage
closet already running.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Iterable


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class LaunchContext(enum.Enum):
    """Where the user is when they run ``pm up``."""

    OUTSIDE_TMUX = "outside_tmux"
    """No ``$TMUX`` env var; user is at a normal shell prompt."""

    INSIDE_UNRELATED_TMUX = "inside_unrelated_tmux"
    """Inside a tmux session that is not a Polly session."""

    INSIDE_POLLY_TMUX_SAME_SESSION = "inside_polly_tmux_same_session"
    """Inside the Polly main session — already there."""

    INSIDE_POLLY_TMUX_DIFFERENT_SESSION = "inside_polly_tmux_different_session"
    """Inside a Polly session (e.g., the storage closet) but not
    the main cockpit session — needs a switch."""

    UNSUPPORTED = "unsupported"
    """A combination the state machine refuses to act on (e.g.,
    inside an SSH-nested unrelated tmux that we cannot safely
    join). Returns a fail-closed plan with an actionable message."""


class LaunchState(enum.Enum):
    """The high-level state ``pm up`` is in.

    Each state has exactly one canonical action plan. The plan
    function may produce additional repair sub-actions, but the
    state itself is a single label so logs and tests can name
    it unambiguously.
    """

    FIRST_LAUNCH = "first_launch"
    """No Polly main session, no storage closet. Bootstrap."""

    ATTACH_EXISTING = "attach_existing"
    """Polly main session exists, all windows healthy. Attach."""

    RESTORE_FROM_CLOSET = "restore_from_closet"
    """Storage closet alive but main session gone. Recreate the
    main console window and rejoin."""

    RECOVER_DEAD_SHELL = "recover_dead_shell"
    """Main session exists but the console pane is dead.
    Respawn the shell only — never the rail."""

    RECOVER_DEAD_RAIL = "recover_dead_rail"
    """Rail pane is dead and the rail is the only thing missing.
    Respawn rail. ``#841``: never respawn a *live* non-shell
    rail pane — that path hit a tmux segfault."""

    RECOVER_MISSING_CLOSET = "recover_missing_closet"
    """Main session exists but storage closet is gone. Recreate
    the closet and reconcile windows."""

    UPGRADE_RESTART = "upgrade_restart"
    """Post-upgrade: ``pm upgrade`` left an explicit marker.
    Controlled relaunch — different from a generic recovery
    because the rail and workers were intentionally killed."""

    UNSUPPORTED = "unsupported"
    """Fail closed with an actionable message. Used when the
    user is inside an unrelated tmux nested session, when the
    persisted session inventory disagrees with tmux in a way the
    state machine cannot reconcile, or when an environment guard
    fails."""


class LaunchAction(enum.Enum):
    """A single primitive action the runtime must perform.

    ``LaunchPlan.actions`` is an ordered tuple of these. The
    runtime executes them in order, halting on first failure.
    """

    ENSURE_STORAGE_CLOSET = "ensure_storage_closet"
    """Create the storage-closet session if missing."""

    ENSURE_MAIN_SESSION = "ensure_main_session"
    """Create the main Polly session with the console window."""

    BOOTSTRAP_LAUNCHES = "bootstrap_launches"
    """Run the controller-account selection + per-session
    launches. Heaviest action; only valid in FIRST_LAUNCH or
    after the closet has been recreated."""

    ENSURE_CONSOLE_WINDOW = "ensure_console_window"
    """The cockpit's console window. Idempotent."""

    RESPAWN_SHELL = "respawn_shell"
    """Respawn the dead shell pane."""

    RESPAWN_RAIL = "respawn_rail"
    """Respawn the rail pane. Forbidden when the rail is *live
    and not a shell* — see ``#841``."""

    SCHEDULE_HEARTBEAT = "schedule_heartbeat"
    """Make sure the heartbeat is scheduled."""

    START_RAIL_DAEMON = "start_rail_daemon"
    """Best-effort spawn of the headless rail daemon so
    heartbeat / recovery keep ticking outside the cockpit."""

    ATTACH_SESSION = "attach_session"
    """Attach the user's terminal to the main session
    (outside-tmux case)."""

    SWITCH_CLIENT = "switch_client"
    """Switch the user's existing tmux client to the main
    session (inside-other-tmux case)."""

    FOCUS_CONSOLE = "focus_console"
    """Bring focus to the console window without attach
    (already-inside case)."""

    FAIL_CLOSED = "fail_closed"
    """Refuse to act. The plan ``reason`` carries the user-
    facing message. Used in UNSUPPORTED."""


# ---------------------------------------------------------------------------
# Probe / Plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LaunchProbe:
    """Read-only snapshot of tmux + filesystem state.

    The state machine consumes this and produces a plan. Because
    the probe is a frozen dataclass, every test can construct any
    state — no tmux fork required.

    Fields:

    * ``main_session_name`` — config's ``project.tmux_session``
      (e.g., ``"pollypm"``).
    * ``closet_session_name`` — derived storage-closet session
      (``"<main>-storage-closet"``).
    * ``main_session_alive`` — True if the main session exists.
    * ``closet_session_alive`` — True if the closet exists.
    * ``console_pane_alive`` — True if the console window's pane
      is alive (False = dead shell).
    * ``rail_pane_alive`` — True if the rail pane is alive.
    * ``rail_pane_running_non_shell`` — True if the rail pane is
      currently running a non-shell command (e.g., a TUI). The
      state machine will refuse to respawn a live non-shell
      rail (#841 segfault path).
    * ``current_tmux_session`` — the user's *current* tmux
      session name (None when outside tmux).
    * ``inside_unrelated_tmux`` — True iff
      ``current_tmux_session`` is set and is neither
      ``main_session_name`` nor ``closet_session_name``.
    * ``upgrade_marker_present`` — True if a previous
      ``pm upgrade`` left a restart marker.
    * ``persisted_sessions`` — names of session rows the state
      store believes exist. Used by
      :func:`reconcile_session_inventory`.
    * ``tmux_windows`` — names of every live tmux window across
      both sessions. Used by
      :func:`reconcile_session_inventory`.
    """

    main_session_name: str
    closet_session_name: str
    main_session_alive: bool
    closet_session_alive: bool
    console_pane_alive: bool
    rail_pane_alive: bool
    rail_pane_running_non_shell: bool
    current_tmux_session: str | None
    upgrade_marker_present: bool = False
    persisted_sessions: frozenset[str] = field(default_factory=frozenset)
    tmux_windows: frozenset[str] = field(default_factory=frozenset)

    @property
    def context(self) -> LaunchContext:
        """Classify the launch context from the probe."""
        if self.current_tmux_session is None:
            return LaunchContext.OUTSIDE_TMUX
        if self.current_tmux_session == self.main_session_name:
            return LaunchContext.INSIDE_POLLY_TMUX_SAME_SESSION
        if self.current_tmux_session == self.closet_session_name:
            return LaunchContext.INSIDE_POLLY_TMUX_DIFFERENT_SESSION
        return LaunchContext.INSIDE_UNRELATED_TMUX


@dataclass(frozen=True, slots=True)
class LaunchPlan:
    """The result of :func:`plan_launch`.

    Fields:

    * ``state`` — high-level :class:`LaunchState`.
    * ``context`` — derived :class:`LaunchContext`.
    * ``actions`` — ordered tuple of :class:`LaunchAction` to
      execute.
    * ``reason`` — short human-readable description (used for
      log lines and the fail-closed user message).
    """

    state: LaunchState
    context: LaunchContext
    actions: tuple[LaunchAction, ...]
    reason: str


# ---------------------------------------------------------------------------
# Inventory disagreement type
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InventoryDisagreement:
    """One disagreement between persisted state and live tmux.

    The audit cites #871: the cockpit session inventory reported
    zero sessions while tmux had live worker/architect/reviewer/
    operator/heartbeat windows. The reconcile helper surfaces
    each disagreement so the cockpit can render an actionable
    "your sessions table is out of date" warning instead of
    rendering a misleading zero count.
    """

    kind: str  # "missing_in_tmux" | "missing_in_persisted"
    name: str
    detail: str = ""


# ---------------------------------------------------------------------------
# Pure planner
# ---------------------------------------------------------------------------


def plan_launch(probe: LaunchProbe) -> LaunchPlan:
    """Return the canonical :class:`LaunchPlan` for ``probe``.

    Pure, total, side-effect free. Every state in
    :class:`LaunchState` is reachable; tests cover each one.
    """
    context = probe.context

    # ------------------------------------------------------------------
    # UNSUPPORTED: short-circuit for combinations we refuse to handle.
    # ------------------------------------------------------------------
    #
    # The genuinely unsupported case is a probe whose own
    # session names are empty — that means the config did not
    # supply the names the launcher needs, and any further action
    # would mutate ambient tmux state speculatively. Refuse with
    # an actionable message instead.
    #
    # ``tmux switch-client`` is safe across unrelated tmux
    # sessions, so being inside an unrelated tmux while Polly is
    # alive is *not* unsupported — the existing launcher
    # successfully calls switch_client in that case. The audit's
    # "fails closed when state is unsupported" rule is about
    # avoiding speculative pane mutation, not about refusing
    # legitimate switch-client attaches.
    if not probe.main_session_name or not probe.closet_session_name:
        return LaunchPlan(
            state=LaunchState.UNSUPPORTED,
            context=context,
            actions=(LaunchAction.FAIL_CLOSED,),
            reason=(
                "Tmux session names missing from config — set "
                "`project.tmux_session` in pollypm.toml and re-run "
                "`pm up`."
            ),
        )

    # ------------------------------------------------------------------
    # UPGRADE_RESTART: explicit post-upgrade marker takes precedence.
    # ------------------------------------------------------------------
    if probe.upgrade_marker_present and probe.main_session_alive:
        return LaunchPlan(
            state=LaunchState.UPGRADE_RESTART,
            context=context,
            actions=(
                LaunchAction.ENSURE_CONSOLE_WINDOW,
                LaunchAction.SCHEDULE_HEARTBEAT,
                LaunchAction.START_RAIL_DAEMON,
                _attach_action(context),
            ),
            reason="post-upgrade restart: reconnect to the existing session",
        )

    # ------------------------------------------------------------------
    # FIRST_LAUNCH: no main, no closet.
    # ------------------------------------------------------------------
    if not probe.main_session_alive and not probe.closet_session_alive:
        return LaunchPlan(
            state=LaunchState.FIRST_LAUNCH,
            context=context,
            actions=(
                LaunchAction.ENSURE_STORAGE_CLOSET,
                LaunchAction.BOOTSTRAP_LAUNCHES,
                LaunchAction.ENSURE_MAIN_SESSION,
                LaunchAction.SCHEDULE_HEARTBEAT,
                LaunchAction.START_RAIL_DAEMON,
                _attach_action(context),
            ),
            reason="first launch: clean bootstrap",
        )

    # ------------------------------------------------------------------
    # RESTORE_FROM_CLOSET: closet alive, main session gone.
    # ------------------------------------------------------------------
    if not probe.main_session_alive and probe.closet_session_alive:
        # #1098 — the closet may be "alive" but stripped down to just
        # ``pm-heartbeat`` (after ``pm reset --force`` killed both
        # sessions and a stray heartbeat tick re-created the closet
        # before ``pm up`` ran). Including BOOTSTRAP_LAUNCHES routes
        # through ``Supervisor.bootstrap_tmux`` → ``_reconcile_existing``
        # which is idempotent: it skips windows that already exist
        # and bulk-spawns the missing storage-closet windows
        # (architect/reviewer/operator/etc.) in a single ``pm up``
        # invocation. Reconcile also recreates the main session on
        # its own, so ENSURE_MAIN_SESSION is no longer required —
        # but we keep it for defensiveness and because its handler
        # is a no-op once the session exists.
        return LaunchPlan(
            state=LaunchState.RESTORE_FROM_CLOSET,
            context=context,
            actions=(
                LaunchAction.BOOTSTRAP_LAUNCHES,
                LaunchAction.ENSURE_MAIN_SESSION,
                LaunchAction.SCHEDULE_HEARTBEAT,
                LaunchAction.START_RAIL_DAEMON,
                _attach_action(context),
            ),
            reason=(
                "main session vanished but storage closet is alive — "
                "reconcile missing windows and rebuild the cockpit"
            ),
        )

    # ------------------------------------------------------------------
    # RECOVER_MISSING_CLOSET: main session alive, closet gone.
    # ------------------------------------------------------------------
    if probe.main_session_alive and not probe.closet_session_alive:
        return LaunchPlan(
            state=LaunchState.RECOVER_MISSING_CLOSET,
            context=context,
            actions=(
                LaunchAction.ENSURE_STORAGE_CLOSET,
                LaunchAction.BOOTSTRAP_LAUNCHES,
                LaunchAction.SCHEDULE_HEARTBEAT,
                _attach_action(context),
            ),
            reason="storage closet missing — rebuild and reconcile",
        )

    # ------------------------------------------------------------------
    # RECOVER_DEAD_SHELL: console pane is dead.
    # ------------------------------------------------------------------
    if probe.main_session_alive and not probe.console_pane_alive:
        return LaunchPlan(
            state=LaunchState.RECOVER_DEAD_SHELL,
            context=context,
            actions=(
                LaunchAction.RESPAWN_SHELL,
                _attach_action(context),
            ),
            reason="console shell pane is dead — respawn the shell only",
        )

    # ------------------------------------------------------------------
    # RECOVER_DEAD_RAIL: rail pane dead or dropped back to a shell.
    # ------------------------------------------------------------------
    if (
        probe.main_session_alive
        and probe.console_pane_alive
        and (
            not probe.rail_pane_alive
            or not probe.rail_pane_running_non_shell
        )
    ):
        # #841: never respawn a *live* non-shell rail. The dead-
        # rail branch only fires when the rail is genuinely dead or
        # has fallen back to an interactive shell.
        return LaunchPlan(
            state=LaunchState.RECOVER_DEAD_RAIL,
            context=context,
            actions=(
                LaunchAction.RESPAWN_RAIL,
                _attach_action(context),
            ),
            reason="rail pane is not running the cockpit — respawn the rail",
        )

    # ------------------------------------------------------------------
    # RECOVER_DEAD_RAIL refuse: rail is live and running a non-shell.
    # The audit cites the segfault path explicitly. Normal attach
    # must NEVER respawn a live non-shell rail pane.
    # ------------------------------------------------------------------
    if (
        probe.main_session_alive
        and probe.rail_pane_alive
        and probe.rail_pane_running_non_shell
    ):
        # This is the happy path — ATTACH_EXISTING.
        return LaunchPlan(
            state=LaunchState.ATTACH_EXISTING,
            context=context,
            # SCHEDULE_HEARTBEAT is included even on attach: the
            # heartbeat schedule is idempotent, and a session
            # that has been attached and detached repeatedly may
            # have lost its scheduled tick. Cheap to re-arm.
            actions=(
                LaunchAction.ENSURE_CONSOLE_WINDOW,
                LaunchAction.SCHEDULE_HEARTBEAT,
                _attach_action(context),
            ),
            reason="cockpit healthy: attach without respawning the rail",
        )

    # ------------------------------------------------------------------
    # ATTACH_EXISTING: everything healthy.
    # ------------------------------------------------------------------
    return LaunchPlan(
        state=LaunchState.ATTACH_EXISTING,
        context=context,
        actions=(
            LaunchAction.ENSURE_CONSOLE_WINDOW,
            LaunchAction.SCHEDULE_HEARTBEAT,
            _attach_action(context),
        ),
        reason="cockpit healthy: attach existing session",
    )


def _attach_action(context: LaunchContext) -> LaunchAction:
    """Pick the right attach primitive for ``context``."""
    if context is LaunchContext.OUTSIDE_TMUX:
        return LaunchAction.ATTACH_SESSION
    if context is LaunchContext.INSIDE_POLLY_TMUX_SAME_SESSION:
        return LaunchAction.FOCUS_CONSOLE
    if context is LaunchContext.INSIDE_POLLY_TMUX_DIFFERENT_SESSION:
        return LaunchAction.SWITCH_CLIENT
    # INSIDE_UNRELATED_TMUX without main alive: switch.
    return LaunchAction.SWITCH_CLIENT


# ---------------------------------------------------------------------------
# Inventory reconciliation (#871)
# ---------------------------------------------------------------------------


def reconcile_session_inventory(
    *,
    persisted: Iterable[str],
    live: Iterable[str],
) -> tuple[InventoryDisagreement, ...]:
    """Compare two session-name sets and return disagreements.

    The audit (#871) cites the canonical bug: the cockpit
    inventory reported zero live sessions while tmux had five
    live windows. The bug was a stale/partial sessions table.
    The fix is to reconcile two readings (persisted and tmux)
    every time the cockpit mounts and surface every disagreement.

    Each :class:`InventoryDisagreement` tags ``kind`` as either
    ``"missing_in_tmux"`` (persisted but no live window) or
    ``"missing_in_persisted"`` (live window but no persisted
    row). The cockpit renders the count of disagreements and a
    drill-in for the names.
    """
    persisted_set = set(persisted)
    live_set = set(live)

    out: list[InventoryDisagreement] = []
    for name in sorted(persisted_set - live_set):
        out.append(
            InventoryDisagreement(
                kind="missing_in_tmux",
                name=name,
                detail=(
                    "persisted as live in the sessions table but no "
                    "matching tmux window"
                ),
            )
        )
    for name in sorted(live_set - persisted_set):
        out.append(
            InventoryDisagreement(
                kind="missing_in_persisted",
                name=name,
                detail=(
                    "live tmux window has no persisted session row — "
                    "session inventory drifted"
                ),
            )
        )
    return tuple(out)
