"""``pm upgrade`` — detect the install method, delegate to the right
package manager, and wire the migration / notice-injection hooks (#716).

Design goals:

* **One command everywhere.** `pm upgrade` works whether the user
  installed via ``uv tool install``, ``pip``, ``brew``, or ``npm -g``.
  The wrapper detects which (in priority order) and shells out.
* **Safe-by-default.** Before any mutation, the migration check from
  #717 runs. On failure, upgrade aborts with a specific error — the
  user is never left on a half-upgraded install.
* **Observable.** Each step streams to stdout with a ``[step]`` prefix
  so a log / pane can show progress. The caller (rail one-click in
  #719) reads these markers.
* **No new dependencies.** Detection uses ``shutil.which`` and
  ``subprocess.run``; no external libraries.

The dependent issues (#717 migration gate, #718 notice injection) are
invoked via import-if-available so this module works on a system where
either hasn't landed yet — we log "skipped: not yet implemented" and
carry on. Once those merge, the stubs become real calls without
touching this module.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pollypm


_POST_UPGRADE_FLAG = Path.home() / ".pollypm" / "post-upgrade.flag"


def _write_post_upgrade_flag(
    old_version: str,
    new_version: str,
    *,
    notified: int = 0,
    recycled: int = 0,
    pending_restart: int = 0,
    recycle_scope: str | None = None,
) -> None:
    """Drop the cockpit's post-upgrade summary sentinel.

    The cockpit's ``_check_post_upgrade_flag`` reads this file each
    tick and swaps the rail's update pill to a summary like
    ``Upgraded v1.2.0 → v1.3.2 · 3 sessions notified · 2 pending
    restart · 5 idle recycled``. The richer payload (#720) replaces
    the prior versions-only shape so the user sees exactly what
    happened — what was notified in-conversation, what was hard-
    recycled, and what's still running on the old code.

    Best-effort: filesystem errors are swallowed so a write failure
    doesn't break the otherwise-successful upgrade flow.
    """
    payload = {
        "from": old_version,
        "to": new_version,
        "at": time.time(),
        # #720 — counts the rail summary pill renders.
        "notified": int(notified),
        "recycled": int(recycled),
        "pending_restart": int(pending_restart),
        "recycle_scope": recycle_scope,
    }
    try:
        _POST_UPGRADE_FLAG.parent.mkdir(parents=True, exist_ok=True)
        _POST_UPGRADE_FLAG.write_text(json.dumps(payload))
    except OSError:
        pass


Installer = str  # "uv" | "pip" | "brew" | "npm" | "unknown"


@dataclass(slots=True)
class UpgradePlan:
    """Resolved upgrade strategy for a specific install method.

    ``command`` is the exact argv that will be run; ``notes`` is a
    user-visible explanation of what's happening (shown in the pane).
    """

    installer: Installer
    command: list[str]
    channel: str
    notes: str


@dataclass(slots=True)
class UpgradeResult:
    ok: bool
    installer: Installer
    old_version: str
    new_version: str
    migration_checked: bool
    notified: bool
    stdout: str
    stderr: str
    message: str
    # #720 — counts surfaced on the rail summary pill so the user
    # sees what happened in the upgrade. ``notified_count`` is the
    # number of live sessions that received the in-conversation
    # notice (#718), ``recycled_count`` is the number explicitly
    # killed and respawned by ``--recycle-all`` / ``--recycle-idle``,
    # ``pending_restart_count`` is sessions that received the notice
    # but haven't turned over since (so they're still on the old
    # in-memory prompt). ``None`` means the count wasn't sampled —
    # e.g. failed upgrade aborted before any of the post-install
    # steps ran.
    notified_count: int | None = None
    recycled_count: int | None = None
    pending_restart_count: int | None = None
    recycle_scope: str | None = None


class Step:
    """Tiny stdout-marker helper so the rail pane can tail progress."""

    def __init__(self, emit: Callable[[str], None]) -> None:
        self._emit = emit

    def __call__(self, text: str) -> None:
        self._emit(f"[step] {text}")


def _available_upgrade(channel: str) -> tuple[str, bool] | None:
    """Return ``(latest_version, upgrade_available)`` when the release check works.

    A quiet ``None`` keeps ``pm upgrade`` usable offline or before release
    metadata exists. In that case the installer remains the source of truth.
    """
    try:
        from pollypm.release_check import check_latest
    except ImportError:
        return None
    try:
        check = check_latest(channel, force_refresh=True)
    except Exception:  # noqa: BLE001
        return None
    if check is None:
        return None
    return (check.latest, check.upgrade_available)


def _runs_ok(cmd: list[str], *, timeout: float = 5.0) -> bool:
    """True iff ``cmd`` exits 0 within the timeout."""
    try:
        result = subprocess.run(
            cmd, check=False, capture_output=True, text=True, timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _uv_installed() -> bool:
    return _runs_ok(["uv", "tool", "list"]) and _uv_has_pollypm()


def _uv_has_pollypm() -> bool:
    try:
        result = subprocess.run(
            ["uv", "tool", "list"],
            check=False, capture_output=True, text=True, timeout=5.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    return "pollypm" in result.stdout


def _pip_has_pollypm() -> bool:
    return _runs_ok(["pip", "show", "pollypm"], timeout=3.0)


def _brew_has_pollypm() -> bool:
    return _runs_ok(["brew", "list", "pollypm"], timeout=3.0)


def _npm_has_pollypm() -> bool:
    try:
        result = subprocess.run(
            ["npm", "list", "-g", "--depth=0"],
            check=False, capture_output=True, text=True, timeout=5.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    return "pollypm" in result.stdout


def detect_installer(
    *, overrides: dict[str, bool] | None = None,
) -> Installer:
    """Return the name of the package manager that installed PollyPM.

    Priority order: uv → pip → brew → npm → unknown. The ``overrides``
    kwarg is a test seam: pass ``{"uv": True, "pip": False, ...}`` to
    skip the real shell probes.
    """
    if overrides is None:
        overrides = {}

    def probe(name: str, real: Callable[[], bool]) -> bool:
        if name in overrides:
            return overrides[name]
        return real()

    if probe("uv", _uv_installed):
        return "uv"
    if probe("pip", _pip_has_pollypm):
        return "pip"
    if probe("brew", _brew_has_pollypm):
        return "brew"
    if probe("npm", _npm_has_pollypm):
        return "npm"
    return "unknown"


def plan_upgrade(installer: Installer, channel: str) -> UpgradePlan:
    """Produce the exact argv + notes for a given installer + channel.

    ``unknown`` returns a plan whose ``command`` is empty — the CLI
    must treat that as an error, not execute it.
    """
    if channel not in {"stable", "beta"}:
        channel = "stable"
    if installer == "uv":
        if channel == "beta":
            cmd = [
                "uv", "tool", "install", "--reinstall", "--prerelease", "allow",
                "pollypm",
            ]
            notes = "uv tool install --reinstall --prerelease allow pollypm (beta)"
        else:
            cmd = ["uv", "tool", "upgrade", "pollypm"]
            notes = "uv tool upgrade pollypm (stable)"
        return UpgradePlan(installer, cmd, channel, notes)
    if installer == "pip":
        if channel == "beta":
            cmd = ["pip", "install", "-U", "--pre", "pollypm"]
            notes = "pip install -U --pre pollypm (beta)"
        else:
            cmd = ["pip", "install", "-U", "pollypm"]
            notes = "pip install -U pollypm (stable)"
        return UpgradePlan(installer, cmd, channel, notes)
    if installer == "brew":
        # brew doesn't distinguish stable/beta on this channel; beta is
        # no-op and we warn the caller.
        cmd = ["brew", "upgrade", "pollypm"]
        notes = (
            "brew upgrade pollypm — brew does not expose pre-release "
            "channels; stable-only."
        )
        return UpgradePlan(installer, cmd, "stable", notes)
    if installer == "npm":
        if channel == "beta":
            cmd = ["npm", "install", "-g", "pollypm@beta"]
            notes = "npm install -g pollypm@beta"
        else:
            cmd = ["npm", "update", "-g", "pollypm"]
            notes = "npm update -g pollypm"
        return UpgradePlan(installer, cmd, channel, notes)
    return UpgradePlan("unknown", [], channel, "no supported installer detected")


def unsupported_installer_help() -> str:
    """User-facing text shown when no installer is detected.

    Uses the StructuredUserMessage shape (#760). The four per-tool
    commands populate ``suggested_actions`` so the renderer emits a
    labeled ``Options:`` block — one entry per supported installer
    with the copy-pasteable command underneath. The pre-release note
    stays in ``details`` because it's follow-up info, not a primary
    action.
    """
    from pollypm.structured_message import StructuredUserMessage

    msg = StructuredUserMessage(
        summary="Could not detect how PollyPM was installed.",
        why=(
            "pm upgrade needs to know which package manager placed "
            "pollypm on your PATH so it can hand off the upgrade to "
            "the right tool. Your system doesn't show any of the "
            "four we know about (uv, pip, brew, npm)."
        ),
        next_action="Run the command matching how you installed pollypm.",
        suggested_actions=(
            ("uv", "uv tool upgrade pollypm"),
            ("pip", "pip install -U pollypm"),
            ("brew", "brew upgrade pollypm"),
            ("npm", "npm update -g pollypm"),
        ),
        details=(
            "For pre-release builds, add --pre (pip) / --prerelease "
            "allow (uv) / @beta (npm)."
        ),
    )
    return msg.render_cli(show_details=True)


def run_migration_check() -> tuple[bool, str]:
    """Run ``pm migrate --check`` if the migration gate (#717) is
    present. Returns ``(ok, message)``.

    Until #717 lands, this is a no-op that returns ``(True, "skipped:
    migration gate not yet implemented")``. Once the gate ships, this
    function will delegate to its entry point.
    """
    try:
        from pollypm.store import migrations as _migrations  # noqa: F401
    except ImportError:
        return (True, "skipped: migration gate not yet implemented (#717)")
    try:
        from pollypm.store.migrations import check_pending
    except ImportError:
        return (True, "skipped: migrations module present but check_pending() absent")
    try:
        ok, detail = check_pending()
    except Exception as exc:  # noqa: BLE001
        return (False, f"migration check raised {type(exc).__name__}: {exc}")
    return (ok, detail or "migration check ok")


def inject_notice(old_version: str, new_version: str) -> tuple[bool, str]:
    """In-session notice injection — disabled in the default flow (#755).

    Modeled as a no-op because the ``<system-update>`` tag, delivered
    as a user-turn message, triggers anti-prompt-injection defenses in
    every live session (both Claude Code and Codex correctly refuse to
    swap roles based on an unverifiable in-channel signal). Post-
    upgrade behavior is now driven by the sentinel flag at
    ``~/.pollypm/post-upgrade.flag`` + the cockpit restart-nudge from
    #719, which are out-of-band signals the user can actually trust.

    The underlying :func:`pollypm.upgrade_notice.inject_system_update_notice`
    helper is still available for explicit opt-in use (e.g. a future
    ``pm upgrade --force-notify`` flag or debug scripts) but is never
    invoked by the default upgrade flow.
    """
    return (True, "skipped: in-channel <system-update> disabled (see #755)")


def _read_new_version() -> str:
    """Return the version string after install.

    ``pollypm.__version__`` is cached in the old Python process — we
    can't just re-read it. Shell out to ``pip show pollypm`` as the
    canonical source. Returns "" on any failure.
    """
    try:
        result = subprocess.run(
            ["pip", "show", "pollypm"],
            check=False, capture_output=True, text=True, timeout=3.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    for line in result.stdout.splitlines():
        if line.lower().startswith("version:"):
            return line.split(":", 1)[1].strip()
    return ""


_DEFAULT_IDLE_THRESHOLD_MINUTES = 30


def _perform_recycle(
    *,
    scope: str,
    idle_threshold_minutes: int = _DEFAULT_IDLE_THRESHOLD_MINUTES,
    step: Callable[[str], None] | None = None,
) -> tuple[int, int]:
    """Recycle live sessions per ``--recycle-all`` / ``--recycle-idle`` (#720).

    Returns ``(recycled_count, skipped_count)``. ``recycled_count``
    is the number of sessions that were torn down and respawned;
    ``skipped_count`` is the number left running (always 0 for
    ``recycle-all``; idle-only scope skips actively-turning sessions).

    Best-effort: when supervisor / config can't be loaded (e.g.
    upgrade ran in a clean install with no config yet), returns
    ``(0, 0)`` so the upgrade flow doesn't crash on the recycle leg.
    """
    log = step or (lambda _msg: None)
    try:
        from pollypm.config import DEFAULT_CONFIG_PATH, load_config
        from pollypm.supervisor import Supervisor
    except Exception:  # noqa: BLE001
        log(f"recycle ({scope}): pollypm runtime imports failed")
        return (0, 0)

    if not DEFAULT_CONFIG_PATH.is_file():
        log(f"recycle ({scope}): no config — nothing to recycle")
        return (0, 0)

    try:
        config = load_config(DEFAULT_CONFIG_PATH)
        supervisor = Supervisor(config)
    except Exception as exc:  # noqa: BLE001
        log(f"recycle ({scope}): supervisor init failed ({exc})")
        return (0, 0)

    recycled = 0
    skipped = 0
    threshold_seconds = idle_threshold_minutes * 60
    now = time.time()

    try:
        launches, _windows, _alerts, _leases, _errors = supervisor.status()
    except Exception as exc:  # noqa: BLE001
        log(f"recycle ({scope}): status snapshot failed ({exc})")
        return (0, 0)

    for launch in launches:
        session_name = getattr(getattr(launch, "session", None), "name", None)
        if not session_name:
            continue
        if scope == "idle":
            # Conservative idle gate: only recycle sessions whose
            # last heartbeat is older than the threshold AND whose
            # runtime row reports "healthy" or "idle". Anything mid-
            # turn or in error state stays running so the user can
            # decide; idle-only is the gentle middle ground.
            try:
                hb_rows = supervisor.store.recent_heartbeats(session_name, limit=1)
                last_hb = hb_rows[0].created_at if hb_rows else None
            except Exception:  # noqa: BLE001
                last_hb = None
            recent_enough = False
            if last_hb is not None:
                try:
                    parsed = _parse_iso_timestamp(last_hb)
                    if parsed is not None and (now - parsed) < threshold_seconds:
                        recent_enough = True
                except Exception:  # noqa: BLE001
                    recent_enough = True  # err on the side of NOT recycling
            else:
                recent_enough = True  # no observed heartbeat → leave alone
            if recent_enough:
                skipped += 1
                continue
        account_name = getattr(getattr(launch, "account", None), "name", None)
        if not account_name:
            skipped += 1
            continue
        try:
            supervisor.restart_session(
                session_name, account_name, failure_type="upgrade_recycle",
            )
            recycled += 1
        except Exception as exc:  # noqa: BLE001
            log(f"recycle ({scope}): {session_name} restart failed ({exc})")
            skipped += 1

    log(f"recycle ({scope}): {recycled} recycled, {skipped} skipped")
    return (recycled, skipped)


def _parse_iso_timestamp(value: str) -> float | None:
    """Best-effort ISO-8601 → epoch seconds for ``_perform_recycle``."""
    if not value:
        return None
    try:
        from datetime import datetime
        # Tolerate the bare-space SQLite shape too.
        normalized = value.replace(" ", "T", 1)
        dt = datetime.fromisoformat(normalized)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def upgrade(
    *,
    channel: str = "stable",
    check_only: bool = False,
    recycle_all: bool = False,
    recycle_idle: bool = False,
    emit: Callable[[str], None] | None = None,
    installer_overrides: dict[str, bool] | None = None,
    plan_only: bool = False,
) -> UpgradeResult:
    """Execute the full upgrade flow and return a structured result.

    The ``plan_only`` flag is for tests — compose the plan, don't run
    it. Production paths never set it.

    ``recycle_all`` / ``recycle_idle`` are wired here so the CLI flags
    are stable, but the actual recycling behavior ships in #720. For
    now they're recorded on the result for the caller to inspect.
    """
    step = Step(emit or print)
    old_version = pollypm.__version__

    installer = detect_installer(overrides=installer_overrides)
    plan = plan_upgrade(installer, channel)
    step(f"installer={installer} channel={plan.channel}")

    if installer == "unknown":
        return UpgradeResult(
            ok=False,
            installer="unknown",
            old_version=old_version,
            new_version=old_version,
            migration_checked=False,
            notified=False,
            stdout="",
            stderr=unsupported_installer_help(),
            message="no supported installer",
        )

    # Migration check runs for both check-only and full upgrade — even
    # check-only users want to know if they'd hit a blocker. Stay silent
    # on the happy path (no pending migration, nothing for the operator
    # to act on); only emit step markers when the check actually
    # surfaced a problem so ``pm upgrade`` doesn't spam migration noise
    # on every run.
    mig_ok, mig_detail = run_migration_check()
    if not mig_ok:
        step("migration check")
        step(f"migration: {mig_detail}")
        return UpgradeResult(
            ok=False,
            installer=installer,
            old_version=old_version,
            new_version=old_version,
            migration_checked=True,
            notified=False,
            stdout="",
            stderr=mig_detail,
            message="migration check failed — upgrade aborted",
        )

    available = _available_upgrade(plan.channel)
    if available is not None:
        latest_version, upgrade_available = available
        if not upgrade_available:
            step(f"already up to date: {old_version}")
            return UpgradeResult(
                ok=True,
                installer=installer,
                old_version=old_version,
                new_version=old_version,
                migration_checked=True,
                notified=False,
                stdout="",
                stderr="",
                message=f"already up to date on {old_version}",
            )
        step(f"latest available: {latest_version}")

    if check_only:
        step(f"check-only: would run `{' '.join(plan.command)}`")
        return UpgradeResult(
            ok=True,
            installer=installer,
            old_version=old_version,
            new_version=old_version,
            migration_checked=True,
            notified=False,
            stdout="",
            stderr="",
            message=f"check-only: {plan.notes}",
        )

    if plan_only:
        return UpgradeResult(
            ok=True,
            installer=installer,
            old_version=old_version,
            new_version=old_version,
            migration_checked=True,
            notified=False,
            stdout="",
            stderr="",
            message=f"plan-only: {plan.notes}",
        )

    step(f"installing: {plan.notes}")
    try:
        result = subprocess.run(
            plan.command,
            check=False, capture_output=True, text=True, timeout=600.0,
        )
    except FileNotFoundError as exc:
        return UpgradeResult(
            ok=False,
            installer=installer,
            old_version=old_version,
            new_version=old_version,
            migration_checked=True,
            notified=False,
            stdout="",
            stderr=str(exc),
            message=f"installer binary not on PATH: {plan.command[0]}",
        )
    except subprocess.TimeoutExpired:
        return UpgradeResult(
            ok=False,
            installer=installer,
            old_version=old_version,
            new_version=old_version,
            migration_checked=True,
            notified=False,
            stdout="",
            stderr="install timed out after 10m",
            message="install timed out — network, or installer hung",
        )
    if result.returncode != 0:
        return UpgradeResult(
            ok=False,
            installer=installer,
            old_version=old_version,
            new_version=old_version,
            migration_checked=True,
            notified=False,
            stdout=result.stdout,
            stderr=result.stderr,
            message=f"install failed (exit {result.returncode})",
        )

    new_version = _read_new_version() or old_version
    step(f"installed {new_version}")
    if new_version == old_version:
        return UpgradeResult(
            ok=True,
            installer=installer,
            old_version=old_version,
            new_version=new_version,
            migration_checked=True,
            notified=False,
            stdout=result.stdout,
            stderr=result.stderr,
            message=f"already up to date on {old_version}",
        )

    step("notifying live sessions")
    notify_ok, notify_detail = inject_notice(old_version, new_version)
    step(f"notify: {notify_detail}")
    notified_count = _notified_session_count() if notify_ok else 0

    recycled_count = 0
    recycle_scope: str | None = None
    if recycle_all or recycle_idle:
        recycle_scope = "all" if recycle_all else "idle"
        recycled_count, _skipped = _perform_recycle(
            scope=recycle_scope, step=step,
        )

    # ``pending_restart_count`` is the number of sessions notified
    # in-conversation that haven't turned over yet (so they're still
    # running on the old in-memory prompt). When ``--recycle-all``
    # was used, every notified session was recycled too — pending
    # is zero. When ``--recycle-idle`` ran, the notified set minus
    # the recycled set is the rough pending count. Otherwise every
    # notified session is "pending" until it turns over on its own.
    if recycle_scope == "all":
        pending_restart_count = 0
    else:
        pending_restart_count = max(0, notified_count - recycled_count)

    _write_post_upgrade_flag(
        old_version, new_version,
        notified=notified_count,
        recycled=recycled_count,
        pending_restart=pending_restart_count,
        recycle_scope=recycle_scope,
    )
    step(
        "flagged cockpit for restart nudge "
        f"(notified={notified_count} recycled={recycled_count} "
        f"pending={pending_restart_count})"
    )

    return UpgradeResult(
        ok=True,
        installer=installer,
        old_version=old_version,
        new_version=new_version,
        migration_checked=True,
        notified=notify_ok,
        stdout=result.stdout,
        stderr=result.stderr,
        message=f"upgraded {old_version} → {new_version}",
        notified_count=notified_count,
        recycled_count=recycled_count,
        pending_restart_count=pending_restart_count,
        recycle_scope=recycle_scope,
    )


def _notified_session_count() -> int:
    """Count live sessions that received the in-conversation notice.

    Best-effort: when no supervisor is available we assume the notice
    didn't reach anyone (returns 0). The cockpit pill renders the
    raw count; an over-conservative zero is preferable to crashing
    the upgrade flow on a count probe.
    """
    try:
        from pollypm.config import DEFAULT_CONFIG_PATH, load_config
        from pollypm.supervisor import Supervisor
    except Exception:  # noqa: BLE001
        return 0
    if not DEFAULT_CONFIG_PATH.is_file():
        return 0
    try:
        config = load_config(DEFAULT_CONFIG_PATH)
        supervisor = Supervisor(config)
        launches, _windows, _alerts, _leases, _errors = supervisor.status()
    except Exception:  # noqa: BLE001
        return 0
    return sum(1 for launch in launches if getattr(getattr(launch, "session", None), "name", None))


def read_changelog_diff(since: str, *, path: Path | None = None) -> str:
    """Return the CHANGELOG.md section(s) above ``since`` version.

    Best-effort scraping — we don't enforce strict changelog format.
    If ``since`` appears as a heading in the file, return everything
    above it (exclusive). Otherwise return an empty string.
    """
    changelog = path or Path.cwd() / "CHANGELOG.md"
    try:
        text = changelog.read_text()
    except (OSError, FileNotFoundError):
        return ""
    lines = text.splitlines()
    cut_at: int | None = None
    for idx, line in enumerate(lines):
        if line.startswith("#") and since in line:
            cut_at = idx
            break
    if cut_at is None:
        return ""
    return "\n".join(lines[:cut_at]).rstrip()
