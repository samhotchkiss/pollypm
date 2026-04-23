"""Tmux-based session service implementation.

This is the default SessionService plugin. It manages agent sessions
as tmux windows/panes within a storage-closet tmux session.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from pollypm.session_services.base import (
    SessionCreatedEvent,
    SessionHandle,
    SessionHealth,
    TranscriptStream,
    dispatch_session_event,
)
from pollypm.tmux.client import TmuxClient

logger = logging.getLogger(__name__)

# Suffix appended to the main tmux session name for the storage closet.
_STORAGE_CLOSET_SUFFIX = "-storage-closet"

# Console window name in the main tmux session.
_CONSOLE_WINDOW = "PollyPM"


class TmuxSessionService:
    """Tmux-backed session service — the default implementation.

    All session mechanics live here: window creation, pane capture,
    stabilization, message sending, snapshot writing.  Policy decisions
    (recovery, failover, leases) stay in the supervisor.
    """

    name = "tmux"

    # ------------------------------------------------------------------
    # Static bootstrap methods — usable before a full service is created
    # ------------------------------------------------------------------

    @staticmethod
    def probe_tmux_session(name: str) -> bool:
        from pollypm.tmux.client import TmuxClient
        return TmuxClient().has_session(name)

    @staticmethod
    def attach_tmux_session(name: str) -> int:
        from pollypm.tmux.client import TmuxClient
        return TmuxClient().attach_session(name)

    @staticmethod
    def switch_tmux_client(name: str) -> int:
        from pollypm.tmux.client import TmuxClient
        return TmuxClient().switch_client(name)

    @staticmethod
    def current_tmux_session() -> str | None:
        from pollypm.tmux.client import TmuxClient
        return TmuxClient().current_session_name()

    def __init__(self, *, config: object, store: object) -> None:
        # Accept config and store as opaque objects to avoid circular
        # imports at module level.  At runtime these are PollyPMConfig
        # and StateStore.
        self._config = config
        self._store = store
        self.tmux = TmuxClient()

    # ------------------------------------------------------------------
    # Naming helpers
    # ------------------------------------------------------------------

    def storage_closet_session_name(self) -> str:
        return f"{self._config.project.tmux_session}{_STORAGE_CLOSET_SUFFIX}"

    def _all_tmux_session_names(self) -> list[str]:
        names = [self._config.project.tmux_session]
        storage = self.storage_closet_session_name()
        if storage not in names:
            names.append(storage)
        return names

    # ------------------------------------------------------------------
    # Protocol: create
    # ------------------------------------------------------------------

    def create(
        self,
        name: str,
        provider: str,
        account: str,
        cwd: Path,
        prompt: str | None = None,
        *,
        command: str | None = None,
        window_name: str | None = None,
        log_path: Path | None = None,
        on_status: Callable[[str], None] | None = None,
        tmux_session: str | None = None,
        stabilize: bool = True,
        initial_input: str | None = None,
        fresh_launch_marker: Path | None = None,
        resume_marker: Path | None = None,
        session_role: str | None = None,
        task_title: str | None = None,
        task_description: str | None = None,
        guide_reference: str | None = None,
        user_id: str = "operator",
    ) -> SessionHandle:
        wname = window_name or f"session-{name}"
        tsession = tmux_session or self.storage_closet_session_name()
        cmd = command or f"echo 'No command for {name}'"

        # Check if already exists
        existing = self._find_window(tsession, wname)
        if existing is not None:
            handle = SessionHandle(
                name=name,
                provider=provider,
                account=account,
                window_name=wname,
                pane_id=existing,
                tmux_session=tsession,
                cwd=str(cwd),
                log_path=log_path,
            )
            # #246: the session is already up, but re-fire the lifecycle
            # event so subscribers (task-assignment resume pings, etc.)
            # see the "session is ready now" signal after a supervisor
            # restart that re-attaches to an existing window.
            self._emit_session_created(
                name=name,
                provider=provider,
                session_role=session_role,
            )
            return handle

        _status = on_status or (lambda _msg: None)
        _status(f"Creating tmux window for {name}...")

        # #243: new-session → new-window fallback. If has_session
        # reports False but new-session then fails (TOCTOU race, exotic
        # tmux state), check again and fall through to new-window
        # rather than raising — same end state either way.
        import subprocess as _subprocess
        if not self.tmux.has_session(tsession):
            try:
                self.tmux.create_session(tsession, wname, cmd)
            except _subprocess.CalledProcessError:
                if self.tmux.has_session(tsession):
                    logger.info(
                        "create: new-session raced for %r; using new-window",
                        tsession,
                    )
                    self.tmux.create_window(tsession, wname, cmd, detached=True)
                else:
                    raise
            target = f"{tsession}:{wname}"
        else:
            self.tmux.create_window(tsession, wname, cmd, detached=True)
            target = f"{tsession}:{wname}"

        self.tmux.set_window_option(target, "allow-passthrough", "on")
        self.tmux.set_pane_history_limit(target, 200)
        if log_path:
            self.tmux.pipe_pane(target, log_path)

        pane_id = self._find_window(tsession, wname)

        # Stabilize (dismiss provider prompts, detect ready state)
        if stabilize:
            self._stabilize(
                provider,
                target,
                name,
                on_status=on_status,
                account_name=account,
            )

        # Send initial input if this is a fresh launch
        if initial_input and fresh_launch_marker and fresh_launch_marker.exists():
            if session_role in {"heartbeat-supervisor", "operator-pm", "reviewer", "triage", "worker"}:
                # M05: prepend a "What you should know" section with
                # recalled memories before the persona prompt is written
                # to disk. When the memory backend isn't available (or
                # no relevant memories surface), the helper returns the
                # prompt unchanged — so a brand-new project boots with
                # exactly today's behavior.
                injected_input = self._inject_memory_into_prompt(
                    initial_input=initial_input,
                    session_role=session_role,
                    task_title=task_title,
                    task_description=task_description,
                    guide_reference=guide_reference,
                    user_id=user_id,
                )
                kickoff = self._prepare_initial_input(
                    name,
                    injected_input,
                    expected_window=wname,
                    session_role=session_role,
                )
                time.sleep(0.5)
                self.tmux.send_keys(target, kickoff)
                self._verify_input_submitted(target, kickoff, provider)
                fresh_launch_marker.unlink(missing_ok=True)

        # Write resume marker
        if resume_marker:
            resume_marker.parent.mkdir(parents=True, exist_ok=True)
            resume_marker.write_text(
                datetime.now(UTC).isoformat().replace("+00:00", "Z") + "\n"
            )

        handle = SessionHandle(
            name=name,
            provider=provider,
            account=account,
            window_name=wname,
            pane_id=pane_id,
            tmux_session=tsession,
            cwd=str(cwd),
            log_path=log_path,
        )

        # #246: notify subscribers that a new session is stable and ready
        # to receive messages. Plugins use this to replay pings that would
        # otherwise wait on the next sweeper cycle (e.g. resume-work pings
        # for an in_progress task whose worker session just came back).
        self._emit_session_created(
            name=name,
            provider=provider,
            session_role=session_role,
        )

        return handle

    # ------------------------------------------------------------------
    # Internal: session lifecycle event dispatch (#246)
    # ------------------------------------------------------------------

    def _emit_session_created(
        self,
        *,
        name: str,
        provider: str,
        session_role: str | None,
    ) -> None:
        """Emit a ``SessionCreatedEvent`` — best-effort, never raises.

        The ``project`` field is resolved from the configured project
        name; the ``role`` field is the caller's ``session_role`` kwarg
        (empty string when unspecified — subscribers treat that as
        "don't know the role, skip role-specific lookups").
        """
        try:
            project = getattr(self._config.project, "name", "") or ""
        except Exception:  # noqa: BLE001
            project = ""
        try:
            dispatch_session_event(
                SessionCreatedEvent(
                    name=name,
                    role=(session_role or "").strip(),
                    project=str(project),
                    provider=provider,
                )
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "tmux: session.created dispatch failed for %s", name, exc_info=True,
            )

    # ------------------------------------------------------------------
    # Protocol: destroy
    # ------------------------------------------------------------------

    def destroy(self, name: str) -> None:
        handle = self.get(name)
        if handle is None:
            return
        target = f"{handle.tmux_session}:{handle.window_name}"
        try:
            self.tmux.kill_window(target)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # Protocol: get / list
    # ------------------------------------------------------------------

    def get(self, name: str) -> SessionHandle | None:
        for handle in self.list():
            if handle.name == name:
                return handle
        return None

    def list(self) -> list[SessionHandle]:
        our_sessions = set(self._all_tmux_session_names())
        windows: dict[str, object] = {}
        for window in self.tmux.list_all_windows():
            if window.session in our_sessions:
                windows[window.name] = window

        # Merge cockpit-mounted sessions
        mounted = self._mounted_window_override()
        if mounted is not None:
            windows[mounted.name] = mounted

        # Build handles from StateStore session records
        handles: list[SessionHandle] = []
        try:
            sessions = self._store.list_sessions()
        except Exception:  # noqa: BLE001
            sessions = []

        for session in sessions:
            window = windows.get(session.window_name)
            handles.append(SessionHandle(
                name=session.name,
                provider=session.provider,
                account=session.account,
                window_name=session.window_name,
                pane_id=getattr(window, "pane_id", None) if window else None,
                tmux_session=getattr(window, "session", self.storage_closet_session_name()) if window else self.storage_closet_session_name(),
                cwd=session.cwd,
            ))
        return handles

    # ------------------------------------------------------------------
    # Protocol: health
    # ------------------------------------------------------------------

    def health(self, name: str, *, capture_lines: int = 200) -> SessionHealth:
        handle = self.get(name)
        if handle is None or handle.pane_id is None:
            return SessionHealth(
                window_present=False,
                pane_alive=False,
                pane_dead=True,
                pane_command=None,
                pane_text="",
            )
        target = handle.pane_id
        try:
            alive = self.tmux.is_pane_alive(target)
        except Exception:  # noqa: BLE001
            alive = False
        try:
            text = self.tmux.capture_pane(target, lines=capture_lines)
        except Exception:  # noqa: BLE001
            text = ""

        # Get pane command
        pane_cmd = None
        try:
            panes = self.tmux.list_panes(f"{handle.tmux_session}:{handle.window_name}")
            for p in panes:
                if p.pane_id == target:
                    pane_cmd = p.pane_current_command
                    break
        except Exception:  # noqa: BLE001
            pass

        return SessionHealth(
            window_present=True,
            pane_alive=alive,
            pane_dead=not alive,
            pane_command=pane_cmd,
            pane_text=text,
        )

    # ------------------------------------------------------------------
    # Protocol: is_turn_active
    # ------------------------------------------------------------------

    def is_turn_active(self, name: str) -> bool:
        h = self.health(name)
        if not h.pane_alive:
            return False
        lowered = h.pane_text.lower()
        # Claude Code: active turn shows progress indicators
        if "⏺" in h.pane_text or "working" in lowered:
            return True
        # Codex: active turn shows "working (" with "esc to interrupt"
        if "working (" in lowered and "esc to interrupt" in lowered:
            return True
        return False

    # ------------------------------------------------------------------
    # Protocol: capture
    # ------------------------------------------------------------------

    def capture(self, name: str, lines: int = 200) -> str:
        handle = self.get(name)
        if handle is None or handle.pane_id is None:
            return ""
        try:
            return self.tmux.capture_pane(handle.pane_id, lines=lines)
        except Exception:  # noqa: BLE001
            return ""

    # ------------------------------------------------------------------
    # Protocol: send
    # ------------------------------------------------------------------

    def send(self, name: str, text: str, *, press_enter: bool = True) -> None:
        handle = self.get(name)
        if handle is None:
            raise RuntimeError(f"Session '{name}' not found")

        target = self._resolve_send_target(handle)
        self.tmux.send_keys(target, text, press_enter=press_enter)

        # Codex CLI buffers input — send a second Enter after a short delay
        if press_enter and handle.provider == "codex":
            time.sleep(0.3)
            self.tmux.send_keys(target, "", press_enter=True)

        if press_enter:
            self._verify_input_submitted(target, text, handle.provider)

    # ------------------------------------------------------------------
    # Protocol: transcript
    # ------------------------------------------------------------------

    def transcript(self, name: str) -> TranscriptStream | None:
        handle = self.get(name)
        if handle is None or handle.log_path is None:
            return None
        log = Path(handle.log_path)
        if not log.exists():
            return None
        return TranscriptStream(path=log)

    # ------------------------------------------------------------------
    # Protocol: switch_account
    # ------------------------------------------------------------------

    def switch_account(
        self,
        name: str,
        new_account: str,
        new_provider: str,
        *,
        command: str | None = None,
        cwd: Path | None = None,
        prompt: str | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> SessionHandle:
        old = self.get(name)
        if old is not None:
            self.destroy(name)

        return self.create(
            name=name,
            provider=new_provider,
            account=new_account,
            cwd=cwd or Path(old.cwd if old else "."),
            prompt=prompt,
            command=command,
            window_name=old.window_name if old else None,
            log_path=old.log_path if old else None,
            on_status=on_status,
        )

    # ------------------------------------------------------------------
    # Snapshot writing (used by heartbeat)
    # ------------------------------------------------------------------

    def write_snapshot(self, name: str, snapshot_lines: int = 200) -> tuple[Path, str]:
        """Capture pane text and write to a timestamped snapshot file."""
        handle = self.get(name)
        if handle is None or handle.pane_id is None:
            return Path("/dev/null"), ""
        target = handle.pane_id
        content = self.tmux.capture_pane(target, lines=snapshot_lines)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        snapshot_path = self._config.project.snapshots_dir / f"{handle.window_name}-{stamp}.txt"
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_text(content)
        return snapshot_path, content

    # ------------------------------------------------------------------
    # Internal: window lookup
    # ------------------------------------------------------------------

    def _find_window(self, tmux_session: str, window_name: str) -> str | None:
        """Find a pane ID for a window by name. Returns None if not found."""
        try:
            for w in self.tmux.list_windows(tmux_session):
                if w.name == window_name:
                    return w.pane_id
        except Exception:  # noqa: BLE001
            pass
        return None

    def _mounted_window_override(self) -> object | None:
        """Check if a session is mounted in the cockpit right pane."""
        from pollypm.tmux.client import TmuxWindow

        state_path = self._config.project.base_dir / "cockpit_state.json"
        if not state_path.exists():
            return None
        try:
            data = json.loads(state_path.read_text())
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(data, dict):
            return None
        mounted_session = data.get("mounted_session")
        if not isinstance(mounted_session, str) or not mounted_session:
            return None

        # Look up the window name for the mounted session from state store
        try:
            sessions = self._store.list_sessions()
            session_rec = next((s for s in sessions if s.name == mounted_session), None)
        except Exception:  # noqa: BLE001
            return None
        if session_rec is None:
            return None

        target = f"{self._config.project.tmux_session}:{_CONSOLE_WINDOW}"
        try:
            panes = self.tmux.list_panes(target)
        except Exception:  # noqa: BLE001
            return None
        if len(panes) < 2:
            return None
        right_pane = max(panes, key=lambda pane: pane.pane_left)
        return TmuxWindow(
            session=self._config.project.tmux_session,
            index=0,
            name=session_rec.window_name,
            active=True,
            pane_id=right_pane.pane_id,
            pane_current_command=right_pane.pane_current_command,
            pane_current_path=right_pane.pane_current_path,
            pane_dead=right_pane.pane_dead,
        )

    # ------------------------------------------------------------------
    # Internal: send target resolution
    # ------------------------------------------------------------------

    def _resolve_send_target(self, handle: SessionHandle) -> str:
        """Find the actual tmux target for sending to a session.

        Checks storage closet first, then cockpit mount.
        """
        storage = self.storage_closet_session_name()
        if self.tmux.has_session(storage):
            windows = {w.name for w in self.tmux.list_windows(storage)}
            if handle.window_name in windows:
                return f"{storage}:{handle.window_name}"

        # Check cockpit mount
        cockpit_session = self._config.project.tmux_session
        state_path = self._config.project.base_dir / "cockpit_state.json"
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text())
                if state.get("mounted_session") == handle.name:
                    right_pane = state.get("right_pane_id")
                    if right_pane:
                        try:
                            cockpit_window = f"{cockpit_session}:{_CONSOLE_WINDOW}"
                            panes = self.tmux.list_panes(cockpit_window)
                            if any(p.pane_id == right_pane for p in panes):
                                return right_pane
                        except Exception:  # noqa: BLE001
                            pass
                        state.pop("right_pane_id", None)
                        state.pop("mounted_session", None)
                        from pollypm.atomic_io import atomic_write_json
                        atomic_write_json(state_path, state)
            except Exception:  # noqa: BLE001
                pass

        raise RuntimeError(
            f"Session '{handle.name}' (window '{handle.window_name}') not found in "
            f"storage closet or cockpit."
        )

    def _verify_input_submitted(
        self,
        target: str,
        text: str,
        provider: str,
        max_retries: int = 3,
    ) -> None:
        """Check that sent text left the input bar; press Enter again if stuck."""
        check_text = text[:60].strip()
        if not check_text:
            return
        for _attempt in range(max_retries):
            time.sleep(0.4)
            try:
                snapshot = self.tmux.capture_pane(target, lines=5)
            except Exception:  # noqa: BLE001
                return
            lines = snapshot.strip().splitlines()
            if not lines:
                return
            last_lines = "\n".join(lines[-3:])
            if check_text in last_lines:
                try:
                    self.tmux.send_keys(target, "", press_enter=True)
                except Exception:  # noqa: BLE001
                    return
            else:
                return

    # ------------------------------------------------------------------
    # Internal: stabilization
    # ------------------------------------------------------------------

    def _stabilize(
        self,
        provider: str,
        target: str,
        name: str,
        on_status: Callable[[str], None] | None = None,
        account_name: str | None = None,
    ) -> None:
        def _prefixed(msg: str) -> None:
            if on_status:
                on_status(f"[{name}] {msg}")

        if provider == "claude":
            self._stabilize_claude_launch(target, on_status=_prefixed)
        elif provider == "codex":
            self._stabilize_codex_launch(
                target, on_status=_prefixed, account_name=account_name,
            )

    def _stabilize_claude_launch(
        self, target: str, on_status: Callable[[str], None] | None = None,
    ) -> None:
        timeout = 90
        start = time.monotonic()
        deadline = start + timeout
        last_action = ""
        poll_interval = 0.2
        _status = on_status or (lambda _msg: None)
        _status("Waiting for Claude Code to start...")
        while time.monotonic() < deadline:
            elapsed = int(time.monotonic() - start)
            try:
                pane = self.tmux.capture_pane(target, lines=320)
            except Exception:  # noqa: BLE001
                _status(f"Waiting for Claude Code to start... ({elapsed}s)")
                time.sleep(poll_interval)
                continue
            lowered = pane.lower()

            if "select login method:" in lowered or "paste code here if prompted" in lowered:
                _status("Login required — authenticate from the cockpit")
                return
            if "please run /login" in lowered or "invalid authentication credentials" in lowered:
                _status("Login required — re-authenticate interactively")
                return

            if "choose the text style that looks best with your terminal" in lowered:
                if last_action != "theme":
                    _status(f"Dismissing theme picker... ({elapsed}s)")
                    self.tmux.send_keys(target, "", press_enter=True)
                    last_action = "theme"
                    poll_interval = 0.5
                time.sleep(poll_interval)
                continue

            if "quick safety check" in lowered and "yes, i trust this folder" in lowered:
                if last_action != "trust":
                    _status(f"Accepting trust prompt... ({elapsed}s)")
                    self.tmux.send_keys(target, "", press_enter=True)
                    last_action = "trust"
                    poll_interval = 0.5
                time.sleep(poll_interval)
                continue

            if "warning: claude code running in bypass permissions mode" in lowered:
                if last_action != "bypass-confirm":
                    _status(f"Confirming bypass permissions mode... ({elapsed}s)")
                    self.tmux.send_keys(target, "2", press_enter=False)
                    self.tmux.send_keys(target, "", press_enter=True)
                    last_action = "bypass-confirm"
                    poll_interval = 0.5
                time.sleep(poll_interval)
                continue

            if "we recommend medium effort for opus" in lowered:
                if last_action != "effort":
                    _status(f"Dismissing effort recommendation... ({elapsed}s)")
                    self.tmux.send_keys(target, "", press_enter=True)
                    last_action = "effort"
                    poll_interval = 0.5
                time.sleep(poll_interval)
                continue

            if "\u276f" in pane and (
                "welcome back" in lowered
                or "0 tokens" in lowered
                or "/buddy" in pane
                or "bypass permissions" in lowered
                or "shift+tab" in lowered
            ):
                _status("Claude Code ready")
                return

            _status(f"Waiting for Claude Code to start... ({elapsed}s)")
            poll_interval = min(poll_interval + 0.1, 1.0)
            time.sleep(poll_interval)

        _status("Timed out waiting for Claude Code")

    def _stabilize_codex_launch(
        self,
        target: str,
        on_status: Callable[[str], None] | None = None,
        account_name: str | None = None,
    ) -> None:
        timeout = 60
        start = time.monotonic()
        deadline = start + timeout
        last_action = ""
        ready_streak = 0
        poll_interval = 0.2
        _status = on_status or (lambda _msg: None)
        _status("Waiting for Codex to start...")
        while time.monotonic() < deadline:
            elapsed = int(time.monotonic() - start)
            try:
                pane = self.tmux.capture_pane(target, lines=260)
            except Exception:  # noqa: BLE001
                _status(f"Waiting for Codex to start... ({elapsed}s)")
                time.sleep(poll_interval)
                continue
            lowered = pane.lower()

            if "approaching rate limits" in lowered and "switch to gpt-5.1-codex-mini" in lowered:
                if last_action != "switch-mini":
                    _status(f"Switching to codex-mini due to rate limits... ({elapsed}s)")
                    self.tmux.send_keys(target, "", press_enter=True)
                    last_action = "switch-mini"
                    poll_interval = 0.5
                time.sleep(poll_interval)
                continue
            if "usage limit" in lowered:
                from pollypm.errors import _last_lines, format_probe_failure
                who = account_name or "<controller>"
                raise RuntimeError(
                    format_probe_failure(
                        provider="Codex",
                        account_name=who,
                        account_email=None,
                        reason="the account is out of credits",
                        pane_tail=_last_lines(pane, n=5),
                        fix=(
                            f"switch the controller to a different account "
                            f"with `pm failover` (see `pm accounts`), or top "
                            f"up '{who}' and rerun `pm up`."
                        ),
                    )
                )
            if "press enter to continue" in lowered:
                if last_action != "continue":
                    _status(f"Dismissing continue prompt... ({elapsed}s)")
                    self.tmux.send_keys(target, "", press_enter=True)
                    last_action = "continue"
                    poll_interval = 0.5
                time.sleep(poll_interval)
                continue
            if "do you trust the contents of this directory" in lowered and "1. yes, continue" in lowered:
                if last_action != "trust":
                    _status(f"Accepting trust prompt... ({elapsed}s)")
                    self.tmux.send_keys(target, "", press_enter=True)
                    last_action = "trust"
                    poll_interval = 0.5
                time.sleep(poll_interval)
                continue
            prompt_visible = "% left" in lowered or "\u203a" in pane
            working = "working (" in lowered and "esc to interrupt" in lowered
            booting = "booting mcp server" in lowered
            if "openai codex" in lowered and (prompt_visible or working) and not booting:
                ready_streak += 1
                if ready_streak >= 2:
                    _status("Codex ready")
                    return
                time.sleep(0.3)
                continue
            ready_streak = 0
            _status(f"Waiting for Codex to start... ({elapsed}s)")
            poll_interval = min(poll_interval + 0.1, 1.0)
            time.sleep(poll_interval)

        _status("Timed out waiting for Codex")

    # ------------------------------------------------------------------
    # Internal: memory injection (M05 / #234)
    # ------------------------------------------------------------------

    def _inject_memory_into_prompt(
        self,
        *,
        initial_input: str,
        session_role: str | None,
        task_title: str | None,
        task_description: str | None,
        guide_reference: str | None = None,
        user_id: str,
    ) -> str:
        """Prepend memory recall + (for workers) the worker guide.

        Two injections compose here:

        1. The "## What you should know" memory recall section (M05).
        2. The "## Worker Protocol" worker-guide section (wg02 / #239),
           only when ``session_role == "worker"``.

        Order in the final prompt (top → bottom):
            ## Worker Protocol       (workers only)
            ## What you should know  (role-scoped recall)
            <persona prompt>

        This means the worker reads the lifecycle protocol first, then
        context-specific memories, then their persona. Non-worker roles
        get exactly today's behavior (worker protocol is empty).

        All failure paths fall through to the unchanged ``initial_input``
        so a broken memory store or missing guide never blocks a launch.
        """
        try:
            project_root = Path(self._config.project.root_dir)
            project_name = getattr(self._config.project, "name", project_root.name)
        except Exception:  # noqa: BLE001
            return initial_input
        try:
            from pollypm.memory_backends import get_memory_backend
            from pollypm.memory_prompts import (
                build_memory_injection,
                build_worker_protocol_injection,
                compute_task_context_summary,
                prepend_memory_injection,
                prepend_worker_protocol,
            )
        except Exception:  # noqa: BLE001
            return initial_input

        # Memory injection (may fail if backend is unavailable).
        memory_injection = ""
        try:
            backend = get_memory_backend(project_root, "file")
            summary = compute_task_context_summary(
                task_title=task_title,
                task_description=task_description,
                session_role=session_role,
                project=project_name,
            )
            memory_injection = build_memory_injection(
                backend,
                user_id=user_id,
                project_name=project_name,
                task_context_summary=summary,
            )
        except Exception:  # noqa: BLE001
            memory_injection = ""

        with_memory = prepend_memory_injection(initial_input, memory_injection)

        # Worker protocol injection — only for role=worker. Empty for
        # every other role, so the composition is a no-op for PM /
        # reviewer / supervisor / triage sessions.
        try:
            worker_injection = build_worker_protocol_injection(
                session_role=session_role,
                guide_reference=guide_reference,
            )
        except Exception:  # noqa: BLE001
            worker_injection = ""

        return prepend_worker_protocol(with_memory, worker_injection)

    # ------------------------------------------------------------------
    # Internal: initial input preparation
    # ------------------------------------------------------------------

    def _assert_session_launch_matches(
        self,
        session_name: str,
        *,
        expected_window: str | None,
        session_role: str | None,
    ) -> None:
        """Fail loud when a (session_name, target-window) tuple is crossed.

        Mirrors :meth:`pollypm.supervisor.Supervisor._assert_session_launch_matches`
        but operates on :class:`pollypm.config.PollyPMConfig.sessions` — the
        session service layer doesn't have a launch planner handle.

        Worker sessions are transient (per-task window names under a
        dynamic tmux session), so they are never registered in the
        static ``sessions`` config. Skip the check for those; the
        supervisor-layer assertion covers the static control roles.
        """
        if session_role == "worker":
            return
        sessions = getattr(self._config, "sessions", None) or {}
        cfg = sessions.get(session_name) if isinstance(sessions, dict) else None
        if cfg is None:
            # Session not in static config — likely an ad-hoc / worker
            # session. Nothing to cross-check against.
            return
        configured_window = cfg.window_name or cfg.name
        mismatch_window = (
            expected_window is not None and configured_window != expected_window
        )
        if mismatch_window:
            details = (
                f"session_name={session_name!r} "
                f"expected_window={expected_window!r} "
                f"configured_window={configured_window!r} "
                f"role={session_role!r}"
            )
            logger.error("persona_swap_detected (session_service): %s", details)
            # Audit the swap on the unified ``messages`` table. The
            # RuntimeError below still makes the failure unmissable if
            # the audit write itself fails.
            try:
                from pollypm.store.registry import get_store

                msg_store = get_store(self._config)
            except Exception:  # noqa: BLE001
                msg_store = None
            if msg_store is not None:
                try:
                    msg_store.append_event(
                        scope=session_name,
                        sender=session_name,
                        subject="persona_swap_detected",
                        payload={"message": details},
                    )
                except Exception:  # noqa: BLE001
                    pass
                finally:
                    close = getattr(msg_store, "close", None)
                    if callable(close):
                        try:
                            close()
                        except Exception:  # noqa: BLE001
                            pass
            raise RuntimeError(f"persona_swap_detected: {details}")

    def _prepare_initial_input(
        self,
        session_name: str,
        initial_input: str,
        *,
        expected_window: str | None = None,
        session_role: str | None = None,
    ) -> str:
        # Fail-loud persona-swap guard. The parallel path in
        # ``pollypm.supervisor.Supervisor._prepare_initial_input`` has
        # the same check against ``launch_by_session``; here we use the
        # config-level session map because the session service layer
        # has no launch planner handle. See 2026-04-16 commit that
        # added this check for the overnight-E2E context.
        self._assert_session_launch_matches(
            session_name,
            expected_window=expected_window,
            session_role=session_role,
        )
        if len(initial_input) <= 280:
            return initial_input
        from pollypm.project_paths import session_control_prompts_dir
        prompts_dir = session_control_prompts_dir(self._config, session_name)
        prompts_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = prompts_dir / f"{session_name}.md"
        prompt_path.write_text(initial_input.rstrip() + "\n")
        # Use absolute paths so the kickoff resolves regardless of the
        # worker's cwd (workers run from their worktree, not project root).
        # See issue #263.
        display_path = prompt_path
        instruct_path = self._config.project.root_dir / ".pollypm" / "docs" / "SYSTEM.md"
        if instruct_path.exists():
            instruct_display = instruct_path
            return (
                f"Read {instruct_display} for system context, then read {display_path} for your role. "
                f'Adopt both as your operating instructions, reply only "ready", then wait.'
            )
        return (
            f'Read {display_path}, adopt it as your operating instructions, reply only "ready", then wait.'
        )
