"""Session/runtime/operator CLI commands.

Contract:
- Inputs: Typer options/arguments plus helper callbacks exported by
  ``pollypm.cli`` for supervisor loading, config-path resolution, and
  shared JSON/session guards.
- Outputs: root command registrations on the passed Typer app.
- Side effects: session startup/shutdown, lease mutations, pane sends,
  inbox notifications, and diagnostic reads against the supervisor/store.
- Invariants: session/runtime command bodies stay out of ``pollypm.cli``;
  the root module remains composition plus shared compatibility helpers.
"""

from __future__ import annotations

import functools
import inspect
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import typer

from pollypm.config import DEFAULT_CONFIG_PATH
from pollypm.inbox.kind import InboxItemKind

logger = logging.getLogger(__name__)

_TASK_ID_PATTERN = re.compile(r"\b([A-Za-z0-9_.-]+/\d+)\b")

# #1076 — match Polly's freeform "Nth (suspected) fake RECOVERY MODE
# injection ..." subjects so they're auto-routed to ``channel:dev``
# (suppressed from the default inbox view) instead of dirtying the
# user-facing surface. Polly emits these as natural-language ``pm
# notify`` calls during prompt-injection self-reports — the literal
# never appears in source, but the subject shape is consistent enough
# to gate at the producer. The dev-only override
# ``POLLYPM_DEV_FAKE_RECOVERY_INBOX=1`` opts back in to the legacy
# inbox-channel routing for harness work that explicitly wants these
# in the user inbox.
_FAKE_RECOVERY_INJECTION_SUBJECT = re.compile(
    r"\bfake\s+RECOVERY\s+MODE\s+injection\b",
    re.IGNORECASE,
)


def _is_fake_recovery_injection_subject(subject: str) -> bool:
    """Return True when the subject matches the Polly meta-report shape (#1076)."""
    if not subject:
        return False
    return bool(_FAKE_RECOVERY_INJECTION_SUBJECT.search(subject))


def _fake_recovery_inbox_override_enabled() -> bool:
    """Return True when the dev-only env var opts these into the inbox channel.

    Off by default — Polly's meta-reports stay in ``channel:dev`` unless
    a developer explicitly sets ``POLLYPM_DEV_FAKE_RECOVERY_INBOX=1`` to
    reproduce the legacy noise.
    """
    raw = os.environ.get("POLLYPM_DEV_FAKE_RECOVERY_INBOX", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _notify_user_prompt_fallback_note_enabled() -> bool:
    """Return True when missing-user-prompt fallback notes should be printed."""
    raw = os.environ.get("POLLYPM_NOTIFY_USER_PROMPT_FALLBACK_NOTE", "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_notify_store():
    """Return the configured-backend ``Store`` for ``pm notify`` writes.

    Routes through :func:`pollypm.store.get_store` so
    ``[storage].backend`` decides where the write lands. The returned
    ``Store`` is a process-wide singleton — do NOT ``close()`` it.

    Issue #1755 / #1790: ``pm notify`` constructing
    ``SQLAlchemyStore("sqlite:///<state>")`` directly silently wrote
    to the empty sqlite shadow whenever the configured backend was
    postgres, so live pg readers never saw the alert and the
    immediate-priority inbox task fan-out hit a non-existent row.
    The legacy ``--db`` flag was removed in the sqlite-ripout
    (refs #1971); pg is the only supported backend post-#1737.
    """
    from pollypm.config import load_config
    from pollypm.store import get_store

    return get_store(load_config())


# ``<project>/<N>`` matches the canonical task id form. When ``pm send``
# receives an argument matching this shape we translate it to the
# per-task worker window name (#924) so the user does not have to know
# the ``task-<project>-<N>`` convention.
_TASK_ID_FULL_PATTERN = re.compile(r"^([A-Za-z0-9_.-]+)/(\d+)$")


def _resolve_send_target_name(name: str) -> str:
    """Translate ``<project>/<N>`` to ``task-<project>-<N>``; pass through otherwise.

    The canonical per-task window name comes from
    :func:`pollypm.work.session_manager.task_window_name`. Mirroring the
    construction here keeps ``pm send`` independent of an import on the
    work-service module.
    """
    match = _TASK_ID_FULL_PATTERN.match(name)
    if match is None:
        return name
    project, number = match.group(1), match.group(2)
    return f"task-{project}-{number}"


# Dispatch identifiers the cockpit dashboard's
# ``_perform_dashboard_action`` knows how to route. Producers that
# emit user_prompt actions with kinds outside this set hit the
# generic record-response fallback, which is almost never the
# intended behaviour. The set lives here so the producer-side
# validator catches typos before the message lands in the store.
_USER_PROMPT_ACTION_KINDS: frozenset[str] = frozenset(
    {
        "review_plan",
        "open_task",
        "open_inbox",
        "discuss_pm",
        "approve_task",
        "record_response",
    }
)


def _infer_notify_actor(config_path: Path, actor: str) -> tuple[str, str | None]:
    """Resolve ``pm notify``'s default actor from the current tmux window.

    ``pm notify`` is frequently run from managed role panes (reviewer,
    operator, architect). Leaving the default sender as ``polly`` hides
    who actually raised the escalation. When the caller did not
    override ``--actor`` and we're running inside a managed tmux window,
    infer the sender from the matching configured session name.
    """
    if (actor or "").strip() != "polly":
        return actor, None
    matched = _match_notify_session_config(config_path)
    if matched is None:
        return actor, None
    session_name, _session_cfg = matched
    return session_name, session_name


def _match_notify_session_config(
    config_path: Path,
) -> tuple[str, object] | None:
    """Find the configured session matching the current tmux window.

    Returns ``(session_name, session_cfg)`` when the caller is running
    inside a managed role pane (reviewer, operator, architect, worker),
    or ``None`` when no match can be made. Shared by the actor and
    project inference paths so both surfaces agree on the session.
    """
    try:
        from pollypm.config import load_config
        from pollypm.session_services import create_tmux_client

        tmux = create_tmux_client()
        tmux_session = tmux.current_session_name()
        window_index = tmux.current_window_index()
        if not tmux_session or window_index is None:
            return None
        window_name = None
        for window in tmux.list_windows(tmux_session):
            if str(getattr(window, "index", "")) == str(window_index):
                window_name = getattr(window, "name", None)
                break
        if not window_name:
            return None
        config = load_config(config_path)
        sessions = getattr(config, "sessions", {}) or {}
        for session_name, session_cfg in sessions.items():
            expected = getattr(session_cfg, "window_name", None) or session_name
            if expected == window_name:
                return session_name, session_cfg
    except Exception:  # noqa: BLE001
        return None
    return None


def _infer_notify_project(config_path: Path) -> str | None:
    """Resolve ``pm notify``'s default ``--project`` from the calling pane.

    Issue #1425: when an agent that's running in a project context
    (e.g. ``reviewer-savethenovel``) calls ``pm notify`` without an
    explicit ``--project``, the message should land at that project's
    namespace — not the synthetic ``inbox`` bucket. Otherwise project-
    scoped views (``pm message list --project <key>``, the cockpit's
    project-filtered inbox) silently drop substantive review feedback
    because the operator has to know to look in the global inbox.

    Returns the matching session's ``project`` key, or ``None`` when no
    project context can be inferred (so the caller can fall back to
    the legacy ``inbox`` default).
    """
    matched = _match_notify_session_config(config_path)
    if matched is None:
        return None
    _session_name, session_cfg = matched
    project_key = getattr(session_cfg, "project", None)
    if not project_key:
        return None
    return str(project_key)


def _hold_review_tasks_for_notify(
    *,
    actor: str,
    current_session_name: str | None,
    priority: str,
    subject: str,
    body: str,
) -> list[str]:
    """Keep notify-driven review tasks in ``review``.

    The inbox message itself carries the action context. Demoting review
    tasks to ``on_hold`` hides the accept/reject path and breaks the v1
    state model, so this helper is intentionally a no-op.
    """
    _ = actor, current_session_name, priority, subject, body
    return []


def _bind_session_command(callback, helpers):
    """Bind ``helpers`` as the first arg of ``callback`` while preserving the
    inspectable signature Typer needs.

    Typer's command-registration path calls :func:`typing.get_type_hints` on
    the registered callable. ``functools.partial`` objects don't satisfy
    ``get_type_hints``'s "module/class/method/function" check (Python 3.13's
    typing.py raises ``TypeError`` on them), so we build a real wrapper
    function whose signature drops the bound first parameter — Typer then
    sees a normal function with the user-facing options/arguments only.
    """
    sig = inspect.signature(callback)
    parameters = list(sig.parameters.values())
    if not parameters:
        bound_param_name = None
        new_sig = sig
    else:
        bound_param_name = parameters[0].name
        new_sig = sig.replace(parameters=parameters[1:])

    @functools.wraps(callback)
    def wrapper(*args, **kwargs):
        return callback(helpers, *args, **kwargs)

    wrapper.__signature__ = new_sig
    if bound_param_name is not None:
        wrapper.__annotations__ = {
            name: annotation
            for name, annotation in callback.__annotations__.items()
            if name != bound_param_name
        }
    return wrapper


def launch(
    helpers,
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."
    ),
) -> None:
    # #1111 — pass phantom_client=False explicitly. Calling the
    # Typer-decorated up() directly leaves OptionInfo sentinels for
    # unsupplied params, which are truthy and would spawn the
    # phantom client unintentionally.
    helpers.up(config_path=config_path, phantom_client=False)


def rail_daemon(
    helpers,
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."
    ),
    poll_interval: float = typer.Option(
        60.0, "--poll-interval", help="Seconds between idle-loop wakeups."
    ),
) -> None:
    """Run the headless heartbeat/recovery rail in the foreground.

    This is the same rail ``pm up`` auto-spawns in the background.
    Run it yourself if you want to:
      - supervise it from launchd / systemd
      - watch its log output directly
      - debug scheduler / recovery behavior

    The daemon auto-exits if another rail daemon is already live.
    """
    from pollypm.rail_daemon import run as _run_daemon

    config_path = helpers._discover_config_path(config_path)
    raise typer.Exit(code=_run_daemon(config_path, poll_interval=poll_interval))


def reset(
    helpers,
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."
    ),
    force: bool = typer.Option(
        False, "--force", "-f", help="Skip confirmation prompt."
    ),
) -> None:
    """Kill all PollyPM tmux sessions (cockpit + storage closet). Use `pm up` to restart."""
    from pollypm.errors import format_config_not_found_error

    config_path = helpers._discover_config_path(config_path)
    if not config_path.exists():
        typer.echo(format_config_not_found_error(config_path), err=True)
        raise typer.Exit(code=1)
    supervisor = helpers._load_supervisor(config_path)
    session_name = supervisor.config.project.tmux_session
    storage_name = supervisor.storage_closet_session_name()
    sessions_to_kill = [
        name
        for name in [session_name, storage_name]
        if supervisor.tmux.has_session(name)
    ]
    if not sessions_to_kill:
        typer.echo("No PollyPM tmux sessions found.")
        return
    if not force:
        names = ", ".join(sessions_to_kill)
        session_word = "session" if len(sessions_to_kill) == 1 else "sessions"
        typer.confirm(
            f"This will kill the PollyPM {session_word} ({names}). Continue?",
            abort=True,
        )
    supervisor.shutdown_tmux()
    helpers._stop_rail_daemon()
    # #1590 — tmux kill-session SIGHUP doesn't propagate to ``python -m
    # pollypm cockpit-pane`` children (Python ignores SIGHUP by default,
    # and the pane child often re-parents to the tmux daemon / PID 1
    # so the pane-kill has no pollypm-side target). Without this sweep,
    # orphans accumulate across ``pm reset`` cycles — the field report
    # had one running 4 days at 98% CPU. Reap them explicitly after
    # the tmux teardown so the reset promise actually holds.
    try:
        from pollypm.cockpit_pane_reaper import reap_orphan_cockpit_panes

        reaped_panes = reap_orphan_cockpit_panes()
    except Exception:  # noqa: BLE001 - reap is best-effort, never fail reset
        reaped_panes = []
    jobs_path = supervisor.config.project.base_dir / "scheduler" / "jobs.json"
    jobs_path.unlink(missing_ok=True)
    cockpit_state = supervisor.config.project.base_dir / "cockpit_state.json"
    cockpit_state.unlink(missing_ok=True)
    try:
        # ``supervisor.store`` is the StateStore (sqlite-only — it owns
        # the ``leases`` / ``session_runtime`` tables that don't have
        # a pg equivalent yet, tracked separately). ``msg_store`` may
        # be either SQLAlchemyStore or PgStore; route the alert wipe
        # through the typed ``prune_messages`` (#1820) so we don't
        # depend on the SQLAlchemy ``execute`` escape hatch.
        supervisor.store.execute("DELETE FROM leases")
        supervisor.store.execute("DELETE FROM session_runtime")
        try:
            supervisor.msg_store.prune_messages(type="alert", state="open")
        except Exception:  # noqa: BLE001
            logger.debug(
                "Failed to prune open alerts via typed path; "
                "falling back to SQLAlchemy execute()",
                exc_info=True,
            )
            from sqlalchemy import delete
            from pollypm.store.schema import messages

            supervisor.msg_store.execute(
                delete(messages).where(
                    messages.c.type == "alert",
                    messages.c.state == "open",
                )
            )
        supervisor.store.commit()
    except Exception:  # noqa: BLE001
        logger.warning(
            "Failed to clear leases/session_runtime/open alerts during reset (#1355)",
            exc_info=True,
        )
    session_word = "session" if len(sessions_to_kill) == 1 else "sessions"
    typer.echo(
        f"Killed {len(sessions_to_kill)} {session_word}: {', '.join(sessions_to_kill)}"
    )
    if reaped_panes:
        # Surface the orphan kill so the operator knows the reset
        # actually cleaned up tmux-leaked children (#1590).
        pids_str = ", ".join(str(entry.pid) for entry in reaped_panes)
        pane_word = "pane" if len(reaped_panes) == 1 else "panes"
        typer.echo(
            f"Reaped {len(reaped_panes)} orphan cockpit-{pane_word}: {pids_str}"
        )


def status(
    helpers,
    session_name: str | None = typer.Argument(
        None, help="Optional session name from config."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."
    ),
) -> None:
    if not helpers._config_option_was_explicit():
        config_path = helpers._discover_config_path(config_path)
    helpers._enforce_migration_gate(config_path)
    from pollypm.service_api import PollyPMService

    payload = PollyPMService(config_path).session_status(session_name)
    sessions = payload["sessions"]
    if session_name is not None and not sessions:
        raise typer.BadParameter(f"Unknown session: {session_name}")
    if json_output:
        helpers._emit_json(payload)
        return
    plugin_errors = payload.get("plugin_errors") or []
    if not sessions:
        typer.echo("No sessions configured.")
    else:
        typer.echo(f"Config: {payload['config_path']}")
        for item in sessions:
            # Per-task workers (#1061) are tagged ``per_task`` by the
            # service layer so the user can tell at a glance which
            # rows came from the post-#1059 per-task-claim flow vs.
            # the long-lived configured sessions.
            suffix = " (per-task)" if item.get("kind") == "per_task" else ""
            typer.echo(
                f"- {item['name']}: status={item['status']} running={'yes' if item['running'] else 'no'} "
                f"alerts={item['alert_count']} lease={item['lease_owner'] or '-'} "
                f"project={item['project']} role={item['role']}{suffix}"
            )
            if item["last_failure_message"]:
                typer.echo(f"  reason={item['last_failure_message']}")
        for error in payload["errors"]:
            typer.echo(f"- error: {error}")
    # Plugin load errors — surfaced so a silently-broken plugin
    # (e.g. #957's relative-import bug in core_recurring, which
    # disappeared two scheduled jobs without warning) is visible
    # to the operator. See #960. Always render this section even
    # when there are no sessions configured — broken plugins are
    # equally invisible in that state.
    if plugin_errors:
        typer.echo("")
        typer.echo(f"Plugin load errors ({len(plugin_errors)}):")
        for entry in plugin_errors:
            plugin_name = entry.get("plugin") or "<host>"
            stage = entry.get("stage") or "load"
            message = entry.get("message") or ""
            typer.echo(f"- {plugin_name} [{stage}]: {message}")


def plan(
    helpers,
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."
    ),
) -> None:
    # Read-only inspection — runs from any shell. The tmux gate was
    # removed for #1055 so the diagnostic flow ``pm alerts`` ->
    # ``pm plan`` -> ``pm worker-start`` works end-to-end without an
    # intervening ``pm up``. ``plan_launches`` only reads config; no
    # tmux state is mutated here.
    supervisor = helpers._load_supervisor(config_path)
    for launch in supervisor.plan_launches():
        typer.echo(f"[{launch.session.name}]")
        typer.echo(f"window = {launch.window_name}")
        typer.echo(f"log = {launch.log_path}")
        typer.echo(f"command = {launch.command}")
        typer.echo("")


def alerts(
    helpers,
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."
    ),
) -> None:
    from pollypm.service_api import PollyPMService

    items = PollyPMService(config_path).list_alerts()
    if not items:
        typer.echo("No open alerts.")
        return
    if json_output:
        helpers._emit_json({"alerts": items})
        return
    for alert in items:
        typer.echo(
            f"- #{alert.alert_id} {alert.severity} {alert.session_name}/{alert.alert_type}: {alert.message}"
        )


def failover(
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."
    ),
) -> None:
    """Show failover configuration: controller account and failover order."""
    from pollypm.config import load_config

    config = load_config(config_path)
    typer.echo(f"Controller: {config.pollypm.controller_account}")
    typer.echo(
        f"Failover enabled: {'yes' if config.pollypm.failover_enabled else 'no'}"
    )
    if config.pollypm.failover_accounts:
        typer.echo("Failover order:")
        for index, name in enumerate(config.pollypm.failover_accounts, 1):
            account = config.accounts.get(name)
            label = f"{account.email} [{account.provider.value}]" if account else name
            typer.echo(f"  {index}. {label}")
    else:
        typer.echo("No failover accounts configured.")


def debug_command(
    helpers,
    session: str | None = typer.Option(
        None, "--session", "-s", help="Filter to a specific session."
    ),
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."
    ),
) -> None:
    """Show diagnostic info: open alerts, session states, recent events. Works outside tmux."""
    supervisor = helpers._load_supervisor(config_path)

    all_alerts = supervisor.open_alerts()
    alerts_list = [
        alert
        for alert in all_alerts
        if session is None or alert.session_name == session
    ]
    typer.echo(f"Open alerts: {len(alerts_list)}")
    for alert in alerts_list:
        typer.echo(
            f"  {alert.severity} {alert.session_name}/{alert.alert_type}: {alert.message}"
        )

    typer.echo("")
    launches = supervisor.plan_launches()
    windows = supervisor.window_map()
    for launch in launches:
        if session is not None and launch.session.name != session:
            continue
        # #1096 — window_map keys by (tmux_session, window_name).
        tmux_session = supervisor.tmux_session_for_launch(launch)
        window = windows.get((tmux_session, launch.window_name))
        if window is None:
            state = "not running"
        elif window.pane_dead:
            state = "dead"
        else:
            state = f"running ({window.pane_current_command})"
        typer.echo(
            f"  {launch.session.name}: {state} [{launch.session.provider.value}/{launch.account.name}]"
        )

    typer.echo("")
    # #1830: route through supervisor facade for pg/sqlite parity.
    events_list = supervisor.recent_events(limit=5)
    if session is not None:
        events_list = [event for event in events_list if event.session_name == session]
    typer.echo(f"Recent events: {len(events_list)}")
    for event in events_list[:5]:
        typer.echo(
            f"  {event.created_at} {event.session_name}/{event.event_type}: {event.message}"
        )


def events(
    helpers,
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."
    ),
    limit: int = typer.Option(
        20, "--limit", min=1, max=200, help="Maximum number of events to show."
    ),
) -> None:
    # Read-only event log — runs from any shell. Gate removed for
    # #1055 so triage-from-any-shell works (alerts hint ``pm events``
    # as the next-step diagnostic; that hint must work without
    # ``pm up``). ``recent_events`` is a pure DB read.
    supervisor = helpers._load_supervisor(config_path)
    # #1830: route through supervisor facade for pg/sqlite parity.
    items = supervisor.recent_events(limit=limit)
    if not items:
        typer.echo("No events recorded.")
        return
    for event in items:
        typer.echo(
            f"- {event.created_at} {event.session_name}/{event.event_type}: {event.message}"
        )


def claim(
    helpers,
    session_name: str = typer.Argument(..., help="Session name from config."),
    owner: str = typer.Option("human", "--owner", help="Lease owner label."),
    note: str = typer.Option("", "--note", help="Optional note for the lease."),
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."
    ),
) -> None:
    supervisor = helpers._load_supervisor(config_path)
    helpers._require_pollypm_session(supervisor)
    supervisor.claim_lease(session_name, owner, note)
    typer.echo(f"Lease set on {session_name} for {owner}")


def release(
    helpers,
    session_name: str = typer.Argument(..., help="Session name from config."),
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."
    ),
) -> None:
    supervisor = helpers._load_supervisor(config_path)
    helpers._require_pollypm_session(supervisor)
    supervisor.release_lease(session_name)
    typer.echo(f"Lease released for {session_name}")


def send(
    helpers,
    session_name: str = typer.Argument(
        ...,
        help=(
            "Session name from config (e.g. ``operator``), the "
            "per-task worker window ``task-<project>-<N>``, or the "
            "shortcut ``<project>/<N>`` which resolves to the per-task "
            "window."
        ),
    ),
    text: str = typer.Argument(..., help="Text to send into the tmux pane."),
    owner: str = typer.Option(
        "pollypm", "--owner", help="Sender label for lease checks."
    ),
    force: bool = typer.Option(False, "--force", help="Bypass a conflicting lease."),
    no_enter: bool = typer.Option(
        False, "--no-enter", help="Do not send Enter after the text."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."
    ),
) -> None:
    supervisor = helpers._load_supervisor(config_path)
    # ``<project>/<N>`` shortcut → per-task worker window (#924). The
    # canonical window name lives in
    # :func:`pollypm.work.session_manager.task_window_name`; mirror its
    # shape here so ``pm send`` users do not have to type the
    # ``task-<project>-<N>`` form by hand.
    resolved_name = _resolve_send_target_name(session_name)
    if resolved_name != session_name:
        session_name = resolved_name
    session_cfg = supervisor.config.sessions.get(session_name)
    if session_cfg and session_cfg.role == "worker" and not force:
        project = session_cfg.project or session_name.replace("worker_", "", 1)
        typer.echo(
            f"Blocked: dispatch work through the task system.\n"
            f'  pm task create "Title" -p {project} -d "description" '
            f"-f standard -r worker=worker -r reviewer=polly\n"
            f"  pm task queue {project}/<number>\n"
            f"\n"
            f"The worker picks up queued tasks automatically.\n"
            f"If the auto-pickup path is broken and you need to nudge "
            f"this worker directly, re-run with --force."
        )
        raise typer.Exit(code=1)
    try:
        supervisor.send_input(
            session_name,
            text,
            owner=owner,
            force=force,
            press_enter=not no_enter,
        )
    except RuntimeError as exc:
        raise typer.BadParameter(str(exc)) from exc
    except KeyError as exc:
        # ``launch_by_session`` raises KeyError when the name is not
        # a config-defined session and not a per-task worker window
        # (#924). Surface the friendly message rather than a stack
        # trace.
        raise typer.BadParameter(
            exc.args[0] if exc.args else f"Unknown session: {session_name}"
        ) from exc
    if json_output:
        helpers._emit_json(
            {
                "session_name": session_name,
                "owner": owner,
                "text": text,
                "press_enter": not no_enter,
                "forced": force,
            }
        )
        return
    typer.echo(f"Sent input to {session_name}")


def _validate_user_prompt_payload(user_prompt_json: str) -> dict[str, object] | None:
    """Parse and validate ``--user-prompt-json`` for ``pm notify`` (#1356 wedge).

    Returns the parsed payload dict when the caller supplied a non-empty
    JSON string, ``None`` when ``user_prompt_json`` is empty/whitespace.
    Emits the same producer-side errors and raises ``typer.Exit(1)`` on
    contract violations so the operator sees the failure immediately
    instead of in the dashboard pane hours later. Extracted verbatim from
    :func:`notify` to keep the parent body under the 200-LOC threshold;
    see issue #1356.
    """
    if not user_prompt_json.strip():
        return None
    try:
        parsed_prompt = json.loads(user_prompt_json)
    except json.JSONDecodeError as exc:
        typer.echo(f"Error: --user-prompt-json is not valid JSON: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if not isinstance(parsed_prompt, dict):
        typer.echo(
            "Error: --user-prompt-json must decode to an object.",
            err=True,
        )
        raise typer.Exit(code=1)
    # The dashboard contract requires at least one of summary,
    # steps (or required_actions), or question — otherwise the
    # rendered Action Needed card has nothing to show and the
    # caller is sending a structurally-empty payload that
    # silently degrades to the heuristic fallback. Catch this
    # at the producer so the operator sees the contract failure
    # immediately instead of in the dashboard pane hours later.
    has_summary = bool(str(parsed_prompt.get("summary") or "").strip())
    has_question = bool(str(parsed_prompt.get("question") or "").strip())
    raw_steps = (
        parsed_prompt.get("steps") or parsed_prompt.get("required_actions") or []
    )
    has_steps = isinstance(raw_steps, list) and any(
        str(step).strip() for step in raw_steps
    )
    if not (has_summary or has_question or has_steps):
        typer.echo(
            "Error: --user-prompt-json must include at least one of "
            "'summary', 'steps' (or 'required_actions'), or "
            "'question' — those are the fields the dashboard "
            "Action Needed card renders. Empty payloads degrade "
            "to body heuristics and are indistinguishable from "
            "omitting the flag entirely.",
            err=True,
        )
        raise typer.Exit(code=1)
    # Each ``action`` must use one of the dispatch identifiers
    # the dashboard's _perform_dashboard_action understands.
    # Unknown kinds silently fall through to the generic
    # record-response path, so the operator clicks a button
    # labelled 'Approve' and nothing actually approves —
    # exactly the symptom the v1 doc flagged. Reject at the
    # producer so typos and outdated kind names surface
    # immediately.
    raw_actions = parsed_prompt.get("actions") or []
    if isinstance(raw_actions, list):
        for idx, raw_action in enumerate(raw_actions):
            if not isinstance(raw_action, dict):
                typer.echo(
                    f"Error: --user-prompt-json action[{idx}] "
                    f"must be an object with 'label' and 'kind' "
                    f"keys, got {type(raw_action).__name__}.",
                    err=True,
                )
                raise typer.Exit(code=1)
            label_value = str(raw_action.get("label") or "").strip()
            kind_value = str(raw_action.get("kind") or "").strip()
            # An action without a label can't render a button, and
            # an action without a kind can't dispatch on click —
            # the dashboard's _user_prompt_decision drops both
            # silently and falls back to default copy. Producer
            # almost certainly meant to specify both.
            if not label_value:
                typer.echo(
                    f"Error: --user-prompt-json action[{idx}] is "
                    f"missing a non-empty 'label'. The dashboard "
                    f"renders that as the button caption — "
                    f"actions without one get silently dropped.",
                    err=True,
                )
                raise typer.Exit(code=1)
            if not kind_value:
                typer.echo(
                    f"Error: --user-prompt-json action[{idx}] "
                    f"('label': {label_value!r}) is missing a "
                    f"non-empty 'kind'. Supported kinds: "
                    f"{', '.join(sorted(_USER_PROMPT_ACTION_KINDS))}.",
                    err=True,
                )
                raise typer.Exit(code=1)
            if kind_value not in _USER_PROMPT_ACTION_KINDS:
                typer.echo(
                    f"Error: --user-prompt-json action[{idx}] "
                    f"has unknown kind '{kind_value}'. Supported "
                    f"kinds: "
                    f"{', '.join(sorted(_USER_PROMPT_ACTION_KINDS))}. "
                    f"Custom kinds silently fall back to "
                    f"record-response in the dashboard, which is "
                    f"almost never the producer's intent.",
                    err=True,
                )
                raise typer.Exit(code=1)
    return parsed_prompt


def _create_notify_inbox_task(
    *,
    store,
    message_id: object,
    subject: str,
    body: str,
    project: str,
    actor: str,
    requester_role: str,
    label_list: list[str],
    notify_kind: str,
    payload: dict[str, object],
) -> str:
    """Create the inbox task for an immediate-priority ``pm notify``.

    Extracted from :func:`notify` (#1356 wedge) to keep the parent body
    under the 200-LOC threshold. Builds the work-service task, refreshes
    the originating message payload with the assigned ``task_id`` so the
    dashboard can cross-link them, and returns the ``task_id``. Errors
    are surfaced via ``typer.echo`` + ``typer.Exit(1)`` to match the
    original inline behavior.

    Backend-aware (#1790): the messages-store ``store`` is the
    process-wide singleton resolved by :func:`_resolve_notify_store`,
    and the work-service is built through
    :func:`pollypm.work.create_work_service` with the active
    :class:`PollyPMConfig` so ``[storage].backend`` lands the row on
    the configured backend. Post-sqlite-ripout (refs #1971) there is
    only the pg backend.
    """
    from pollypm.work import create_work_service

    config_obj = None
    try:
        from pollypm.config import load_config

        config_obj = load_config()
    except Exception:  # noqa: BLE001
        config_obj = None

    svc = create_work_service(config=config_obj, project_key=project)
    try:
        task_labels = [
            *label_list,
            "notify",
            f"notify_message:{message_id}",
        ]
        task = svc.create(
            title=subject,
            description=body,
            type="task",
            project=project,
            flow_template="chat",
            roles={
                "requester": requester_role,
                "operator": actor or "polly",
            },
            priority="high",
            created_by=actor,
            labels=task_labels,
            kind=notify_kind,
        )
        inbox_task_id = task.task_id
        # ``store`` is the process-wide messages singleton from
        # :func:`_resolve_notify_store`; do NOT close it.
        store.update_message(
            message_id,
            payload={**payload, "task_id": inbox_task_id},
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"Failed to create inbox task: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        svc.close()
    return inbox_task_id


def _kind_for_notify(
    labels: list[str],
    *,
    requester: str,
    user_prompt_payload: dict | None,
) -> str:
    """Pick the inbox ``kind`` for a ``pm notify`` emit (#1567/#1568).

    Most ``pm notify`` calls land with no label hint, and the caller is
    a human (the architect, Polly herself, etc.) typing free-form text
    — those stay ``legacy`` so the predicate's fail-open default keeps
    them visible until #1570 backfills. Only label-keyed shapes that
    map cleanly onto an actionable kind get explicit retraining here:

    * ``plan_review`` label → ``plan_review_pending`` (architect's
      canonical ``pm notify --label plan_review`` handoff).
    * ``--user-prompt-json`` payload routed at ``requester=user`` →
      ``pm_question_unanswered`` (producer explicitly built an Action
      Needed card with a question for the user).
    """
    label_set = {label for label in labels if isinstance(label, str)}
    if "plan_review" in label_set:
        return InboxItemKind.PLAN_REVIEW_PENDING.value
    if user_prompt_payload is not None and requester == "user":
        return InboxItemKind.PM_QUESTION_UNANSWERED.value
    return InboxItemKind.LEGACY.value


@dataclass(frozen=True)
class _NotifyArgs:
    """Normalized ``pm notify`` arguments after validation.

    Produced by :func:`_normalize_notify_args`; bundles every field the
    enqueue path needs so the top-level command stays a thin driver.
    """

    channel_name: str
    body: str
    actor: str
    current_session_name: str | None
    project: str
    resolved_priority: str
    requester_role: str
    label_list: list[str]
    milestone_key: str | None
    user_prompt_payload: dict[str, object] | None


def _normalize_notify_args(
    helpers,
    *,
    subject: str,
    body: str,
    actor: str,
    project: str,
    priority: str,
    milestone: str,
    labels: list[str] | None,
    requester: str,
    user_prompt_json: str,
    channel: str,
) -> _NotifyArgs:
    """Validate + normalize the raw ``pm notify`` arguments.

    Centralizes every error-and-exit / classification / inference step
    so the top-level ``notify`` body can stay focused on the enqueue
    pipeline. Behavior is identical to the inline version: each
    ``typer.Exit`` site is preserved verbatim, and the helpers
    (``_infer_notify_actor``, ``_infer_notify_project``,
    ``_validate_user_prompt_payload``, the classifier import) are
    called in the same order with the same arguments.
    """
    if not subject.strip():
        typer.echo("Error: subject must not be empty.", err=True)
        raise typer.Exit(code=1)

    channel_name = (channel or "inbox").strip().lower()
    if channel_name not in {"inbox", "dev"}:
        typer.echo(
            f"Error: --channel must be 'inbox' or 'dev' (got {channel!r}).",
            err=True,
        )
        raise typer.Exit(code=1)

    # #1076 — auto-route Polly's "Nth fake RECOVERY MODE injection"
    # meta-reports to channel:dev so they don't pollute the user-
    # facing inbox. Gated behind ``POLLYPM_DEV_FAKE_RECOVERY_INBOX``
    # so harness work that explicitly wants these in the inbox can
    # still opt in (off by default — the user-facing inbox is the
    # default surface and dev scaffolding doesn't belong there).
    if (
        channel_name == "inbox"
        and _is_fake_recovery_injection_subject(subject)
        and not _fake_recovery_inbox_override_enabled()
    ):
        channel_name = "dev"

    if body == "-":
        body = sys.stdin.read()
    if not body.strip():
        typer.echo(
            "Error: body must not be empty (pass '-' to read from stdin).",
            err=True,
        )
        raise typer.Exit(code=1)

    resolved_config_path = helpers._discover_config_path(DEFAULT_CONFIG_PATH)
    actor, current_session_name = _infer_notify_actor(
        resolved_config_path,
        actor,
    )

    # #1425 — when the caller didn't pass ``--project``, infer it
    # from the calling pane's session config so a reviewer running
    # in ``reviewer-savethenovel`` lands its notify on
    # ``project=savethenovel`` instead of the synthetic global
    # ``inbox`` bucket. Explicit ``--project inbox`` (or any other
    # value) still wins so global broadcasts and bootstrap scripts
    # keep working from inside a project-scoped pane.
    if not (project or "").strip():
        inferred_project = _infer_notify_project(resolved_config_path)
        project = inferred_project or "inbox"

    from pollypm.store.classifier import classify_priority, validate_priority

    requested = (priority or "auto").strip().lower()
    if requested == "auto":
        resolved_priority = classify_priority(subject, body)
    else:
        try:
            resolved_priority = validate_priority(requested)
        except ValueError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=1) from exc

    requester_role = (requester or "user").strip().lower()
    if requester_role not in ("user", "polly"):
        typer.echo(
            f"Error: --requester must be 'user' or 'polly' (got {requester!r}).",
            err=True,
        )
        raise typer.Exit(code=1)

    label_list = [label for label in (labels or []) if label and label.strip()]
    # Channel separation (#754): dev-channel messages carry a
    # ``channel:dev`` label so the default inbox view (and the
    # cockpit rail count) can skip them. Regular user-facing
    # notifications inherit the implicit ``channel:inbox`` label.
    if channel_name == "dev":
        if "channel:dev" not in label_list:
            label_list.append("channel:dev")
    milestone_key = milestone.strip() or None
    user_prompt_payload = _validate_user_prompt_payload(user_prompt_json)

    return _NotifyArgs(
        channel_name=channel_name,
        body=body,
        actor=actor,
        current_session_name=current_session_name,
        project=project,
        resolved_priority=resolved_priority,
        requester_role=requester_role,
        label_list=label_list,
        milestone_key=milestone_key,
        user_prompt_payload=user_prompt_payload,
    )


def _maybe_warn_user_prompt_fallback(args: _NotifyArgs) -> None:
    """Emit the producer-side diagnostic note about heuristic fallback.

    The dashboard has a heuristic fallback when producers omit
    ``--user-prompt-json``. Keep that fallback quiet by default: role
    panes often stream stderr into user-facing logs, where a
    ``Warning:`` prefix reads like a broken escalation. Developers
    debugging producer payloads can opt into this diagnostic note via
    :func:`_notify_user_prompt_fallback_note_enabled`.
    """
    if (
        args.user_prompt_payload is None
        and args.resolved_priority == "immediate"
        and args.requester_role == "user"
        and args.channel_name == "inbox"
        and _notify_user_prompt_fallback_note_enabled()
    ):
        typer.echo(
            "note: posting an immediate-priority user-facing "
            "notify without --user-prompt-json. The dashboard's "
            "Action Needed card is using body-heuristic fallback "
            "and lose structured steps + decision question + "
            "contextual buttons. Pass --user-prompt-json '{...}' "
            "with at least one of summary/steps/question for a "
            "first-class action surface.",
            err=True,
        )


def _enqueue_notify_message(
    store,
    *,
    subject: str,
    args: _NotifyArgs,
    dedup_key: str,
    notify_kind: str,
) -> str:
    """Dispatch the notify message: dedup-bump or first-write insert.

    #1013 — dedup-key collapsing for repeated alert patterns. When
    ``--dedup-key`` is set and a matching open notify exists, increment
    its ``count`` + refresh ``last_seen`` instead of spawning a second
    row. Empty key (the default) keeps the legacy insert-every-time
    behavior so existing callers don't silently change semantics.

    The ``store`` is the process-wide singleton from
    :func:`_resolve_notify_store` (#1790); the caller must NOT close it
    after this returns.
    """
    from pollypm.inbox_dedup import (
        bump_dedup_message,
        find_open_dedup_message,
        initial_dedup_payload,
    )

    tier_state = {
        "immediate": "open",
        "digest": "staged",
        "silent": "closed",
    }[args.resolved_priority]

    payload: dict[str, object] = {
        "actor": args.actor,
        "project": args.project,
        "milestone_key": args.milestone_key,
        "requester": args.requester_role,
    }
    if args.user_prompt_payload is not None:
        payload["user_prompt"] = args.user_prompt_payload

    dedup_key_value = (dedup_key or "").strip()
    existing_dedup_row = (
        find_open_dedup_message(
            store,
            dedup_key_value,
            recipient=args.requester_role,
        )
        if dedup_key_value
        else None
    )

    try:
        if existing_dedup_row is not None:
            # Bump path — caller signaled "this is the same alert
            # I posted before". Refresh subject/body/payload so the
            # most recent context wins, and increment count.
            message_id = bump_dedup_message(
                store,
                existing_dedup_row,
                subject=subject,
                body=args.body,
                payload={**payload, "dedup_key": dedup_key_value},
                labels=args.label_list or None,
                tier=args.resolved_priority,
            )
        else:
            # First-write path. Annotate payload with count=1 +
            # last_seen so a future bump has a stable shape to
            # increment.
            seeded_payload = (
                initial_dedup_payload(payload, dedup_key_value)
                if dedup_key_value
                else payload
            )
            message_id = store.enqueue_message(
                type="notify",
                tier=args.resolved_priority,
                recipient=args.requester_role,
                sender=args.actor,
                subject=subject,
                body=args.body,
                scope=args.project,
                labels=args.label_list or None,
                payload=seeded_payload,
                state=(
                    "closed"
                    if args.resolved_priority == "immediate"
                    else tier_state
                ),
                kind=notify_kind,
            )
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"Failed to enqueue notify message: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    return message_id


def notify(
    helpers,
    subject: str = typer.Argument(..., help="Short title for the inbox item."),
    body: str = typer.Argument(..., help="Message body. Pass '-' to read from stdin."),
    actor: str = typer.Option(
        "polly", "--actor", help="Who is posting the notification."
    ),
    project: str = typer.Option(
        "",
        "--project",
        "-p",
        help=(
            "Project namespace for the notification task. When omitted, "
            "infer from the calling tmux pane's session config "
            "(reviewer-<project>, worker-<project>, architect, …) — "
            "agents running in a project context land their notify on "
            "that project automatically (#1425). Falls back to 'inbox' "
            "(global) when no project context is detectable. Pass "
            "``--project inbox`` explicitly to force a global "
            "notification from a project-scoped pane."
        ),
    ),
    priority: str = typer.Option(
        "auto",
        "--priority",
        help=(
            "Tier: 'immediate' surfaces in the inbox now; 'digest' stages "
            "silently and rolls up at the next milestone boundary; "
            "'silent' only records an audit event. 'auto' (default) "
            "infers the tier from subject/body keywords — falling back "
            "to 'immediate' when ambiguous."
        ),
    ),
    milestone: str = typer.Option(
        "",
        "--milestone",
        help=(
            "Optional milestone key for digest bucketing "
            "(e.g. 'milestones/02-core-features'). Leave blank to let "
            "milestone detection classify at flush time."
        ),
    ),
    labels: list[str] = typer.Option(
        None,
        "--label",
        help=(
            "Attach a label to the created inbox task. Repeatable. "
            "Used by typed flows like plan_review "
            "(e.g. --label plan_review --label 'plan_task:key/1' "
            "--label 'explainer:/abs/path/plan-review.html')."
        ),
    ),
    requester: str = typer.Option(
        "user",
        "--requester",
        help=(
            "Role assigned as the task's requester. Defaults to 'user' "
            "(normal user inbox). Pass 'polly' to route to Polly's "
            "inbox instead (fast-track plan_review)."
        ),
    ),
    user_prompt_json: str = typer.Option(
        "",
        "--user-prompt-json",
        help=(
            "JSON contract for user-facing action cards. Shape: "
            '{"summary": str, "steps": [str], "question": str, '
            '"actions": [{"label": str, "kind": str, ...}]}.'
        ),
    ),
    channel: str = typer.Option(
        "inbox",
        "--channel",
        help=(
            "Delivery channel. ``inbox`` (default) = real user-facing "
            "notification that surfaces in ``pm inbox`` and the cockpit. "
            "``dev`` = developer / test-harness traffic that stays in "
            "the store for debugging but is hidden from the default "
            "inbox view. Use ``dev`` in tests and one-off scripts so "
            "they never pollute the real signal (#754)."
        ),
    ),
    dedup_key: str = typer.Option(
        "",
        "--dedup-key",
        help=(
            "Stable identifier for collapsing repeated alert "
            "patterns (#1013). When two ``pm notify`` calls share "
            "the same ``--dedup-key`` and an open notify with that "
            "key exists, the existing row's ``count`` is "
            "incremented and ``last_seen`` refreshed instead of a "
            "second row being inserted. Use for repeating "
            "operator-tooling alerts like "
            "``polly:rejected-recovery-mode-injection`` so the "
            "inbox shows ``9x - last seen 2 days ago`` instead of "
            "twelve near-identical rows. Empty (default) keeps "
            "the legacy insert-every-time behavior."
        ),
    ),
) -> None:
    """Create a work-service inbox item for the human user."""
    args = _normalize_notify_args(
        helpers,
        subject=subject,
        body=body,
        actor=actor,
        project=project,
        priority=priority,
        milestone=milestone,
        labels=labels,
        requester=requester,
        user_prompt_json=user_prompt_json,
        channel=channel,
    )
    _maybe_warn_user_prompt_fallback(args)

    # Backend-aware Store dispatch (#1790). Singleton — do NOT close.
    store = _resolve_notify_store()
    notify_kind = _kind_for_notify(
        args.label_list,
        requester=args.requester_role,
        user_prompt_payload=args.user_prompt_payload,
    )
    message_id = _enqueue_notify_message(
        store,
        subject=subject,
        args=args,
        dedup_key=dedup_key,
        notify_kind=notify_kind,
    )

    # Rebuild the payload shape that ``_create_notify_inbox_task``
    # consumes. Mirrors the seed dict in :func:`_enqueue_notify_message`
    # so the inbox-task projection matches the message-row payload.
    payload: dict[str, object] = {
        "actor": args.actor,
        "project": args.project,
        "milestone_key": args.milestone_key,
        "requester": args.requester_role,
    }
    if args.user_prompt_payload is not None:
        payload["user_prompt"] = args.user_prompt_payload

    inbox_task_id: str | None = None
    if args.resolved_priority == "immediate":
        inbox_task_id = _create_notify_inbox_task(
            store=store,
            message_id=message_id,
            subject=subject,
            body=args.body,
            project=args.project,
            actor=args.actor,
            requester_role=args.requester_role,
            label_list=args.label_list,
            notify_kind=notify_kind,
            payload=payload,
        )

    _hold_review_tasks_for_notify(
        actor=args.actor,
        current_session_name=args.current_session_name,
        priority=args.resolved_priority,
        subject=subject,
        body=args.body,
    )

    if args.resolved_priority == "silent":
        typer.echo("silent")
    elif args.resolved_priority == "digest":
        typer.echo(f"digest:{message_id}")
    else:
        typer.echo(str(inbox_task_id or message_id))


def bug_report(
    helpers,
    title: str = typer.Argument(
        ...,
        help=(
            "One-line summary of the system bug you noticed. Used "
            "verbatim as the GitHub issue title — keep it stable "
            "across observations so the helper can dedup repeated "
            "reports."
        ),
    ),
    body: str = typer.Argument(
        ...,
        help=(
            "Full bug description. Markdown supported. Pass '-' to "
            "read from stdin (typical: pipe Polly's longer "
            "explanation in)."
        ),
    ),
    actor: str = typer.Option(
        "polly",
        "--actor",
        help=(
            "Who noticed the bug. Recorded in the audit log and "
            "added as a footer on the GitHub issue body."
        ),
    ),
    project: str = typer.Option(
        "",
        "--project",
        "-p",
        help=(
            "PollyPM project key the bug was observed against. Leave "
            "empty for workspace-level / cross-project bugs."
        ),
    ),
    subject: str = typer.Option(
        "",
        "--subject",
        help=(
            "Free-form forensic subject (task id, session name, "
            "etc.). Recorded in audit metadata only — the issue "
            "title is the user-facing surface."
        ),
    ),
    dedup_window: int = typer.Option(
        3600,
        "--dedup-window-seconds",
        help=(
            "Suppress duplicate issues with the same title filed "
            "within this many seconds. Default 1 hour. Set to 0 to "
            "disable deduplication."
        ),
    ),
) -> None:
    """File a PollyPM self-bug-report as a GitHub issue (#1569).

    Before this command existed, Polly and the heartbeat filed
    system-bug observations via ``pm notify`` with ``--project inbox``.
    Those rows ended up in the user's actionable to-do queue, even
    though every one of them is a meta-bug about PollyPM itself.

    This command routes the observation to a GitHub issue with the
    ``polly-self-report`` label, where it can be prioritized against
    other dev work. The user's inbox stays focused on tasks that need
    a human decision.
    """
    from pollypm.audit.bug_reporter import file_bug_report_detailed

    clean_title = (title or "").strip()
    if not clean_title:
        typer.echo("Error: title must not be empty.", err=True)
        raise typer.Exit(code=1)

    if body == "-":
        body = sys.stdin.read()
    if not (body or "").strip():
        typer.echo(
            "Error: body must not be empty (pass '-' to read from stdin).",
            err=True,
        )
        raise typer.Exit(code=1)

    result = file_bug_report_detailed(
        title=clean_title,
        body=body,
        actor=actor or "polly",
        project=project or "",
        subject=subject or "",
        dedup_window_seconds=max(0, int(dedup_window)),
    )
    if result is None:
        typer.echo(
            "bug_report: gh CLI unavailable or create failed — "
            "observation recorded in the audit log only.",
            err=True,
        )
        raise typer.Exit(code=1)
    if result.created:
        typer.echo(f"filed:#{result.issue_number}")
    else:
        typer.echo(f"deduped:#{result.issue_number}")


def register_session_runtime_commands(app: typer.Typer, *, helpers) -> None:
    app.command(help=helpers._UP_HELP)(_bind_session_command(launch, helpers))
    app.command("rail-daemon")(_bind_session_command(rail_daemon, helpers))
    app.command()(_bind_session_command(reset, helpers))
    app.command(help=helpers._STATUS_HELP)(_bind_session_command(status, helpers))
    app.command(
        help=(
            "Print the launch plan PollyPM would execute for each "
            "configured session — window name, log path, command — "
            "without starting anything."
        ),
    )(_bind_session_command(plan, helpers))
    app.command(help="List currently-open alerts (operator + session faults).")(
        _bind_session_command(alerts, helpers)
    )
    app.command("failover")(failover)
    app.command("debug")(_bind_session_command(debug_command, helpers))
    app.command(
        help=(
            "Print recent supervisor events (heartbeat, send_input, "
            "alerts, recoveries) ordered newest first."
        ),
    )(_bind_session_command(events, helpers))
    app.command(
        help=(
            "Set a lease on a session so other actors won't auto-send "
            "input. Pair with ``release`` when done."
        ),
    )(_bind_session_command(claim, helpers))
    app.command(help="Release the lease set on a session by ``pm claim``.")(
        _bind_session_command(release, helpers)
    )
    app.command(help=helpers._SEND_HELP)(_bind_session_command(send, helpers))
    app.command(help=helpers._NOTIFY_HELP)(_bind_session_command(notify, helpers))
    app.command(
        "bug-report",
        help=(
            "File a PollyPM self-bug-report as a GitHub issue with "
            "the polly-self-report label (#1569). Use this instead "
            "of ``pm notify`` when reporting meta-bugs about PollyPM "
            "itself — the user's inbox stays focused on actionable "
            "tasks."
        ),
    )(_bind_session_command(bug_report, helpers))
