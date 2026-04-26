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

import json
import re
import sys
from pathlib import Path

import typer

from pollypm.config import DEFAULT_CONFIG_PATH

_TASK_ID_PATTERN = re.compile(r"\b([A-Za-z0-9_.-]+/\d+)\b")

# Dispatch identifiers the cockpit dashboard's
# ``_perform_dashboard_action`` knows how to route. Producers that
# emit user_prompt actions with kinds outside this set hit the
# generic record-response fallback, which is almost never the
# intended behaviour. The set lives here so the producer-side
# validator catches typos before the message lands in the store.
_USER_PROMPT_ACTION_KINDS: frozenset[str] = frozenset({
    "review_plan",
    "open_task",
    "open_inbox",
    "discuss_pm",
    "approve_task",
    "record_response",
})


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
    try:
        from pollypm.config import load_config
        from pollypm.session_services import create_tmux_client

        tmux = create_tmux_client()
        tmux_session = tmux.current_session_name()
        window_index = tmux.current_window_index()
        if not tmux_session or window_index is None:
            return actor, None
        window_name = None
        for window in tmux.list_windows(tmux_session):
            if str(getattr(window, "index", "")) == str(window_index):
                window_name = getattr(window, "name", None)
                break
        if not window_name:
            return actor, None
        config = load_config(config_path)
        sessions = getattr(config, "sessions", {}) or {}
        for session_name, session_cfg in sessions.items():
            expected = getattr(session_cfg, "window_name", None) or session_name
            if expected == window_name:
                return session_name, session_name
    except Exception:  # noqa: BLE001
        return actor, None
    return actor, None


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


def register_session_runtime_commands(app: typer.Typer, *, helpers) -> None:
    @app.command(help=helpers._UP_HELP)
    def launch(
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    ) -> None:
        helpers.up(config_path=config_path)

    @app.command("rail-daemon")
    def rail_daemon(
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
        poll_interval: float = typer.Option(60.0, "--poll-interval", help="Seconds between idle-loop wakeups."),
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

    @app.command()
    def reset(
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
        force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation prompt."),
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
            name for name in [session_name, storage_name]
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
        jobs_path = supervisor.config.project.base_dir / "scheduler" / "jobs.json"
        jobs_path.unlink(missing_ok=True)
        cockpit_state = supervisor.config.project.base_dir / "cockpit_state.json"
        cockpit_state.unlink(missing_ok=True)
        try:
            from sqlalchemy import delete

            from pollypm.store.schema import messages

            supervisor.store.execute("DELETE FROM leases")
            supervisor.store.execute("DELETE FROM session_runtime")
            supervisor.msg_store.execute(
                delete(messages).where(
                    messages.c.type == "alert",
                    messages.c.state == "open",
                )
            )
            supervisor.store.commit()
        except Exception:  # noqa: BLE001
            pass
        session_word = "session" if len(sessions_to_kill) == 1 else "sessions"
        typer.echo(
            f"Killed {len(sessions_to_kill)} {session_word}: "
            f"{', '.join(sessions_to_kill)}"
        )

    @app.command(help=helpers._STATUS_HELP)
    def status(
        session_name: str | None = typer.Argument(None, help="Optional session name from config."),
        json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    ) -> None:
        if not helpers._config_option_was_explicit():
            config_path = helpers._discover_config_path(config_path)
        from pollypm.service_api import PollyPMService

        payload = PollyPMService(config_path).session_status(session_name)
        sessions = payload["sessions"]
        if session_name is not None and not sessions:
            raise typer.BadParameter(f"Unknown session: {session_name}")
        if json_output:
            helpers._emit_json(payload)
            return
        if not sessions:
            typer.echo("No sessions configured.")
            return
        typer.echo(f"Config: {payload['config_path']}")
        for item in sessions:
            typer.echo(
                f"- {item['name']}: status={item['status']} running={'yes' if item['running'] else 'no'} "
                f"alerts={item['alert_count']} lease={item['lease_owner'] or '-'} "
                f"project={item['project']} role={item['role']}"
            )
            if item["last_failure_message"]:
                typer.echo(f"  reason={item['last_failure_message']}")
        for error in payload["errors"]:
            typer.echo(f"- error: {error}")

    @app.command(
        help=(
            "Print the launch plan PollyPM would execute for each "
            "configured session — window name, log path, command — "
            "without starting anything."
        ),
    )
    def plan(
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    ) -> None:
        supervisor = helpers._load_supervisor(config_path)
        helpers._require_pollypm_session(supervisor)
        for launch in supervisor.plan_launches():
            typer.echo(f"[{launch.session.name}]")
            typer.echo(f"window = {launch.window_name}")
            typer.echo(f"log = {launch.log_path}")
            typer.echo(f"command = {launch.command}")
            typer.echo("")

    @app.command(help="List currently-open alerts (operator + session faults).")
    def alerts(
        json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
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
            typer.echo(f"- #{alert.alert_id} {alert.severity} {alert.session_name}/{alert.alert_type}: {alert.message}")

    @app.command("failover")
    def failover(
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    ) -> None:
        """Show failover configuration: controller account and failover order."""
        from pollypm.config import load_config

        config = load_config(config_path)
        typer.echo(f"Controller: {config.pollypm.controller_account}")
        typer.echo(f"Failover enabled: {'yes' if config.pollypm.failover_enabled else 'no'}")
        if config.pollypm.failover_accounts:
            typer.echo("Failover order:")
            for index, name in enumerate(config.pollypm.failover_accounts, 1):
                account = config.accounts.get(name)
                label = f"{account.email} [{account.provider.value}]" if account else name
                typer.echo(f"  {index}. {label}")
        else:
            typer.echo("No failover accounts configured.")

    @app.command("debug")
    def debug_command(
        session: str | None = typer.Option(None, "--session", "-s", help="Filter to a specific session."),
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    ) -> None:
        """Show diagnostic info: open alerts, session states, recent events. Works outside tmux."""
        supervisor = helpers._load_supervisor(config_path)

        all_alerts = supervisor.open_alerts()
        alerts_list = [alert for alert in all_alerts if session is None or alert.session_name == session]
        typer.echo(f"Open alerts: {len(alerts_list)}")
        for alert in alerts_list:
            typer.echo(f"  {alert.severity} {alert.session_name}/{alert.alert_type}: {alert.message}")

        typer.echo("")
        launches = supervisor.plan_launches()
        windows = supervisor.window_map()
        for launch in launches:
            if session is not None and launch.session.name != session:
                continue
            window = windows.get(launch.window_name)
            if window is None:
                state = "not running"
            elif window.pane_dead:
                state = "dead"
            else:
                state = f"running ({window.pane_current_command})"
            typer.echo(f"  {launch.session.name}: {state} [{launch.session.provider.value}/{launch.account.name}]")

        typer.echo("")
        events_list = supervisor.store.recent_events(limit=5)
        if session is not None:
            events_list = [event for event in events_list if event.session_name == session]
        typer.echo(f"Recent events: {len(events_list)}")
        for event in events_list[:5]:
            typer.echo(f"  {event.created_at} {event.session_name}/{event.event_type}: {event.message}")

    @app.command(
        help=(
            "Print recent supervisor events (heartbeat, send_input, "
            "alerts, recoveries) ordered newest first."
        ),
    )
    def events(
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
        limit: int = typer.Option(20, "--limit", min=1, max=200, help="Maximum number of events to show."),
    ) -> None:
        supervisor = helpers._load_supervisor(config_path)
        helpers._require_pollypm_session(supervisor)
        items = supervisor.store.recent_events(limit=limit)
        if not items:
            typer.echo("No events recorded.")
            return
        for event in items:
            typer.echo(f"- {event.created_at} {event.session_name}/{event.event_type}: {event.message}")

    @app.command(
        help=(
            "Set a lease on a session so other actors won't auto-send "
            "input. Pair with ``release`` when done."
        ),
    )
    def claim(
        session_name: str = typer.Argument(..., help="Session name from config."),
        owner: str = typer.Option("human", "--owner", help="Lease owner label."),
        note: str = typer.Option("", "--note", help="Optional note for the lease."),
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    ) -> None:
        supervisor = helpers._load_supervisor(config_path)
        helpers._require_pollypm_session(supervisor)
        supervisor.claim_lease(session_name, owner, note)
        typer.echo(f"Lease set on {session_name} for {owner}")

    @app.command(help="Release the lease set on a session by ``pm claim``.")
    def release(
        session_name: str = typer.Argument(..., help="Session name from config."),
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    ) -> None:
        supervisor = helpers._load_supervisor(config_path)
        helpers._require_pollypm_session(supervisor)
        supervisor.release_lease(session_name)
        typer.echo(f"Lease released for {session_name}")

    @app.command(help=helpers._SEND_HELP)
    def send(
        session_name: str = typer.Argument(..., help="Session name from config."),
        text: str = typer.Argument(..., help="Text to send into the tmux pane."),
        owner: str = typer.Option("pollypm", "--owner", help="Sender label for lease checks."),
        force: bool = typer.Option(False, "--force", help="Bypass a conflicting lease."),
        no_enter: bool = typer.Option(False, "--no-enter", help="Do not send Enter after the text."),
        json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    ) -> None:
        supervisor = helpers._load_supervisor(config_path)
        session_cfg = supervisor.config.sessions.get(session_name)
        if session_cfg and session_cfg.role == "worker" and not force:
            project = session_cfg.project or session_name.replace("worker_", "", 1)
            typer.echo(
                f"Blocked: dispatch work through the task system.\n"
                f"  pm task create \"Title\" -p {project} -d \"description\" "
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

    @app.command(help=helpers._NOTIFY_HELP)
    def notify(
        subject: str = typer.Argument(..., help="Short title for the inbox item."),
        body: str = typer.Argument(..., help="Message body. Pass '-' to read from stdin."),
        actor: str = typer.Option("polly", "--actor", help="Who is posting the notification."),
        project: str = typer.Option(
            "inbox", "--project", "-p",
            help="Project namespace for the notification task (default: 'inbox').",
        ),
        priority: str = typer.Option(
            "auto", "--priority",
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
                "{\"summary\": str, \"steps\": [str], \"question\": str, "
                "\"actions\": [{\"label\": str, \"kind\": str, ...}]}."
            ),
        ),
        channel: str = typer.Option(
            "inbox", "--channel",
            help=(
                "Delivery channel. ``inbox`` (default) = real user-facing "
                "notification that surfaces in ``pm inbox`` and the cockpit. "
                "``dev`` = developer / test-harness traffic that stays in "
                "the store for debugging but is hidden from the default "
                "inbox view. Use ``dev`` in tests and one-off scripts so "
                "they never pollute the real signal (#754)."
            ),
        ),
        db: str = typer.Option(
            ".pollypm/state.db", "--db",
            help="Path to SQLite database (default: same resolution as `pm inbox`).",
        ),
    ) -> None:
        """Create a work-service inbox item for the human user."""
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
            resolved_config_path, actor,
        )

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
        user_prompt_payload: dict[str, object] | None = None
        if user_prompt_json.strip():
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
                parsed_prompt.get("steps")
                or parsed_prompt.get("required_actions")
                or []
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
            user_prompt_payload = parsed_prompt

        # Surface the contract gap at producer time — without this,
        # operators discover hours later (in the dashboard or via
        # release_invariants) that the Action Needed card fell back to
        # heuristic body parsing because no ``--user-prompt-json`` was
        # passed. The release invariant ``user_action_message_missing_
        # user_prompt`` is the scan-side companion; this is the
        # emit-side reminder. We warn (don't reject) so existing
        # callers in scripts / older role prompts keep working —
        # tightening to a hard error is a separate decision.
        if (
            user_prompt_payload is None
            and resolved_priority == "immediate"
            and requester_role == "user"
            and channel_name == "inbox"
        ):
            typer.echo(
                "Warning: posting an immediate-priority user-facing "
                "notify without --user-prompt-json. The dashboard's "
                "Action Needed card will fall back to body heuristics "
                "and lose structured steps + decision question + "
                "contextual buttons. Pass --user-prompt-json '{...}' "
                "with at least one of summary/steps/question for a "
                "first-class action surface.",
                err=True,
            )

        from pollypm.store import SQLAlchemyStore
        from pollypm.work.cli import _resolve_db_path

        db_path = _resolve_db_path(db, project=project)
        store = SQLAlchemyStore(f"sqlite:///{db_path}")
        tier_state = {
            "immediate": "open",
            "digest": "staged",
            "silent": "closed",
        }[resolved_priority]

        payload = {
            "actor": actor,
            "project": project,
            "milestone_key": milestone_key,
            "requester": requester_role,
        }
        if user_prompt_payload is not None:
            payload["user_prompt"] = user_prompt_payload

        try:
            message_id = store.enqueue_message(
                type="notify",
                tier=resolved_priority,
                recipient=requester_role,
                sender=actor,
                subject=subject,
                body=body,
                scope=project,
                labels=label_list or None,
                payload=payload,
                state="closed" if resolved_priority == "immediate" else tier_state,
            )
        except Exception as exc:  # noqa: BLE001
            typer.echo(f"Failed to enqueue notify message: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        finally:
            store.close()

        inbox_task_id: str | None = None
        if resolved_priority == "immediate":
            from pollypm.work.sqlite_service import SQLiteWorkService

            svc = SQLiteWorkService(
                db_path=db_path,
                project_path=db_path.parent.parent,
            )
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
                )
                inbox_task_id = task.task_id
                store = SQLAlchemyStore(f"sqlite:///{db_path}")
                try:
                    store.update_message(
                        message_id,
                        payload={**payload, "task_id": inbox_task_id},
                    )
                finally:
                    store.close()
            except Exception as exc:  # noqa: BLE001
                typer.echo(f"Failed to create inbox task: {exc}", err=True)
                raise typer.Exit(code=1) from exc
            finally:
                svc.close()

        _hold_review_tasks_for_notify(
            actor=actor,
            current_session_name=current_session_name,
            priority=resolved_priority,
            subject=subject,
            body=body,
        )

        if resolved_priority == "silent":
            typer.echo("silent")
        elif resolved_priority == "digest":
            typer.echo(f"digest:{message_id}")
        else:
            typer.echo(str(inbox_task_id or message_id))
