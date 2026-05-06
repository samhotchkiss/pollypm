"""Interactive UI CLI commands.

Contract:
- Inputs: Typer options selecting which TUI surface to launch.
- Outputs: root command registrations on the passed Typer app.
- Side effects: starts Textual / tmux-backed interactive screens.
- Invariants: cockpit launch plumbing is isolated from unrelated CLI
  concerns.
"""

from __future__ import annotations

import logging
import time
import traceback
from datetime import datetime
from pathlib import Path

import typer

from pollypm.config import DEFAULT_CONFIG_PATH

_RIGHT_PANE_BRIDGE_BYPASS_ESCAPE_TOKENS = frozenset({"<esc>", "esc", "escape"})
_LIVE_RIGHT_PANE_INPUT_STICKY = "live_right_pane_input_sticky"
_LIVE_CHAT_NETWORK_DEAD_PROMPT_ACTIVE = "live_chat_network_dead_prompt_active"
_HELP_MODAL_BRIDGE_KIND = "help_modal_bridge_kind"
_HELP_MODAL_SELECTED_KEY = "help_modal_selected_key"
_HELP_MODAL_OPENED_AT = "help_modal_opened_at"
_HELP_MODAL_TTL_SECONDS = 300.0
_HELP_KEY_TOKENS = frozenset({"?", "question_mark"})
_HELP_MODAL_DISMISS_TOKENS = frozenset({
    "?",
    "question_mark",
    "q",
    "<esc>",
    "esc",
    "escape",
})
_HELP_MODAL_CONTROL_TOKENS = frozenset({
    *_HELP_MODAL_DISMISS_TOKENS,
    "j",
    "k",
    "f",
    "b",
    "g",
    "down",
    "<down>",
    "up",
    "<up>",
    "pagedown",
    "<pgdn>",
    "pageup",
    "<pgup>",
    "space",
    "<space>",
    "home",
    "<home>",
    "end",
    "<end>",
})
_HELP_CONTENT_BRIDGE_FALLBACK_KINDS = ("dashboard", "pane-inbox", "settings")
# Keep row-navigation keys on the cockpit bridge. The Inbox pane has its
# own bridge for explicit Inbox actions/filter input, but rail j/k/arrows
# must continue moving the rail cursor when the selected row is Inbox
# (#1246/#1272).
_INBOX_BRIDGE_FIRST_TOKENS = frozenset({
    "/",
    "A",
    "a",
    "d",
})
_INBOX_FILTER_INPUT_TOKENS = frozenset({
    "<bs>",
    "<cr>",
    "<esc>",
    "<space>",
    "backspace",
    "enter",
    "esc",
    "escape",
    "space",
})
_SETTINGS_BRIDGE_FIRST_TOKENS = frozenset({
    "<down>",
    "<tab>",
    "<up>",
    "down",
    "j",
    "k",
    "tab",
    "up",
})
_RIGHT_PANE_TMUX_KEY_TOKENS: dict[str, str] = {
    "<bs>": "BSpace",
    "backspace": "BSpace",
    "<cr>": "Enter",
    "enter": "Enter",
    "<tab>": "Tab",
    "tab": "Tab",
    "<space>": "Space",
    "space": "Space",
    "<up>": "Up",
    "up": "Up",
    "<down>": "Down",
    "down": "Down",
    "<left>": "Left",
    "left": "Left",
    "<right>": "Right",
    "right": "Right",
    "<pgup>": "PageUp",
    "pageup": "PageUp",
    "<pgdn>": "PageDown",
    "pagedown": "PageDown",
    "<home>": "Home",
    "home": "Home",
    "<end>": "End",
    "end": "End",
}
_LIVE_CHAT_NETWORK_DEAD_MESSAGE = (
    "PollyPM chat failed: network unreachable. "
    "Type again to clear this and retry; check connection."
)


def _is_help_key(key: str) -> bool:
    return key.strip().lower() in _HELP_KEY_TOKENS


def _is_help_modal_control_key(key: str) -> bool:
    token = key.strip()
    if token == "G":
        return True
    return token.lower() in _HELP_MODAL_CONTROL_TOKENS


def _is_help_modal_dismiss_key(key: str) -> bool:
    return key.strip().lower() in _HELP_MODAL_DISMISS_TOKENS


def _content_bridge_kind_for_selected_key(selected_key: str) -> str | None:
    """Return the right-pane bridge kind for selected static cockpit views."""
    if selected_key in {"dashboard", "polly"}:
        return "dashboard"
    if selected_key == "inbox" or selected_key.startswith("inbox:"):
        return "pane-inbox"
    if selected_key == "settings":
        return "settings"
    return None


def _remember_help_modal_bridge(
    config_path: Path,
    *,
    kind: str,
    selected_key: str,
) -> None:
    try:
        from pollypm.cockpit_rail import CockpitRouter

        router = CockpitRouter(config_path)
        state = router._load_state()
        if not isinstance(state, dict):
            return
        state[_HELP_MODAL_BRIDGE_KIND] = kind
        state[_HELP_MODAL_SELECTED_KEY] = selected_key
        state[_HELP_MODAL_OPENED_AT] = time.time()
        router._write_state(state)
    except Exception:  # noqa: BLE001
        return


def _clear_help_modal_bridge(config_path: Path) -> None:
    try:
        from pollypm.cockpit_rail import CockpitRouter

        router = CockpitRouter(config_path)
        state = router._load_state()
        if not isinstance(state, dict):
            return
        changed = False
        for key in (
            _HELP_MODAL_BRIDGE_KIND,
            _HELP_MODAL_SELECTED_KEY,
            _HELP_MODAL_OPENED_AT,
        ):
            if key in state:
                state.pop(key, None)
                changed = True
        if changed:
            router._write_state(state)
    except Exception:  # noqa: BLE001
        return


def _recorded_help_modal_bridge(config_path: Path) -> str | None:
    try:
        from pollypm.cockpit_rail import CockpitRouter

        router = CockpitRouter(config_path)
        state = router._load_state()
        if not isinstance(state, dict):
            return None
        kind = state.get(_HELP_MODAL_BRIDGE_KIND)
        selected = state.get(_HELP_MODAL_SELECTED_KEY)
        opened_at = state.get(_HELP_MODAL_OPENED_AT)
        current_selected = router.selected_key()
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(kind, str) or not kind:
        return None
    if not isinstance(selected, str):
        _clear_help_modal_bridge(config_path)
        return None
    if selected != current_selected:
        selected_kind = _content_bridge_kind_for_selected_key(selected)
        current_kind = _content_bridge_kind_for_selected_key(current_selected)
        if selected_kind != kind or current_kind != kind:
            _clear_help_modal_bridge(config_path)
            return None
    if not current_selected:
        _clear_help_modal_bridge(config_path)
        return None
    if not isinstance(opened_at, int | float):
        _clear_help_modal_bridge(config_path)
        return None
    if time.time() - float(opened_at) > _HELP_MODAL_TTL_SECONDS:
        _clear_help_modal_bridge(config_path)
        return None
    return kind


def _send_help_modal_key_to_recorded_bridge(
    config_path: Path,
    key: str,
) -> Path | None:
    """Continue routing help-modal keys to the bridge that opened help."""
    if not _is_help_modal_control_key(key):
        return None
    kind = _recorded_help_modal_bridge(config_path)
    if kind is None:
        return None
    try:
        from pollypm.cockpit_input_bridge import send_key_to_first_live

        delivered = send_key_to_first_live(config_path, key, kind=kind, timeout=0.2)
    except Exception:  # noqa: BLE001
        delivered = None
    if delivered is None:
        _clear_help_modal_bridge(config_path)
        return None
    if _is_help_modal_dismiss_key(key):
        _clear_help_modal_bridge(config_path)
    return delivered


def _send_help_key_to_content_bridge(config_path: Path, key: str) -> Path | None:
    """Deliver ``?`` to the selected content-pane app when it has a bridge."""
    if not _is_help_key(key):
        return None
    try:
        from pollypm.cockpit_input_bridge import send_key_to_first_live
        from pollypm.cockpit_rail import CockpitRouter

        selected = CockpitRouter(config_path).selected_key()
    except Exception:  # noqa: BLE001
        return None
    kind = _content_bridge_kind_for_selected_key(selected)
    candidates = []
    if kind is not None:
        candidates.append(kind)
    if selected in {"dashboard", "polly"}:
        # Home help belongs on the visible dashboard pane when its
        # bridge is live, but it should not fall through to stale
        # non-Home content bridges if that pane is not ready (#1254).
        fallbacks: tuple[str, ...] = ()
    else:
        fallbacks = _HELP_CONTENT_BRIDGE_FALLBACK_KINDS
    candidates.extend(
        fallback
        for fallback in fallbacks
        if fallback not in candidates
    )
    for candidate in candidates:
        try:
            delivered = send_key_to_first_live(
                config_path, key, kind=candidate, timeout=0.2,
            )
        except Exception:  # noqa: BLE001
            delivered = None
        if delivered is not None:
            _remember_help_modal_bridge(
                config_path,
                kind=candidate,
                selected_key=selected,
            )
            return delivered
    return None


def _send_selected_action_key_to_content_bridge(
    config_path: Path, key: str,
) -> Path | None:
    """Deliver selected-pane action keys before live-pane PTY fallback."""
    token = key.strip()
    lowered = token.lower()
    try:
        from pollypm.cockpit_input_bridge import send_key_to_first_live
        from pollypm.cockpit_rail import CockpitRouter

        router = CockpitRouter(config_path)
        selected = router.selected_key()
    except Exception:  # noqa: BLE001
        return None
    kind = _content_bridge_kind_for_selected_key(selected)
    if kind == "pane-inbox" and token in _INBOX_BRIDGE_FIRST_TOKENS:
        pass
    elif (
        kind == "pane-inbox"
        and _inbox_filter_token(token, lowered)
        and router.inbox_filter_input_active()
    ):
        pass
    elif kind == "settings" and lowered in _SETTINGS_BRIDGE_FIRST_TOKENS:
        pass
    else:
        return None
    try:
        return send_key_to_first_live(config_path, key, kind=kind, timeout=0.2)
    except Exception:  # noqa: BLE001
        return None


def _inbox_filter_token(token: str, lowered: str) -> bool:
    if lowered in _INBOX_FILTER_INPUT_TOKENS:
        return True
    return len(token) == 1 and token.isprintable()


def _tmux_event_for_cockpit_key(key: str) -> tuple[str, bool] | None:
    """Return ``(value, literal)`` for forwarding a bridge token to tmux."""
    token = key.strip()
    if not token:
        return None
    lowered = token.lower()
    mapped = _RIGHT_PANE_TMUX_KEY_TOKENS.get(lowered)
    if mapped is not None:
        return mapped, False
    if lowered.startswith("ctrl+") and len(token) > len("ctrl+"):
        return f"C-{token[len('ctrl+'):].lower()}", False
    if lowered.startswith("c-") and len(token) > 2:
        return f"C-{token[2:].lower()}", False
    return key, True


def _live_chat_submit_blocked_by_network_dead(
    config_path: Path,
    key: str,
    *,
    router: object,
    right_pane: str,
) -> bool:
    """Consume the dev network-dead marker on live-chat submit."""
    event = _tmux_event_for_cockpit_key(key)
    if event != ("Enter", False):
        return False
    try:
        from pollypm.dev_network_simulation import (
            SimulatedNetworkDead,
            raise_if_network_dead,
        )

        raise_if_network_dead(config_path, surface="cockpit live chat submit")
    except SimulatedNetworkDead:
        tmux = getattr(router, "tmux", None)
        if tmux is not None:
            run = getattr(tmux, "run", None)
            if callable(run):
                run("send-keys", "-t", right_pane, "C-u", check=False)
            send_keys = getattr(tmux, "send_keys", None)
            if callable(send_keys):
                send_keys(
                    right_pane,
                    _LIVE_CHAT_NETWORK_DEAD_MESSAGE,
                    press_enter=False,
                )
                _set_live_chat_network_dead_prompt_active(router, True)
        return True
    return False


def _set_live_chat_network_dead_prompt_active(router: object, active: bool) -> None:
    try:
        load_state = getattr(router, "_load_state")
        write_state = getattr(router, "_write_state")
        state = load_state()
        if not isinstance(state, dict):
            return
        if active:
            if state.get(_LIVE_CHAT_NETWORK_DEAD_PROMPT_ACTIVE) is True:
                return
            state[_LIVE_CHAT_NETWORK_DEAD_PROMPT_ACTIVE] = True
        else:
            if _LIVE_CHAT_NETWORK_DEAD_PROMPT_ACTIVE not in state:
                return
            state.pop(_LIVE_CHAT_NETWORK_DEAD_PROMPT_ACTIVE, None)
        write_state(state)
    except Exception:  # noqa: BLE001
        return


def _live_chat_network_dead_prompt_active(router: object) -> bool:
    try:
        state = getattr(router, "_load_state")()
    except Exception:  # noqa: BLE001
        return False
    if not isinstance(state, dict):
        return False
    return state.get(_LIVE_CHAT_NETWORK_DEAD_PROMPT_ACTIVE) is True


def _set_live_right_pane_input_sticky(router: object, active: bool) -> None:
    try:
        load_state = getattr(router, "_load_state")
        write_state = getattr(router, "_write_state")
        state = load_state()
        if not isinstance(state, dict):
            return
        if active:
            if state.get(_LIVE_RIGHT_PANE_INPUT_STICKY) is True:
                return
            state[_LIVE_RIGHT_PANE_INPUT_STICKY] = True
        else:
            if _LIVE_RIGHT_PANE_INPUT_STICKY not in state:
                return
            state.pop(_LIVE_RIGHT_PANE_INPUT_STICKY, None)
        write_state(state)
    except Exception:  # noqa: BLE001
        return


def _sticky_live_right_pane_id(router: object) -> str | None:
    try:
        state = getattr(router, "_load_state")()
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(state, dict):
        return None
    if state.get(_LIVE_RIGHT_PANE_INPUT_STICKY) is not True:
        return None
    mounted = state.get("mounted_session")
    right_pane_id = state.get("right_pane_id")
    if not isinstance(mounted, str) or not mounted:
        _set_live_right_pane_input_sticky(router, False)
        return None
    if not isinstance(right_pane_id, str) or not right_pane_id:
        _set_live_right_pane_input_sticky(router, False)
        return None
    try:
        config = getattr(router, "_load_config")()
        cockpit_window = getattr(router, "_COCKPIT_WINDOW", "PollyPM")
        window_target = f"{config.project.tmux_session}:{cockpit_window}"
        panes = router.tmux.list_panes(window_target)
    except Exception:  # noqa: BLE001
        return None
    right_pane = next(
        (pane for pane in panes if getattr(pane, "pane_id", None) == right_pane_id),
        None,
    )
    if right_pane is None or getattr(right_pane, "pane_dead", False):
        _set_live_right_pane_input_sticky(router, False)
        return None
    is_live_provider_pane = getattr(router, "_is_live_provider_pane", None)
    if callable(is_live_provider_pane) and not is_live_provider_pane(right_pane):
        _set_live_right_pane_input_sticky(router, False)
        return None
    return right_pane_id


def _clear_live_chat_network_dead_prompt(
    router: object,
    right_pane: str,
    event: tuple[str, bool],
) -> bool:
    if not _live_chat_network_dead_prompt_active(router):
        return False
    tmux = getattr(router, "tmux", None)
    run = getattr(tmux, "run", None) if tmux is not None else None
    if callable(run):
        run("send-keys", "-t", right_pane, "C-u", check=False)
    _set_live_chat_network_dead_prompt_active(router, False)
    value, literal = event
    return not literal and value in {"Enter", "BSpace"}


def _send_key_to_active_live_right_pane(config_path: Path, key: str) -> str | None:
    """Deliver ``key`` to the focused live right pane, if one owns focus."""
    try:
        from pollypm.cockpit_rail import CockpitRouter

        router = CockpitRouter(config_path)
        right_pane = router.active_live_right_pane_id()
    except Exception:  # noqa: BLE001
        return None
    if key.strip().lower() in _RIGHT_PANE_BRIDGE_BYPASS_ESCAPE_TOKENS:
        if right_pane is None:
            right_pane = _sticky_live_right_pane_id(router)
        if right_pane is not None:
            _clear_live_chat_network_dead_prompt(router, right_pane, ("Escape", False))
        _set_live_right_pane_input_sticky(router, False)
        return None
    if right_pane is None:
        right_pane = _sticky_live_right_pane_id(router)
        if right_pane is None:
            return None
    event = _tmux_event_for_cockpit_key(key)
    if event is None:
        return None
    if _live_chat_submit_blocked_by_network_dead(
        config_path, key, router=router, right_pane=right_pane,
    ):
        return right_pane
    value, literal = event
    if _clear_live_chat_network_dead_prompt(router, right_pane, event):
        _set_live_right_pane_input_sticky(router, True)
        return right_pane
    if literal:
        router.tmux.send_keys(right_pane, value, press_enter=False)
    else:
        router.tmux.run("send-keys", "-t", right_pane, value, check=False)
    _set_live_right_pane_input_sticky(router, True)
    return right_pane


def _install_cockpit_debug_log_handler(config_path: Path) -> None:
    """Attach a ``FileHandler`` so cockpit-side ``logger.info``/``warning``
    calls land in ``~/.pollypm/cockpit_debug.log``.

    Closes #1108: previously only the boot ``--- START ... ---`` banner
    (written directly to the file in ``cockpit()`` below) reached the
    debug log. Library code calling ``logger.info(...)`` /
    ``logger.warning(...)`` had no handler to receive it because the
    cockpit's stdout/stderr is the user's TTY (not a captured pipe like
    ``rail_daemon``'s) and nothing else attached a file sink. This made
    it impossible to validate fixes like #1103 from logs.

    The handler is attached to the root logger at ``INFO`` so any
    ``getLogger(__name__)`` user across cockpit-side code is captured
    without per-module wiring. Idempotent — multiple cockpit-pane
    launches in the same process won't stack handlers.
    """
    debug_log = config_path.parent / "cockpit_debug.log"
    try:
        debug_log.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        # If we can't create the dir, the open() below would fail too;
        # logging is best-effort, never block cockpit boot on it.
        return
    sentinel = "_pollypm_cockpit_debug_log"
    root = logging.getLogger()
    for existing in root.handlers:
        if getattr(existing, sentinel, False):
            return  # already installed in this process
    try:
        handler = logging.FileHandler(debug_log, mode="a", encoding="utf-8")
    except OSError:
        return
    handler.setLevel(logging.INFO)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    )
    setattr(handler, sentinel, True)
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)


def _enforce_migration_gate(config_path: Path) -> None:
    """Refuse-start guard (#717). Best-effort — a missing config is not
    a schema-migration problem and onboarding handles it elsewhere."""
    from pollypm.store import migrations as _migrations

    if _migrations.bypass_env_is_set():
        return
    try:
        from pollypm.config import load_config
        config = load_config(config_path)
    except Exception:  # noqa: BLE001
        return
    _migrations.require_no_pending_or_exit(config.project.state_db)


def _warn_on_plugin_load_errors(config_path: Path) -> None:
    """Emit a stderr WARNING at cockpit boot if any plugin failed to load.

    Closes #960: ``ExtensionHost`` previously recorded plugin load
    failures on ``host.errors`` but no surface read them, so a broken
    plugin (e.g. the ``core_recurring`` relative-import bug from #957)
    silently dropped from the registry — taking its scheduled jobs with
    it — while the operator saw nothing. Now the cockpit prints a
    visible warning at startup so the breakage is immediately
    discoverable. The "broken plugin doesn't crash the cockpit"
    contract is preserved — this is informational only.
    """
    try:
        from pollypm.service_api import collect_plugin_load_errors
        errors = collect_plugin_load_errors(config_path)
    except Exception:  # noqa: BLE001
        return
    if not errors:
        return
    plugin_names = sorted({entry.get("plugin") or "<host>" for entry in errors})
    summary = ", ".join(plugin_names)
    count = len(errors)
    word = "plugin" if count == 1 else "plugins"
    typer.echo(
        f"WARNING: {count} {word} failed to load: {summary}",
        err=True,
    )
    for entry in errors:
        plugin_name = entry.get("plugin") or "<host>"
        message = entry.get("message") or ""
        typer.echo(f"  - {plugin_name}: {message}", err=True)
    typer.echo("  Run `pm status` for the full list.", err=True)


def register_ui_commands(app: typer.Typer) -> None:
    @app.command(help="Launch the standalone Accounts management TUI.")
    def accounts_ui(
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    ) -> None:
        from pollypm.account_tui import AccountsApp

        AccountsApp(config_path).run()

    @app.command(help="Launch the legacy control TUI (predecessor to ``cockpit``).")
    def ui(
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    ) -> None:
        from pollypm.control_tui import PollyPMApp

        PollyPMApp(config_path).run()

    @app.command(
        help=(
            "Launch the cockpit TUI — the main interactive surface "
            "(left rail + scoped right pane) for inspecting projects, "
            "inbox, workers, and activity."
        ),
    )
    def cockpit(
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    ) -> None:
        _enforce_migration_gate(config_path)
        _warn_on_plugin_load_errors(config_path)
        _install_cockpit_debug_log_handler(config_path)
        crash_log = config_path.parent / "cockpit_crash.log"
        debug_log = config_path.parent / "cockpit_debug.log"
        try:
            with open(debug_log, "a") as debug_handle:
                debug_handle.write(f"\n--- START {datetime.now().isoformat()} ---\n")
            from pollypm.cockpit_ui import PollyCockpitApp

            PollyCockpitApp(config_path).run(mouse=True)
            with open(debug_log, "a") as debug_handle:
                debug_handle.write(f"--- CLEAN EXIT {datetime.now().isoformat()} ---\n")
        except Exception:
            with open(crash_log, "a") as crash_handle:
                crash_handle.write(f"\n--- {datetime.now().isoformat()} ---\n")
                traceback.print_exc(file=crash_handle)
            with open(debug_log, "a") as debug_handle:
                debug_handle.write(f"--- CRASH {datetime.now().isoformat()} ---\n")
                traceback.print_exc(file=debug_handle)
            raise

    @app.command(
        "cockpit-pane",
        help=(
            "Launch a single cockpit panel (inbox, project, workers, "
            "metrics, activity, settings) standalone — the same screen "
            "the cockpit's right pane embeds, but full-window."
        ),
    )
    def cockpit_pane(
        kind: str = typer.Argument(..., help="Pane type: inbox, settings, workers, metrics, activity, or project."),
        target: str | None = typer.Argument(None, help="Optional project key for project panes."),
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
        project: str | None = typer.Option(
            None,
            "--project",
            "-p",
            help=(
                "Preload a project filter (currently used by `activity` to "
                "scope the feed to one project)."
            ),
        ),
        task_id: str | None = typer.Option(
            None,
            "--task",
            help="Preselect a task in task-oriented cockpit panes.",
        ),
    ) -> None:
        _enforce_migration_gate(config_path)
        _install_cockpit_debug_log_handler(config_path)
        if kind == "settings" and target:
            from pollypm.cockpit_ui import PollyProjectSettingsApp

            PollyProjectSettingsApp(config_path, target).run(mouse=True)
            return
        if kind == "settings":
            from pollypm.cockpit_ui import PollySettingsPaneApp

            PollySettingsPaneApp(config_path).run(mouse=True)
            return
        if kind in ("polly", "dashboard"):
            from pollypm.cockpit_ui import PollyDashboardApp

            PollyDashboardApp(config_path).run(mouse=True)
            return
        if kind == "inbox":
            from pollypm.cockpit_ui import PollyInboxApp

            # #751 — ``--project <key>`` pre-scopes the inbox to the
            # given project on mount. Used when jumping from a
            # project dashboard so the user sees only that project's
            # items on arrival.
            project_key = project or target
            PollyInboxApp(config_path, initial_project=project_key).run(mouse=True)
            return
        if kind == "workers":
            from pollypm.cockpit_ui import PollyWorkerRosterApp

            PollyWorkerRosterApp(config_path).run(mouse=True)
            return
        if kind == "metrics":
            from pollypm.cockpit_ui import PollyMetricsApp

            PollyMetricsApp(config_path).run(mouse=True)
            return
        if kind == "issues" and target:
            from pollypm.cockpit_tasks import PollyTasksApp

            PollyTasksApp(
                config_path,
                target,
                initial_task_id=task_id,
            ).run(mouse=True)
            return
        if kind == "activity":
            from pollypm.cockpit_ui import PollyActivityFeedApp

            project_key = project or target
            PollyActivityFeedApp(config_path, project_key=project_key).run(mouse=True)
            return
        if kind == "project" and target:
            from pollypm.cockpit_ui import PollyProjectDashboardApp

            PollyProjectDashboardApp(config_path, target).run(mouse=True)
            return
        from pollypm.cockpit_ui import PollyCockpitPaneApp

        PollyCockpitPaneApp(config_path, kind, target).run(mouse=True)

    @app.command(
        "cockpit-send-key",
        help=(
            "Send a keystroke to the running cockpit via its TTY-less "
            "input bridge (#1109 follow-up). Works even when no tmux "
            "client is attached. Pass single chars (`I`, `j`) or "
            "tokens (`<bs>`, `<cr>`, `<esc>`, `<tab>`, `<space>`, "
            "`<up>`, `<down>`, `<left>`, `<right>`, `<pgup>`, `<pgdn>`,"
            " `<home>`, `<end>`)."
        ),
    )
    def cockpit_send_key(
        key: str = typer.Argument(
            ...,
            help=(
                "Keystroke token. Single chars (`I`) pass through; "
                "use `<bs>` etc. for special keys. Modifiers like "
                "`ctrl+x` are forwarded verbatim to Textual."
            ),
        ),
        config_path: Path = typer.Option(
            DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."
        ),
        kind: str = typer.Option(
            "cockpit",
            "--kind",
            "-k",
            help=(
                "Restrict delivery to a specific cockpit surface "
                "(`cockpit`, `dashboard`, `pane-inbox`, …). Defaults to "
                "`cockpit` so stale dashboard sockets cannot steal rail input."
            ),
        ),
    ) -> None:
        from pollypm.cockpit_input_bridge import send_key_to_first_live

        if kind == "cockpit":
            delivered_to_content = _send_help_modal_key_to_recorded_bridge(
                config_path, key,
            )
            if delivered_to_content is not None:
                typer.echo(f"Delivered {key!r} via {delivered_to_content}")
                return
            delivered_to_content = _send_selected_action_key_to_content_bridge(
                config_path, key,
            )
            if delivered_to_content is not None:
                typer.echo(f"Delivered {key!r} via {delivered_to_content}")
                return
            delivered_to_right = _send_key_to_active_live_right_pane(
                config_path, key,
            )
            if delivered_to_right is not None:
                typer.echo(
                    f"Delivered {key!r} via cockpit right pane {delivered_to_right}"
                )
                return
            delivered_to_content = _send_help_key_to_content_bridge(
                config_path, key,
            )
            if delivered_to_content is not None:
                typer.echo(f"Delivered {key!r} via {delivered_to_content}")
                return

        delivered_to = send_key_to_first_live(config_path, key, kind=kind)
        if delivered_to is None:
            typer.echo(
                "No live cockpit input bridge found. Either no cockpit "
                "is running, or it predates the #1109 follow-up "
                "(restart the cockpit to pick up the bridge).",
                err=True,
            )
            raise typer.Exit(code=1)
        typer.echo(f"Delivered {key!r} via {delivered_to}")
