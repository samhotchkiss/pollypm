"""PollyPM CLI root command composition.

Contract:
- Inputs: top-level CLI arguments/options plus delegated feature-module
  registration hooks.
- Outputs: the root ``Typer`` app and a small set of root-owned command
  handlers that compose the feature modules.
- Side effects: loads config, routes through ``PollyPMService``, shells
  out for user-facing commands, and launches TUI surfaces on demand.
- Invariants: feature command families live in ``pollypm.cli_features``;
  this module owns root composition, shared help text, and only the
  remaining cross-cutting root commands.
- Allowed dependencies: service facade, feature registration modules,
  and public CLI/session-service APIs.
- Private: root-only helper functions and compatibility exports relied on
  by existing tests/entry points.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

import typer

logger = logging.getLogger(__name__)

# Attach the centralized error log so every ``pm`` invocation writes
# WARNING+ records (plus any tracebacks from logger.exception) into
# ``~/.pollypm/errors.log``. Installed at import time — no plugin
# / rail dependency — so a boot-time crash still lands somewhere
# grep-able. Idempotent.
from pollypm.error_log import install as _install_error_log

_install_error_log(process_label="cli")

from pollypm.cli_shortcuts import render_shortcuts_text
from pollypm.config import (
    DEFAULT_CONFIG_PATH,
    resolve_config_path,
    render_example_config,
    write_example_config,
)
from pollypm.cli_help import help_with_examples
from pollypm.cli_features.alerts import alert_app, heartbeat_app, session_app
from pollypm.launch_executor import LaunchPlanExecutor
from pollypm.launch_state import (
    LaunchProbe,
    LaunchState,
    plan_launch,
)
from pollypm.cli_features.issues import issue_app, itsalive_app, report_app
from pollypm.cli_features.dev import dev_app
from pollypm.cli_features.maintenance import debug_app, register_maintenance_commands
from pollypm.cli_features.migrate import register_migrate_commands
from pollypm.cli_features.projects import register_project_commands
from pollypm.cli_features.session_runtime import register_session_runtime_commands
from pollypm.cli_features.ui import register_ui_commands
from pollypm.cli_features.update import register_update_commands
from pollypm.cli_features.upgrade import register_upgrade_commands
from pollypm.cli_features.workers import register_worker_commands


_APP_HELP = help_with_examples(
    "PollyPM CLI.",
    [
        ("pm", "start or attach to the PollyPM tmux session"),
        ("pm add-project ~/dev/my-app", "register a project in the workspace"),
        ('pm send operator "Build a weather CLI"', "hand a request to Polly"),
    ],
    trailing=(
        "Sub-help:  pm task --help, pm session --help, pm project --help, "
        "pm plugins --help.\n"
        "Role guides: pm help worker."
    ),
)

_UP_HELP = help_with_examples(
    "Create or attach to the PollyPM tmux session and boot the cockpit.",
    [
        ("pm up", "start the tmux session or attach if it already exists"),
        (
            "pm up --config ~/.pollypm/pollypm.toml",
            "boot a specific PollyPM config",
        ),
        (
            "pm up --phantom-client",
            "spawn a detached pty client so the cockpit keeps processing input "
            "after you close the terminal (workaround for #1109)",
        ),
    ],
    trailing=(
        "Note (#1109): the cockpit's Textual app stops handling keystrokes when "
        "no tmux client is attached to the session. Keep a terminal attached "
        "(`tmux attach -t pollypm`) for the lifetime of the session, or pass "
        "`--phantom-client` to spawn a background pty so `tmux send-keys` keeps "
        "working when you detach."
    ),
)
_UP_INVOCATION_COMMAND = "pm up"

_STATUS_HELP = help_with_examples(
    "Show configured session state, runtime status, and open-alert counts.",
    [
        ("pm status", "show every configured session"),
        ("pm status operator", "inspect one session by name"),
        ("pm status --json", "emit structured status for scripts"),
    ],
)

_SEND_HELP = help_with_examples(
    (
        "Send input directly into a managed tmux pane.\n\n"
        "Accepts a config session name (``operator``), a per-task "
        "worker window (``task-<project>-<N>``), or the ``<project>/<N>`` "
        "shortcut that resolves to the per-task window."
    ),
    [
        ('pm send operator "Build a weather CLI"', "ask Polly to start work"),
        (
            'pm send demo/7 "use the new helper" --owner human',
            "steer a per-task worker mid-flow via the <project>/<N> shortcut",
        ),
        (
            'pm send worker_demo "continue" --force',
            "bypass the worker guard for a manual nudge",
        ),
    ],
)

_NOTIFY_HELP = help_with_examples(
    (
        "Create a work-service inbox item for the human user.\n\n"
        "This is PollyPM's canonical escalation channel for blockers, "
        "handoffs, and status updates."
    ),
    [
        (
            'pm notify "Deploy blocked" "Needs verification email click."',
            "open an immediate user-visible inbox item",
        ),
        (
            'echo "Longer body" | pm notify "Status update" -',
            "read the notification body from stdin",
        ),
        (
            'pm notify "Plan ready" "Review the explainer" --priority immediate',
            "create a high-priority review notification",
        ),
    ],
)

app = typer.Typer(help=_APP_HELP, invoke_without_command=True, no_args_is_help=False)
app.add_typer(alert_app, name="alert")
app.add_typer(session_app, name="session")
app.add_typer(heartbeat_app, name="heartbeat")
app.add_typer(issue_app, name="issue")
app.add_typer(report_app, name="report")
app.add_typer(itsalive_app, name="itsalive")
app.add_typer(debug_app, name="debug")
app.add_typer(dev_app, name="dev")

from pollypm.work.cli import task_app, flow_app
app.add_typer(task_app, name="task")
app.add_typer(flow_app, name="flow")

from pollypm.work.inbox_cli import inbox_app
app.add_typer(inbox_app, name="inbox")

from pollypm.jobs.cli import jobs_app
app.add_typer(jobs_app, name="jobs")

from pollypm.plugin_cli import plugins_app
app.add_typer(plugins_app, name="plugins")

from pollypm.rail_cli import rail_app
app.add_typer(rail_app, name="rail")

from pollypm.plugins_builtin.activity_feed.cli import activity_app
app.add_typer(activity_app, name="activity")

from pollypm.plugins_builtin.morning_briefing.cli import briefing_app
app.add_typer(briefing_app, name="briefing")

from pollypm.plugins_builtin.project_planning.cli import project_app
app.add_typer(project_app, name="project")

from pollypm.memory_cli import memory_app
app.add_typer(memory_app, name="memory")

from pollypm.plugins_builtin.advisor.cli.advisor_cli import advisor_app
app.add_typer(advisor_app, name="advisor")

from pollypm.plugins_builtin.downtime.cli import downtime_app
app.add_typer(downtime_app, name="downtime")

register_ui_commands(app)
register_project_commands(app)
register_maintenance_commands(app)
register_update_commands(app)
register_upgrade_commands(app)
register_migrate_commands(app)
register_worker_commands(app)
register_session_runtime_commands(app, helpers=sys.modules[__name__])


def attach_existing_session(session_name: str) -> int:
    from pollypm.session_services import attach_existing_session as _attach_existing_session

    return _attach_existing_session(session_name)


def current_session_name() -> str | None:
    from pollypm.session_services import current_session_name as _current_session_name

    return _current_session_name()


def probe_session(session_name: str) -> bool:
    from pollypm.session_services import probe_session as _probe_session

    return _probe_session(session_name)


def switch_client_to_session(session_name: str) -> int:
    from pollypm.session_services import switch_client_to_session as _switch_client_to_session

    return _switch_client_to_session(session_name)


def session_environment_value(session_name: str, variable: str) -> str | None:
    from pollypm.session_services import (
        session_environment_value as _session_environment_value,
    )

    return _session_environment_value(session_name, variable)


def start_transcript_ingestion(config) -> None:
    from pollypm.transcript_ingest import start_transcript_ingestion as _start_transcript_ingestion

    _start_transcript_ingestion(config)


def create_worker_session(*args, **kwargs):
    from pollypm.workers import create_worker_session as _create_worker_session

    return _create_worker_session(*args, **kwargs)


def launch_worker_session(*args, **kwargs):
    from pollypm.workers import launch_worker_session as _launch_worker_session

    return _launch_worker_session(*args, **kwargs)


def _session_name_candidates() -> list[str]:
    return ["pollypm", "pollypm-storage-closet"]


def _discover_config_path(config_path: Path) -> Path:
    return resolve_config_path(config_path)


def _config_option_was_explicit() -> bool:
    return any(arg == "--config" or arg.startswith("--config=") for arg in sys.argv[1:])


def _attach_existing_session_without_config() -> bool:
    current_tmux = current_session_name()
    for session_name in _session_name_candidates():
        if not probe_session(session_name):
            continue
        if _session_home_mismatch(_bootstrap_session_home(session_name)) is not None:
            continue
        if current_tmux == session_name:
            return True
        if current_tmux:
            raise typer.Exit(code=switch_client_to_session(session_name))
        raise typer.Exit(code=attach_existing_session(session_name))
    return False


def _normalize_path_for_compare(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(Path(value).expanduser().resolve(strict=False))
    except Exception:  # noqa: BLE001
        return str(Path(value).expanduser())


def _session_home_mismatch(owner_home: str | None) -> tuple[str, str] | None:
    current_home = os.environ.get("HOME")
    owner = _normalize_path_for_compare(owner_home)
    current = _normalize_path_for_compare(current_home)
    if not owner or not current or owner == current:
        return None
    return (owner, current)


def _bootstrap_session_home(session_name: str) -> str | None:
    try:
        return session_environment_value(session_name, "HOME")
    except Exception:  # noqa: BLE001
        return None


def _supervisor_session_home(supervisor, session_name: str) -> str | None:
    tmux = getattr(supervisor, "tmux", None)
    show_environment = getattr(tmux, "show_environment", None)
    if not callable(show_environment):
        return None
    try:
        return show_environment(session_name, "HOME")
    except Exception:  # noqa: BLE001
        return None


def _refuse_foreign_tmux_sessions(
    supervisor,
    probe: LaunchProbe,
    config_path: Path,
    command_display: str = "pm up",
) -> None:
    """Fail closed before mutating a same-named session from another HOME."""
    sessions = (
        (probe.main_session_name, probe.main_session_alive),
        (probe.closet_session_name, probe.closet_session_alive),
    )
    for session_name, alive in sessions:
        if not session_name or not alive:
            continue
        mismatch = _session_home_mismatch(
            _supervisor_session_home(supervisor, session_name)
        )
        if mismatch is None:
            continue
        owner_home, current_home = mismatch
        raise typer.BadParameter(
            f"tmux session '{session_name}' is in use by another HOME "
            f"({owner_home}); current HOME is {current_home}. Set "
            f"`project.tmux_session` in {config_path} to a unique name "
            f"before running `{command_display}`."
        )


def _load_supervisor(config_path: Path):
    """Return a full Supervisor via the service_api facade."""
    from pollypm.service_api import PollyPMService

    return PollyPMService(config_path).load_supervisor()


def _enforce_migration_gate(config_path: Path) -> None:
    """Refuse-start guard: bail out if the workspace state.db is behind (#717).

    Skipped when ``POLLYPM_SKIP_MIGRATION_GATE`` is set — ``pm migrate``
    turns the bypass on explicitly so the apply path can itself open the
    store. A config that cannot be loaded is treated as "no gate to
    enforce yet" so onboarding / first-run paths keep working.
    """
    from pollypm.store import migrations as _migrations

    if _migrations.bypass_env_is_set():
        return
    try:
        from pollypm.config import load_config
        config = load_config(config_path)
        db_path = config.project.state_db
    except Exception:  # noqa: BLE001
        return
    _migrations.require_no_pending_or_exit(db_path)


def _account_label(supervisor, account_name: str) -> str:
    account = supervisor.config.accounts.get(account_name)
    if account is None:
        return account_name
    return account.email or account.name


def _cli_status(msg: str) -> None:
    """Print a status update on its own line."""
    typer.echo(msg)


def _emit_json(payload: object) -> None:
    from pollypm.service_api import render_json

    typer.echo(render_json(payload), nl=False)


def _install_global_pollypm(root_dir: Path) -> tuple[bool, str]:
    result = subprocess.run(
        ["uv", "tool", "install", "--editable", "--reinstall", str(root_dir)],
        cwd=root_dir,
        check=False,
        text=True,
        capture_output=True,
    )
    output = "\n".join(part for part in [result.stdout.strip(), result.stderr.strip()] if part).strip()
    return (result.returncode == 0, output)


def _require_pollypm_session(supervisor) -> None:
    current_tmux = supervisor.tmux.current_session_name()
    expected = supervisor.config.project.tmux_session
    allowed = {expected, supervisor.storage_closet_session_name()}
    if current_tmux not in allowed:
        raise typer.BadParameter(
            f"This command must run inside tmux session '{expected}'. Use `pm up` to attach first."
        )


def _first_run_setup_and_launch(config_path: Path) -> None:
    from pollypm.onboarding import run_onboarding
    path = run_onboarding(config_path=config_path, force=False)
    _install_global_pollypm(path.parent)
    # #1111 — pass phantom_client=False explicitly. Calling the
    # Typer-decorated up() as a plain function leaves Option defaults
    # as truthy OptionInfo objects; spelling the value out keeps the
    # phantom_client gate False.
    up(config_path=path, phantom_client=False)


def _print_version_and_exit(value: bool) -> None:
    """Eager ``--version`` callback — prints and exits without running the app."""
    if not value:
        return
    from pollypm import __version__ as _pm_version

    typer.echo(f"pollypm {_pm_version}")
    raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    _version: bool = typer.Option(
        False,
        "--version",
        help="Show the installed PollyPM version and exit.",
        is_eager=True,
        callback=_print_version_and_exit,
    ),
) -> None:
    config_path = _discover_config_path(config_path)
    if ctx.invoked_subcommand is None:
        if not config_path.exists():
            if config_path == DEFAULT_CONFIG_PATH and _attach_existing_session_without_config():
                return
            _first_run_setup_and_launch(config_path=config_path)
            return
        # #1111 — pass phantom_client=False explicitly. ctx.invoke alone
        # didn't reliably apply the Typer Option default in production
        # (the OptionInfo sentinel survived and tripped the phantom-client
        # launch path on bare `pm`).
        global _UP_INVOCATION_COMMAND
        previous_command = _UP_INVOCATION_COMMAND
        _UP_INVOCATION_COMMAND = "pm"
        try:
            ctx.invoke(up, config_path=config_path, phantom_client=False)
        finally:
            _UP_INVOCATION_COMMAND = previous_command


@app.command(help="Write the example PollyPM config to disk to bootstrap a new install.")
def init(
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="Path to write the example config."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing config file."),
) -> None:
    write_example_config(config_path, force=force)
    typer.echo(f"Wrote config to {config_path}")


@app.command(help="Print the example PollyPM config template (TOML) to stdout.")
def example_config() -> None:
    typer.echo(render_example_config())


@app.command()
def shortcuts() -> None:
    """Print a curated cheatsheet of PollyPM commands."""
    typer.echo(render_shortcuts_text())


_ROLE_GUIDES = {
    "worker": ("docs/worker-guide.md", "Worker onboarding guide"),
}


@app.command("help")
def role_help(
    role: str = typer.Argument(
        ...,
        help="Role whose guide to print. Currently supported: worker.",
    ),
) -> None:
    """Print the canonical guide for a role (worker, ...).

    Role-scoped help surfaces the same content that's auto-injected
    into a role's session prompt. Use this when you're outside a
    managed session and need the playbook.
    """
    role_norm = role.strip().lower()
    entry = _ROLE_GUIDES.get(role_norm)
    if entry is None:
        available = ", ".join(sorted(_ROLE_GUIDES.keys())) or "<none>"
        typer.echo(
            f"No guide registered for role '{role}'. "
            f"Available: {available}.",
            err=True,
        )
        raise typer.Exit(code=1)
    rel_path, title = entry
    # Resolve against the repo root. ``pollypm`` is installed editable
    # during dev; at runtime we prefer the packaged doc if it exists,
    # falling back to the repo copy.
    from importlib.resources import files as _files

    doc_text: str | None = None
    try:
        # Packaged layout: src/pollypm/defaults/worker-guide.md (if we
        # later ship it). For now fall through to the repo docs dir.
        candidate = _files("pollypm").joinpath(f"../../{rel_path}")
        if candidate.is_file():
            doc_text = candidate.read_text()
    except (ModuleNotFoundError, FileNotFoundError, TypeError):
        pass
    if doc_text is None:
        # Walk up from this file to find the project root's docs/ dir.
        here = Path(__file__).resolve()
        for parent in here.parents:
            candidate = parent / rel_path
            if candidate.is_file():
                doc_text = candidate.read_text()
                break
    if doc_text is None:
        typer.echo(
            f"Could not locate {rel_path} on disk. "
            f"The guide exists in the PollyPM repo at that path.",
            err=True,
        )
        raise typer.Exit(code=1)
    typer.echo(doc_text)


@app.command(
    help=(
        "Run the interactive onboarding flow to write a PollyPM "
        "config and install the global ``pollypm`` / ``pm`` commands."
    ),
)
def onboard(
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="Path to write the onboarding config."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing config file."),
) -> None:
    from pollypm.onboarding import run_onboarding
    path = run_onboarding(config_path=config_path, force=force)
    installed, install_output = _install_global_pollypm(path.parent)
    typer.echo("")
    typer.echo(f"Wrote onboarding config to {path}")
    if installed:
        typer.echo("Installed global commands: `pollypm` and `pm`.")
    else:
        typer.echo("Could not auto-install the global `pollypm` command.")
        if install_output:
            typer.echo(install_output)
    typer.echo("Next step: run `pollypm up` or `uv run pm up` to create or attach to the PollyPM tmux session.")


_COCKPIT_WINDOW_NAME: str = "PollyPM"
"""Canonical name of the cockpit window (rail + console panes).

Mirrors :data:`pollypm.cockpit_rail.CockpitRouter._COCKPIT_WINDOW`
and :data:`pollypm.supervisor.Supervisor.CONSOLE_WINDOW`. Defined
locally so the launch probe does not have to import either module —
both have transitive cost the launch path should not pay before the
state machine has decided what to do.
"""


_SHELL_COMMANDS: frozenset[str] = frozenset(
    {"bash", "zsh", "fish", "sh", "dash", "ksh"}
)
"""Process names that count as a "shell" for the rail pane.

Used to derive ``rail_pane_running_non_shell``: when the rail pane
is running one of these, the TUI has died back to its shell — the
state machine recovers via ``RECOVER_DEAD_RAIL``. When the pane is
running anything else (the Textual TUI, Python, etc.), the rail is
considered live and ``ATTACH_EXISTING`` will not respawn it (#841).
"""


def _build_launch_probe(supervisor) -> LaunchProbe:
    """Build a :class:`LaunchProbe` snapshot from the supervisor.

    The probe reads tmux state through the public ``supervisor.tmux``
    surface plus the configured session names. Every introspection
    call is wrapped in a defensive ``_safe`` shim — the probe must
    never raise, because a launcher that crashes during state
    detection cannot fall through to a useful error message.

    Pane-level liveness (console / rail) is read from ``list_panes``
    on the cockpit window. When the window or panes cannot be
    enumerated, the probe falls back to ``True`` for liveness:
    assuming live is safer than triggering a speculative respawn on a
    stale snapshot (the #841 segfault path was a respawn-while-live
    case). The fallback is documented and tested explicitly.

    The state machine (#884) consumes this and returns the named
    :class:`LaunchState`. ``up()`` echoes the state name + reason so
    every launch decision is observable in the CLI output.
    """
    project = getattr(getattr(supervisor, "config", None), "project", None)
    main_name = getattr(project, "tmux_session", "") or ""
    closet_name = ""
    closet_getter = getattr(supervisor, "storage_closet_session_name", None)
    if callable(closet_getter):
        try:
            closet_name = closet_getter() or ""
        except Exception:  # noqa: BLE001 — probe must never raise
            closet_name = ""
    # Fallback derivation: the supervisor's canonical convention
    # is ``<main>-storage-closet``. When a supervisor mock / older
    # test harness omits the helper, derive the name so the state
    # machine still has a non-empty closet name to reason about.
    if not closet_name and main_name:
        closet_name = f"{main_name}-storage-closet"

    tmux = getattr(supervisor, "tmux", None)

    def _safe(call, *args, default):
        if tmux is None:
            return default
        method = getattr(tmux, call, None)
        if method is None:
            return default
        try:
            return method(*args)
        except Exception:  # noqa: BLE001
            return default

    main_alive = bool(_safe("has_session", main_name, default=False)) if main_name else False
    closet_alive = (
        bool(_safe("has_session", closet_name, default=False)) if closet_name else False
    )
    current_tmux = _safe("current_session_name", default=None)

    # Pane liveness: only meaningful when the main session is alive.
    # When it isn't, the state machine routes through FIRST_LAUNCH /
    # RESTORE_FROM_CLOSET regardless of pane state, so leaving the
    # defaults at "alive" keeps the no-session path unchanged.
    console_pane_alive = True
    rail_pane_alive = True
    rail_pane_running_non_shell = True
    if main_alive and main_name:
        target = _cockpit_window_pane_target(tmux, main_name)
        # ``list_panes`` returns ``[]`` for both "window absent" and
        # "tmux unavailable"; the state machine treats both as
        # console+rail healthy via the default-True values above.
        # When list_panes returns a non-empty list, the pane shape
        # is the source of truth.
        panes = _safe("list_panes", target, default=None)
        if panes:
            capture_pane = getattr(tmux, "capture_pane", None)
            console_pane_alive, rail_pane_alive, rail_pane_running_non_shell = (
                _classify_cockpit_panes(panes, capture_pane=capture_pane)
            )

    return LaunchProbe(
        main_session_name=main_name,
        closet_session_name=closet_name,
        main_session_alive=main_alive,
        closet_session_alive=closet_alive,
        console_pane_alive=console_pane_alive,
        rail_pane_alive=rail_pane_alive,
        rail_pane_running_non_shell=rail_pane_running_non_shell,
        current_tmux_session=current_tmux,
    )


def _cockpit_window_pane_target(tmux, main_name: str) -> str:
    """Return an unambiguous tmux target for the cockpit window's panes."""
    list_windows = getattr(tmux, "list_windows", None)
    if callable(list_windows):
        try:
            windows = list_windows(main_name) or []
        except Exception:  # noqa: BLE001
            windows = []
        for window in windows:
            if getattr(window, "name", "") != _COCKPIT_WINDOW_NAME:
                continue
            pane_id = getattr(window, "pane_id", None)
            if isinstance(pane_id, str) and pane_id:
                return pane_id
    return f"{main_name}:{_COCKPIT_WINDOW_NAME}"


def _classify_cockpit_panes(panes, *, capture_pane=None) -> tuple[bool, bool, bool]:
    """Classify the cockpit window's pane list into the three
    pane-liveness probe fields.

    Convention: the cockpit window has up to two panes — left rail TUI
    and right scoped pane. ``pane_left`` is "0" for the leftmost pane.
    Returns ``(console_alive, rail_alive, rail_non_shell)``. When only
    one pane is present, infer whether it is the rail from the command:
    shell means the rail has not started; non-shell means the right pane
    is missing. Some real rail panes report the shell wrapper as the
    current command even while the cockpit rail is visibly running; when a
    safe pane capture proves that UI is present, treat the rail as healthy.
    """
    left = None
    right = None
    for pane in panes:
        # ``pane_left`` is the X coordinate as a string ("0" for the
        # leftmost pane). The dataclass exposes it directly.
        try:
            x = int(getattr(pane, "pane_left", "0") or "0")
        except (TypeError, ValueError):
            x = 0
        if left is None or x < left[0]:
            left = (x, pane)
        if right is None or x > right[0]:
            right = (x, pane)

    if left is right and left is not None:
        pane = left[1]
        pane_alive = not bool(getattr(pane, "pane_dead", False))
        pane_cmd = (getattr(pane, "pane_current_command", "") or "").lower()
        pane_non_shell = pane_alive and (
            pane_cmd not in _SHELL_COMMANDS
            or _pane_capture_looks_like_cockpit_rail(pane, capture_pane)
        )
        if pane_non_shell:
            return (False, True, True)
        return (pane_alive, False, False)

    if left is None or right is None:
        return (True, True, True)

    rail_pane = left[1]
    console_pane = right[1]
    console_alive = not bool(getattr(console_pane, "pane_dead", False))
    rail_alive = not bool(getattr(rail_pane, "pane_dead", False))
    rail_cmd = (getattr(rail_pane, "pane_current_command", "") or "").lower()
    rail_non_shell = rail_alive and (
        rail_cmd not in _SHELL_COMMANDS
        or _pane_capture_looks_like_cockpit_rail(rail_pane, capture_pane)
    )
    return (console_alive, rail_alive, rail_non_shell)


def _pane_capture_looks_like_cockpit_rail(pane, capture_pane) -> bool:
    if not callable(capture_pane):
        return False
    pane_id = getattr(pane, "pane_id", None)
    if not isinstance(pane_id, str) or not pane_id:
        return False
    try:
        text = str(capture_pane(pane_id, lines=40) or "")
    except Exception:  # noqa: BLE001
        return False
    normalized = " ".join(text.lower().split())
    return (
        "polly" in normalized
        and "home" in normalized
        and "workers" in normalized
        and "inbox" in normalized
    )


@app.command(help=_UP_HELP)
def up(
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    phantom_client: bool = typer.Option(
        False,
        "--phantom-client",
        help=(
            "After boot, spawn a detached pty client attached to the pollypm "
            "session so the cockpit keeps processing keystrokes when no real "
            "terminal is attached. Workaround for #1109. Experimental — see "
            "the issue for caveats."
        ),
    ),
) -> None:
    config_path = _discover_config_path(config_path)
    if not config_path.exists():
        if config_path == DEFAULT_CONFIG_PATH and _attach_existing_session_without_config():
            return
        typer.echo(f"Config not found at {config_path}. Starting onboarding.")
        onboard(config_path=config_path, force=False)
        return
    _enforce_migration_gate(config_path)
    supervisor = _load_supervisor(config_path)

    # #884 / #905 — consult the launch state machine BEFORE any
    # startup side effects (CoreRail boot, ensure_layout, transcript
    # ingestion, tmux mutation). The fail-closed contract requires
    # UNSUPPORTED to short-circuit before the launcher mutates
    # ambient state. The probe only reads tmux + config; building it
    # is side-effect-free.
    probe = _build_launch_probe(supervisor)
    _refuse_foreign_tmux_sessions(
        supervisor,
        probe,
        config_path,
        command_display=_UP_INVOCATION_COMMAND,
    )
    plan = plan_launch(probe)
    typer.echo(f"[launch] {plan.state.value}: {plan.reason}")
    if plan.state is LaunchState.UNSUPPORTED:
        raise typer.BadParameter(plan.reason)

    # CoreRail owns startup orchestration — it drives plugin host load,
    # state store readiness, and Supervisor boot (which runs ensure_layout,
    # ensure_heartbeat_schedule, and ensure_knowledge_extraction_schedule).
    # Test harnesses that mock Supervisor without a core_rail fall back
    # to ensure_layout for layout scaffolding only; the executor below
    # owns ensure_heartbeat_schedule via the SCHEDULE_HEARTBEAT action.
    if hasattr(supervisor, "core_rail"):
        supervisor.core_rail.start()
    else:  # pragma: no cover - back-compat for mocked Supervisors in tests
        supervisor.ensure_layout()
    if all(hasattr(supervisor.config, field) for field in ("project", "accounts", "projects")) and hasattr(
        supervisor.config.project, "base_dir"
    ):
        start_transcript_ingestion(supervisor.config)
    session_name = supervisor.config.project.tmux_session

    def _prepare_cockpit_before_attach() -> None:
        from pollypm.cockpit_rail import CockpitRouter

        router = CockpitRouter(config_path)
        router.ensure_cockpit_layout()
        import time
        time.sleep(0.3)  # let tmux settle after the split
        supervisor.start_cockpit_tui(session_name)
        if phantom_client:
            # #1109 — spawn a detached pty client so the cockpit's
            # Textual app keeps processing input even when no real
            # terminal is attached. Best-effort; failures only mean
            # the workaround didn't engage.
            spawned = _spawn_phantom_client(session_name)
            if spawned:
                typer.echo(
                    f"[#1109] phantom-client attached to {session_name} "
                    "(cockpit input loop kept alive while you detach)"
                )
            else:
                typer.echo(
                    "[#1109] phantom-client spawn failed — "
                    "`tmux send-keys` may not work after detach. "
                    "See `pm up --help` for context.",
                    err=True,
                )

    # #896 — drive every supervisor / tmux mutation through the
    # state-machine executor so plan.actions is the source of
    # truth for what runs. The executor handles BOOTSTRAP_LAUNCHES,
    # ENSURE_*, RESPAWN_*, SCHEDULE_HEARTBEAT, START_RAIL_DAEMON,
    # ATTACH/SWITCH/FOCUS — every action declared in the plan.
    executor = LaunchPlanExecutor(
        supervisor,
        probe=probe,
        config_path=config_path,
        status_emit=_cli_status,
        rail_daemon_spawner=_spawn_rail_daemon,
        before_attach=_prepare_cockpit_before_attach,
    )
    result = executor.execute(plan)
    for message in result.messages:
        typer.echo(message)

    if result.exit_code is not None:
        raise typer.Exit(code=result.exit_code)


def _rail_daemon_pid_path() -> Path:
    """Location of the rail-daemon PID file (~/.pollypm/rail_daemon.pid)."""
    return Path(DEFAULT_CONFIG_PATH).parent / "rail_daemon.pid"


def _rail_daemon_live() -> bool:
    """Return True iff the PID file names a currently-running process."""
    import os as _os
    pid_path = _rail_daemon_pid_path()
    if not pid_path.exists():
        return False
    try:
        pid = int(pid_path.read_text().strip())
    except (ValueError, OSError):
        return False
    if pid <= 0:
        return False
    try:
        _os.kill(pid, 0)
        return True
    except ProcessLookupError:
        # Stale PID file — clean up for the caller.
        pid_path.unlink(missing_ok=True)
        return False
    except PermissionError:
        return True


def _spawn_rail_daemon(config_path: Path) -> None:
    """Launch ``pollypm.rail_daemon`` detached; best-effort.

    Failures are non-fatal — ``pm up`` succeeds without the daemon,
    users just don't get auto-recovery while the cockpit is closed.
    A warning is printed so the degraded state is visible.

    Tests that exercise ``pm up`` set ``POLLYPM_SKIP_RAIL_DAEMON=1``
    to opt out of the spawn — otherwise they'd leak detached processes
    pointing at their pytest-tmp config paths.
    """
    import os as _os
    import subprocess as _sp
    import sys as _sys

    if _os.environ.get("POLLYPM_SKIP_RAIL_DAEMON"):
        return
    if _rail_daemon_live():
        return
    pollypm_home = Path(DEFAULT_CONFIG_PATH).parent
    pollypm_home.mkdir(parents=True, exist_ok=True)
    log_path = pollypm_home / "rail_daemon.log"
    try:
        log_fh = open(log_path, "a", buffering=1)  # line-buffered
    except OSError as exc:
        typer.echo(
            f"Warning: could not open rail daemon log {log_path}: {exc}. "
            "Skipping daemon spawn — auto-recovery will only run while "
            "the cockpit is open.",
            err=True,
        )
        return
    try:
        _sp.Popen(
            [_sys.executable, "-m", "pollypm.rail_daemon",
             "--config", str(config_path)],
            stdout=log_fh, stderr=log_fh, stdin=_sp.DEVNULL,
            start_new_session=True,  # detach from tty/process group
            close_fds=True,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(
            f"Warning: rail daemon spawn failed ({exc}). Auto-recovery "
            "will only run while the cockpit is open.",
            err=True,
        )


def _spawn_phantom_client(session_name: str) -> bool:
    """Spawn a detached pty-backed ``tmux attach`` client to keep the
    cockpit's input loop alive when no real terminal is attached (#1109).

    Background: the cockpit's Textual app stops handling keystrokes
    when no tmux client is attached to the session. ``tmux send-keys``
    silently no-ops in that state, which destroys overnight test loops
    where Sam detaches and expects the harness to drive the cockpit
    via ``send-keys``. Wrapping ``tmux attach`` in ``script`` gives it
    a controlling pty so tmux treats it as a live client.

    Best-effort. Returns ``True`` on successful spawn (process started),
    ``False`` on any error. Caller decides how to surface failure. The
    spawned process is detached (``start_new_session=True``) so it
    survives the parent ``pm`` exit.

    Caveats (worth documenting in the issue rather than complicating
    the helper):
    - The phantom client doesn't auto-replace itself if it dies.
    - It will steal focus from a real client briefly during attach;
      pass ``-d`` so any real attach detaches the phantom.
    - On systems where ``script`` doesn't accept ``-q`` / the
      pty allocation fails, the spawn returns ``False``.
    """
    import shutil as _shutil
    import subprocess as _sp

    script_bin = _shutil.which("script")
    tmux_bin = _shutil.which("tmux")
    if script_bin is None or tmux_bin is None:
        return False
    pollypm_home = Path(DEFAULT_CONFIG_PATH).parent
    pollypm_home.mkdir(parents=True, exist_ok=True)
    log_path = pollypm_home / "phantom_client.log"
    try:
        log_fh = open(log_path, "a", buffering=1)
    except OSError:
        return False
    # macOS ``script`` signature is ``script [-q] file command...``;
    # GNU ``script`` is ``script [-q] -c "command" file``. Detect by
    # platform — both ship with ``-q``.
    import sys as _sys
    if _sys.platform == "darwin":
        argv = [script_bin, "-q", "/dev/null", tmux_bin, "attach", "-d", "-t", session_name]
    else:
        argv = [
            script_bin,
            "-q",
            "-c",
            f"{tmux_bin} attach -d -t {session_name}",
            "/dev/null",
        ]
    try:
        _sp.Popen(
            argv,
            stdout=log_fh,
            stderr=log_fh,
            stdin=_sp.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except Exception:  # noqa: BLE001
        return False
    return True


def _stop_rail_daemon() -> None:
    """Signal the rail daemon to shut down (SIGTERM). Best-effort."""
    import os as _os
    import signal as _signal

    pid_path = _rail_daemon_pid_path()
    if not pid_path.exists():
        return
    try:
        pid = int(pid_path.read_text().strip())
    except (ValueError, OSError):
        pid_path.unlink(missing_ok=True)
        return
    try:
        _os.kill(pid, _signal.SIGTERM)
    except ProcessLookupError:
        pass  # already gone
    except Exception:  # noqa: BLE001
        pass
    # Best effort; the daemon's atexit handler removes the file.
    # Clean up here too in case the daemon crashed without signal.
    pid_path.unlink(missing_ok=True)


def _tick_core_rail_if_available(supervisor) -> None:
    """Tick the process-wide HeartbeatRail if the supervisor exposes one.

    No-ops silently when the rail isn't available (legacy supervisors,
    mocked test harnesses, boot failures). Swallows tick exceptions so
    a bad roster entry can't break the session-health heartbeat that
    already ran above.
    """
    rail_getter = getattr(supervisor, "core_rail", None)
    if rail_getter is None:
        return None
    try:
        # CoreRail.start() is idempotent and ensures the HeartbeatRail
        # is booted. #1060 — boot in tick-only mode: this CLI helper is
        # the ``pm heartbeat`` cron path, which only needs to enqueue
        # roster-due jobs. Skipping the JobWorkerPool / ticker avoids
        # the "thread did not stop within timeout" warnings that fired
        # 8.3k times in errors.log because workers couldn't honour the
        # 10s pool.stop deadline mid-handler. The long-lived
        # ``rail_daemon`` / cockpit rail drains the enqueued jobs.
        # Mocked supervisors in tests pass ``start_workers`` as a kwarg
        # transparently (MagicMock accepts any signature), so the
        # existing ``core_rail.start.assert_called_once()`` contract
        # still holds.
        try:
            rail_getter.start(start_workers=False)
        except TypeError:
            # Back-compat: a CoreRail/mock without the new kwarg falls
            # back to the legacy boot. Worst case we re-emit the
            # original warning, but tests with MagicMock supervisors
            # accept any kwarg so this branch is dead in production.
            rail_getter.start()
        heartbeat_rail = rail_getter.get_heartbeat_rail()
        if heartbeat_rail is None:
            return None
        return heartbeat_rail.tick()
    except Exception:  # noqa: BLE001
        # Non-fatal — session-health sweep already succeeded above.
        logger.debug("pm heartbeat: core rail tick failed", exc_info=True)
        return None


def _drain_and_stop_core_rail_if_available(
    supervisor,
    *,
    tick_result=None,
    drain_timeout_seconds: float = 5.0,
    poll_interval_seconds: float = 0.05,
) -> None:
    """Best-effort drain + stop for transient CLI-owned HeartbeatRails.

    ``pm heartbeat`` is often invoked from cron as a short-lived
    process. Starting the CoreRail in that path used to boot a worker
    pool and ticker thread; #1060 switched the cron path to tick-only
    mode (``start_workers=False``) so jobs are enqueued and drained by
    the long-lived ``rail_daemon`` / cockpit rail instead. The drain
    poll below is a no-op when the pool isn't running — we skip
    straight to ``stop()`` to keep cron exits crisp.
    """
    rail_getter = getattr(supervisor, "core_rail", None)
    if rail_getter is None:
        return
    try:
        heartbeat_rail = rail_getter.get_heartbeat_rail()
        queue = getattr(heartbeat_rail, "queue", None) if heartbeat_rail is not None else None
        getter = getattr(queue, "get", None)
        enqueued = list(getattr(tick_result, "enqueued", ()) or ())
        # If the rail booted in tick-only mode the pool is not running,
        # so no worker will move enqueued jobs out of ``queued`` —
        # polling would just burn the drain budget. Skip straight to
        # stop(); the rail_daemon picks them up on its next short-poll.
        # Only treat a *known* not-running pool as a skip signal —
        # callers without a ``pool`` attribute (legacy test mocks) keep
        # the original drain semantics.
        pool = getattr(heartbeat_rail, "pool", None) if heartbeat_rail is not None else None
        pool_known_idle = pool is not None and not bool(
            getattr(pool, "is_running", True)
        )
        if callable(getter) and enqueued and not pool_known_idle:
            import time as _time

            pending = {
                getattr(job, "job_id", None)
                for job in enqueued
                if getattr(job, "job_id", None) is not None
            }
            deadline = _time.monotonic() + max(0.0, drain_timeout_seconds)
            while pending and _time.monotonic() < deadline:
                for job_id in tuple(pending):
                    try:
                        job = getter(job_id)
                    except Exception:  # noqa: BLE001
                        pending.discard(job_id)
                        continue
                    if job is None:
                        pending.discard(job_id)
                        continue
                    status = getattr(job, "status", None)
                    status_value = getattr(status, "value", status)
                    if status_value not in {"queued", "claimed"}:
                        pending.discard(job_id)
                if pending and poll_interval_seconds > 0:
                    _time.sleep(poll_interval_seconds)
    except Exception:  # noqa: BLE001
        logger.debug("pm heartbeat: core rail drain failed", exc_info=True)
    finally:
        try:
            rail_getter.stop()
        except Exception:  # noqa: BLE001
            logger.debug("pm heartbeat: core rail stop failed", exc_info=True)
