"""CLI commands for tier-4 cascade authority (#1553).

Two command groups land here:

* ``pm tier4 self-promote`` — the self-promotion API. Tier-3 Polly
  invokes this when she's exhausted normal-mode options on a finding.
  Audit-logs ``audit.tier4_promoted`` with ``promotion_path: "self"``.
  Cooldown is enforced against the same root_cause_hash via the
  tier-4 tracker.

* ``pm tier4 status`` — diagnostic; lists active tier-4 rows.

* ``pm system restart-heartbeat|restart-cockpit|restart-rail-daemon``
  — system-scoped recovery helpers, gated behind a ``--tier4`` flag
  (or the ``POLLYPM_TIER4_AUTHORITY=1`` env var) so tier-3 Polly can't
  invoke them accidentally. Each helper emits
  ``audit.tier4_global_action`` AND fires a desktop notification via
  the existing notifier plumbing.

Contract:
- Inputs: Typer arguments / options.
- Outputs: command registrations on a passed Typer app + two sub-apps
  (tier4_app, system_app).
- Side effects: tracker mutations, audit-log writes, tmux session
  bounces, desktop notifications.
- Invariants: every system-scoped action under tier-4 authority pairs
  the audit row with a desktop notification.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer


logger = logging.getLogger(__name__)


tier4_app = typer.Typer(
    help=(
        "Tier-4 cascade controls (broader-authority Polly + budget). "
        "See `pm tier4 self-promote --help` and `pm tier4 status`."
    ),
    no_args_is_help=True,
)

system_app = typer.Typer(
    help=(
        "System-service recovery helpers. Gated behind --tier4 (or "
        "POLLYPM_TIER4_AUTHORITY=1) because they bounce shared "
        "infrastructure and emit a `audit.tier4_global_action` row."
    ),
    no_args_is_help=True,
)


_TIER4_ENV_FLAG = "POLLYPM_TIER4_AUTHORITY"


def _require_tier4(use_flag: bool) -> None:
    """Refuse system-scoped helpers unless the caller has tier-4 authority.

    The env var is the production carrier (heartbeat / cadence handler
    sets it on tier-4 invocation); the ``--tier4`` flag is the manual
    override for operators / debug shells. One has to be set or the
    helper aborts with a non-zero exit code.
    """
    env_set = os.environ.get(_TIER4_ENV_FLAG, "").strip().lower() in {
        "1", "true", "yes", "on",
    }
    if not (use_flag or env_set):
        typer.echo(
            "refused: system-scoped helpers require --tier4 (or "
            f"{_TIER4_ENV_FLAG}=1 in the environment). This gate "
            "exists so tier-3 Polly cannot invoke system bounces "
            "accidentally.",
            err=True,
        )
        raise typer.Exit(code=2)


def _build_tracker() -> Any | None:
    """Return a Tier4PromotionTracker bound to the workspace state.db."""
    try:
        from pollypm.audit.tier4 import Tier4PromotionTracker
        from pollypm.work.cli import _resolve_db_path
        db_path = _resolve_db_path(".pollypm/state.db", project=None)
    except Exception:  # noqa: BLE001
        logger.debug("tier4 cli: tracker construction failed", exc_info=True)
        return None
    return Tier4PromotionTracker(db_path)


def _resolve_finding_for_self_promote(
    *,
    rule: str,
    project: str,
    subject: str,
    evidence_json: str,
) -> Any:
    """Reconstruct a Finding-shaped object from CLI args.

    Self-promotion takes the finding's identifying fields rather than
    a Finding object directly because the CLI is invoked out-of-band.
    The reconstructed Finding has empty tier / metadata — those don't
    affect the root_cause_hash.
    """
    import json
    from pollypm.audit.watchdog import Finding, TIER_3

    try:
        evidence = json.loads(evidence_json) if evidence_json else {}
    except (TypeError, ValueError) as exc:
        raise typer.BadParameter(
            f"--evidence-json must be valid JSON: {exc}"
        ) from exc
    if not isinstance(evidence, dict):
        raise typer.BadParameter(
            "--evidence-json must decode to a JSON object"
        )
    return Finding(
        rule=rule,
        project=project,
        subject=subject,
        tier=TIER_3,
        evidence=evidence,
    )


@tier4_app.command(
    "self-promote",
    help=(
        "Tier-3 Polly self-promote API. Records the promotion in the "
        "tier-4 tracker, emits `audit.tier4_promoted` with "
        "promotion_path=self, and (unless --dry-run) invokes the "
        "tier-4 dispatch path. Cooldown of 6h per root_cause_hash; a "
        "second self-promote for the same hash inside the cooldown is "
        "refused."
    ),
)
def self_promote(
    rule: str = typer.Option(..., "--rule", help="Watchdog rule name (e.g. queue_without_motion)."),
    project: str = typer.Option(..., "--project", help="Project key."),
    subject: str = typer.Option(..., "--subject", help="Finding subject (typically project/N)."),
    evidence_json: str = typer.Option(
        "{}",
        "--evidence-json",
        help=(
            "JSON-encoded evidence dict, used to derive the "
            "root_cause_hash. Passing the same evidence shape as the "
            "tier-3 Finding ensures the cooldown is keyed correctly."
        ),
    ),
    justification: str = typer.Option(
        ...,
        "--justification",
        help=(
            "Why normal-mode options are exhausted and what tier-4 "
            "action is being attempted. Required — the audit row is "
            "load-bearing."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run/--no-dry-run",
        help=(
            "When set, evaluate the cooldown gate and print the "
            "resulting root_cause_hash without recording the "
            "promotion or invoking dispatch."
        ),
    ),
) -> None:
    if not justification.strip():
        typer.echo(
            "refused: --justification is required and must be non-empty.",
            err=True,
        )
        raise typer.Exit(code=2)

    finding = _resolve_finding_for_self_promote(
        rule=rule,
        project=project,
        subject=subject,
        evidence_json=evidence_json,
    )
    tracker = _build_tracker()
    if tracker is None:
        typer.echo(
            "refused: tier-4 tracker unavailable (workspace state.db "
            "could not be resolved).",
            err=True,
        )
        raise typer.Exit(code=3)
    now = datetime.now(UTC)
    allowed, reason = tracker.can_self_promote(finding, now=now)
    from pollypm.audit.tier4 import root_cause_hash
    rch = root_cause_hash(finding)
    if not allowed:
        typer.echo(
            f"refused: self-promote cooldown active for "
            f"root_cause_hash={rch}: {reason}.",
            err=True,
        )
        raise typer.Exit(code=4)
    if dry_run:
        typer.echo(
            f"ok (dry-run): would self-promote root_cause_hash={rch} "
            f"({reason})."
        )
        return

    from pollypm.plugins_builtin.core_recurring.audit_watchdog import (
        _dispatch_to_operator_tier4,
    )
    outcome = _dispatch_to_operator_tier4(
        finding,
        project_path=None,
        now=now,
        promotion_path="self",
        justification=justification,
        tracker=tracker,
    )
    typer.echo(
        f"self-promote outcome={outcome} root_cause_hash={rch}"
    )
    if outcome == "send_failed":
        raise typer.Exit(code=5)


@tier4_app.command(
    "status",
    help="Print active tier-4 rows from the promotion tracker.",
)
def status() -> None:
    tracker = _build_tracker()
    if tracker is None:
        typer.echo("tracker unavailable.")
        raise typer.Exit(code=1)
    rows = tracker.list_active()
    if not rows:
        typer.echo("(no active tier-4 rows)")
        return
    now = datetime.now(UTC)
    for row in rows:
        remaining = tracker.budget_remaining_seconds(
            row.root_cause_hash, now=now,
        )
        remaining_str = (
            f"{remaining}s" if remaining is not None else "n/a"
        )
        typer.echo(
            f"{row.root_cause_hash}  project={row.project}  rule={row.rule}  "
            f"path={row.promotion_path}  entered_at={row.tier4_entered_at}  "
            f"remaining={remaining_str}"
        )


@tier4_app.command(
    "clear",
    help=(
        "Manually clear a tier-4 row (the finding resolved). Emits "
        "`audit.tier4_demoted` and flips tier4_active=0."
    ),
)
def clear(
    root_cause_hash_value: str = typer.Argument(
        ..., metavar="ROOT_CAUSE_HASH",
        help="The 16-char hash printed by `pm tier4 status`.",
    ),
    reason: str = typer.Option(
        "manual_clear", "--reason",
        help="Reason recorded in `audit.tier4_demoted`.",
    ),
) -> None:
    tracker = _build_tracker()
    if tracker is None:
        raise typer.Exit(code=1)
    state = tracker.get(root_cause_hash_value)
    if state is None or not state.tier4_active:
        typer.echo(f"no active tier-4 row for {root_cause_hash_value}.")
        raise typer.Exit(code=2)
    now = datetime.now(UTC)
    cleared = tracker.clear(root_cause_hash_value, now=now, reason=reason)
    if cleared:
        from pollypm.plugins_builtin.core_recurring.audit_watchdog import (
            _emit_tier4_demoted,
        )
        _emit_tier4_demoted(
            project=state.project,
            root_cause_hash_value=root_cause_hash_value,
            reason=reason,
        )
        typer.echo(f"cleared {root_cause_hash_value} ({reason}).")
    else:
        typer.echo(f"clear no-op for {root_cause_hash_value}.")


# ---------------------------------------------------------------------------
# pm system — system-scoped recovery helpers.
# ---------------------------------------------------------------------------


def _resolve_storage_closet_session() -> str | None:
    """Return the heartbeat / storage-closet tmux session name."""
    try:
        from pollypm.config import DEFAULT_CONFIG_PATH, load_config
        cfg = load_config(DEFAULT_CONFIG_PATH)
        # Mirror Supervisor.storage_closet_session_name() — the session
        # is ``<tmux_session>-storage`` per the constant in supervisor.py.
        base = getattr(cfg.project, "tmux_session", "polly")
        # Defensive: read from the runtime services if available.
        return f"{base}-storage"
    except Exception:  # noqa: BLE001
        return None


def _tmux_kill_session(session_name: str) -> bool:
    """Run ``tmux kill-session -t <name>``. Returns True iff exit code 0."""
    if not shutil.which("tmux"):
        logger.warning(
            "system: tmux not on PATH; cannot kill session %s", session_name,
        )
        return False
    try:
        proc = subprocess.run(
            ["tmux", "kill-session", "-t", session_name],
            check=False, capture_output=True, timeout=5.0,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "system: tmux kill-session failed for %s",
            session_name, exc_info=True,
        )
        return False
    return proc.returncode == 0


def _system_action(
    *,
    action_name: str,
    root_cause_hash_value: str,
    bounce_fn,
) -> int:
    """Execute a system-scoped restart action under tier-4 authority.

    Wraps the bounce in:
    1. ``audit.tier4_global_action`` emit (with the louder push notification).
    2. The bounce itself (caller-supplied).
    3. Status-coded exit.

    Returns 0 on success, 1 on bounce failure.
    """
    from pollypm.plugins_builtin.core_recurring.audit_watchdog import (
        emit_tier4_global_action,
    )
    push_delivered = emit_tier4_global_action(
        action=action_name,
        root_cause_hash_value=root_cause_hash_value,
        actor="polly",
        push_title=f"PollyPM tier-4: {action_name}",
        push_body=(
            f"Tier-4 Polly bounced {action_name}. "
            f"hash={root_cause_hash_value}."
        ),
    )
    ok = bool(bounce_fn())
    typer.echo(
        f"action={action_name} ok={ok} push_delivered={push_delivered} "
        f"hash={root_cause_hash_value}"
    )
    return 0 if ok else 1


@system_app.command(
    "restart-heartbeat",
    help=(
        "Tier-4: bounce the heartbeat tmux session. Emits "
        "`audit.tier4_global_action` and a desktop notification."
    ),
)
def restart_heartbeat(
    tier4: bool = typer.Option(
        False, "--tier4/--no-tier4",
        help="Confirm tier-4 authority (or set POLLYPM_TIER4_AUTHORITY=1).",
    ),
    root_cause_hash_value: str = typer.Option(
        "manual",
        "--root-cause-hash",
        help="The hash this action resolves under.",
    ),
) -> None:
    _require_tier4(tier4)
    session = _resolve_storage_closet_session()
    if session is None:
        typer.echo("refused: could not resolve heartbeat session.", err=True)
        raise typer.Exit(code=3)
    code = _system_action(
        action_name="restart-heartbeat",
        root_cause_hash_value=root_cause_hash_value,
        bounce_fn=lambda: _tmux_kill_session(session),
    )
    if code != 0:
        raise typer.Exit(code=code)


@system_app.command(
    "restart-cockpit",
    help=(
        "Tier-4: bounce the cockpit tmux session. Emits "
        "`audit.tier4_global_action` and a desktop notification."
    ),
)
def restart_cockpit(
    tier4: bool = typer.Option(False, "--tier4/--no-tier4"),
    root_cause_hash_value: str = typer.Option(
        "manual", "--root-cause-hash",
    ),
) -> None:
    _require_tier4(tier4)
    try:
        from pollypm.config import DEFAULT_CONFIG_PATH, load_config
        cfg = load_config(DEFAULT_CONFIG_PATH)
        session = getattr(cfg.project, "tmux_session", "polly")
    except Exception:  # noqa: BLE001
        session = "polly"
    code = _system_action(
        action_name="restart-cockpit",
        root_cause_hash_value=root_cause_hash_value,
        bounce_fn=lambda: _tmux_kill_session(session),
    )
    if code != 0:
        raise typer.Exit(code=code)


@system_app.command(
    "restart-rail-daemon",
    help=(
        "Tier-4: bounce the rail daemon. Emits "
        "`audit.tier4_global_action` and a desktop notification."
    ),
)
def restart_rail_daemon(
    tier4: bool = typer.Option(False, "--tier4/--no-tier4"),
    root_cause_hash_value: str = typer.Option(
        "manual", "--root-cause-hash",
    ),
) -> None:
    _require_tier4(tier4)
    # The rail daemon doesn't run in a separate tmux session — it's a
    # subprocess of the cockpit. The recovery is to flip the cockpit
    # supervisor's run-flag, which the existing cockpit launcher
    # honours on next mount. Until the supervisor exposes a clean
    # restart hook we shell out via `pm rail restart` if available;
    # otherwise we no-op the bounce and rely on the audit row + push.
    def _bounce() -> bool:
        if not shutil.which("pm"):
            return False
        try:
            proc = subprocess.run(
                ["pm", "rail", "restart"],
                check=False, capture_output=True, timeout=5.0,
            )
        except Exception:  # noqa: BLE001
            return False
        return proc.returncode == 0

    code = _system_action(
        action_name="restart-rail-daemon",
        root_cause_hash_value=root_cause_hash_value,
        bounce_fn=_bounce,
    )
    if code != 0:
        # Soft-failure: the audit row + push still landed; we just
        # couldn't bounce the daemon ourselves. Sam can take it from
        # there — exit 1 surfaces that to the caller.
        raise typer.Exit(code=code)


def register_tier4_commands(app: typer.Typer) -> None:
    """Mount the tier-4 + system command groups on the root app."""
    app.add_typer(tier4_app, name="tier4")
    app.add_typer(system_app, name="system")


__all__ = [
    "register_tier4_commands",
    "tier4_app",
    "system_app",
]
