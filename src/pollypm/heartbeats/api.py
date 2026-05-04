from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pollypm.atomic_io import atomic_write_json
from pollypm.checkpoints import record_checkpoint, snapshot_hash, write_mechanical_checkpoint
from pollypm.heartbeats.base import HeartbeatCursor, HeartbeatSessionContext, HeartbeatUnmanagedWindow
from pollypm.heartbeats.types import Alert

if TYPE_CHECKING:
    from pollypm.supervisor import Supervisor


class SupervisorHeartbeatAPI:
    def __init__(self, supervisor: Supervisor, *, snapshot_lines: int = 200) -> None:
        self.supervisor = supervisor
        self.snapshot_lines = snapshot_lines
        self._contexts = self._build_contexts()

    def list_sessions(self) -> list[HeartbeatSessionContext]:
        return list(self._contexts)

    # Transient windows opened by the cockpit/CLI for short-lived utility
    # work (e.g. the rail one-click upgrade). They aren't part of the
    # supervisor's launch plan but are intentional and self-dismissing
    # — see #1000 for the ``pm-upgrade`` orphan-alert context.
    _TRANSIENT_UTILITY_WINDOW_NAMES = frozenset({"pm-upgrade"})

    @staticmethod
    def _normalize_window_key(name: str) -> str:
        """Return a hyphen/underscore-insensitive identity for ``name``.

        #1056: ``pm worker-start --role architect <project>`` writes a
        session config named ``architect_<project>`` (underscore) but
        names the tmux window ``architect-<project>`` (hyphen). Some
        callers — notably stale Supervisor instances served from a
        long-running rail daemon, or cases where the launch plan
        recomputes between a fresh-config edit and the heartbeat sweep
        — surface ``expected_window_names`` based on the session name
        rather than the recorded window_name. Normalising both sides
        before comparison makes the unmanaged-window check tolerant of
        either separator so the alert doesn't fire while ``pm status``
        confirms the same session is healthy.
        """
        return name.replace("-", "_") if name else ""

    def list_unmanaged_windows(self) -> list[HeartbeatUnmanagedWindow]:
        expected_window_names: set[str] = set()
        # Both the recorded window name AND the session name flow into
        # the expected set so naming drift between them (e.g. session
        # ``architect_russell`` with window ``architect-russell``) keeps
        # the live window from being flagged as unmanaged.
        for launch in self.supervisor.plan_launches():
            expected_window_names.add(launch.window_name)
            expected_window_names.add(launch.session.name)
        expected_window_names.add(self.supervisor.console_window_name())
        # Hyphen/underscore-insensitive lookup set — see #1056.
        expected_keys = {
            self._normalize_window_key(name) for name in expected_window_names
        }
        unmanaged: list[HeartbeatUnmanagedWindow] = []
        for window in self.supervisor.window_map().values():
            if window.name in expected_window_names:
                continue
            if self._normalize_window_key(window.name) in expected_keys:
                continue
            # Per-task worker sessions (task-<project>-<number>) are managed
            # by the SessionManager, not the supervisor's launch plan.
            if window.name.startswith("task-"):
                continue
            # Transient utility windows (#1000) — the rail one-click
            # upgrade opens ``pm-upgrade`` so the user can read upgrade
            # output and the changelog. It's intentional, short-lived,
            # and dismissed by the user pressing enter; the supervisor
            # shouldn't alert on it.
            if window.name in self._TRANSIENT_UTILITY_WINDOW_NAMES:
                continue
            unmanaged.append(
                HeartbeatUnmanagedWindow(
                    tmux_session=window.session,
                    window_name=window.name,
                    pane_id=window.pane_id,
                    pane_command=window.pane_current_command,
                    pane_dead=window.pane_dead,
                    pane_path=window.pane_current_path,
                )
            )
        unmanaged.sort(key=lambda item: (item.tmux_session, item.window_name))
        return unmanaged

    def get_cursor(self, session_name: str) -> HeartbeatCursor | None:
        return self._load_cursors().get(session_name)

    def update_cursor(
        self,
        session_name: str,
        *,
        source_path: str,
        last_offset: int,
        snapshot_hash: str = "",
        verdict: str = "",
        reason: str = "",
    ) -> None:
        cursors = self._load_cursors()
        cursors[session_name] = HeartbeatCursor(
            session_name=session_name,
            source_path=source_path,
            last_offset=max(0, int(last_offset)),
            last_processed_at=datetime.now(UTC).isoformat(),
            last_snapshot_hash=snapshot_hash,
            last_verdict=verdict,
            last_reason=reason,
        )
        self._save_cursors(cursors)

    def record_observation(self, context: HeartbeatSessionContext) -> None:
        if not context.window_present or context.snapshot_path is None:
            return
        self.supervisor.store.record_heartbeat(
            session_name=context.session_name,
            tmux_window=context.window_name,
            pane_id=context.pane_id or "",
            pane_command=context.pane_command or "",
            pane_dead=context.pane_dead,
            log_bytes=context.source_bytes,
            snapshot_path=context.snapshot_path,
            snapshot_hash=context.snapshot_hash,
        )

    def record_checkpoint(self, context: HeartbeatSessionContext, *, alerts: list[str]) -> None:
        if not context.window_present or context.snapshot_path is None:
            return
        launch = self.supervisor.launch_by_session(context.session_name)
        artifact = write_mechanical_checkpoint(
            self.supervisor.config,
            launch,
            snapshot_path=Path(context.snapshot_path),
            snapshot_content=context.pane_text,
            log_bytes=context.source_bytes,
            alerts=alerts,
        )
        record_checkpoint(
            self.supervisor.store,
            launch,
            project_key=launch.session.project,
            level="level0",
            artifact=artifact,
            snapshot_path=Path(context.snapshot_path),
            memory_backend_name=self.supervisor.config.memory.backend,
        )
        # Knowledge extraction now happens in session_intelligence (unified Haiku call)
        # instead of per-checkpoint snapshot learning.

    def record_event(self, session_name: str, event_type: str, message: str) -> None:
        # #349: events now land in the unified ``messages`` table via the
        # Store's fire-and-forget buffer. ``session_name`` maps to both
        # scope and sender for parity with legacy ``events`` rows.
        self.supervisor.msg_store.append_event(
            scope=session_name,
            sender=session_name,
            subject=event_type,
            payload={"message": message},
        )

    def raise_alert(self, session_name: str, alert_type: str, severity: str, message: str) -> None:
        self.supervisor.msg_store.upsert_alert(session_name, alert_type, severity, message)

    def clear_alert(self, session_name: str, alert_type: str) -> None:
        self.supervisor.msg_store.clear_alert(session_name, alert_type)

    def open_alerts(self) -> list[Alert]:
        # #349: route through ``Supervisor.open_alerts`` so we read from
        # the unified ``messages`` table (matching the writer flip).
        return [
            Alert(
                session_name=record.session_name,
                alert_type=record.alert_type,
                severity=record.severity,
                message=record.message,
                status=record.status,
                created_at=record.created_at,
                updated_at=record.updated_at,
                alert_id=record.alert_id,
            )
            for record in self.supervisor.open_alerts()
        ]

    def set_session_status(self, session_name: str, status: str, *, reason: str = "") -> None:
        self.supervisor.store.upsert_session_runtime(
            session_name=session_name,
            status=status,
            last_failure_message=reason or None,
        )

    def mark_account_auth_broken(self, account_name: str, provider: str, *, reason: str) -> None:
        self.supervisor.store.upsert_account_runtime(
            account_name=account_name,
            provider=provider,
            status="auth_broken",
            reason=reason,
        )

    def recent_snapshot_hashes(self, session_name: str, *, limit: int = 3) -> list[str]:
        return [item.snapshot_hash for item in self.supervisor.store.recent_heartbeats(session_name, limit=limit)]

    def recover_session(self, session_name: str, *, failure_type: str, message: str) -> None:
        launch = self.supervisor.launch_by_session(session_name)
        self.supervisor.maybe_recover_session(launch, failure_type=failure_type, failure_message=message)

    def send_session_message(self, session_name: str, text: str, *, owner: str = "heartbeat") -> None:
        try:
            self.supervisor.send_input(session_name, text, owner=owner)
        except Exception:  # noqa: BLE001
            # Session may be dead or missing — don't crash the sweep.
            pass

    # Only alert the operator about a session once every 10 minutes to avoid noise
    _FOLLOWUP_COOLDOWN_SECONDS = 600

    def queue_polly_followup(self, session_name: str, reason: str) -> None:
        # Rate-limit: check recent events for a followup already sent.
        # #349: events now live on the unified ``messages`` table; query
        # the Store instead of the legacy ``events`` table.
        try:
            recent = self.supervisor.msg_store.query_messages(
                type="event",
                scope=session_name,
                limit=50,
            )
            now = datetime.now(UTC)
            for event in recent:
                if event.get("subject") == "polly_followup":
                    created_at = event.get("created_at")
                    if created_at is None:
                        continue
                    stamp = (
                        created_at.isoformat()
                        if hasattr(created_at, "isoformat")
                        else str(created_at)
                    )
                    try:
                        parsed = datetime.fromisoformat(stamp)
                    except ValueError:
                        continue
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=UTC)
                    age = (now - parsed).total_seconds()
                    if age < self._FOLLOWUP_COOLDOWN_SECONDS:
                        return  # already notified recently
                    break  # found the most recent one but it's old enough
        except Exception:  # noqa: BLE001
            pass  # if we can't check, send anyway

        try:
            self.supervisor.send_input(
                "operator",
                (
                    f"H: Heartbeat alert for {session_name}. {reason}. "
                    "Options: (1) inspect the task list in the cockpit, "
                    "(2) restart the session from Workers if it is wedged, "
                    "(3) create a new task if the session needs work."
                ),
                owner="heartbeat",
            )
            from pollypm.plugins_builtin.activity_feed.summaries import (
                activity_summary,
            )

            self.supervisor.msg_store.append_event(
                scope=session_name,
                sender=session_name,
                subject="polly_followup",
                payload={
                    "message": activity_summary(
                        summary=f"Nudged operator about {session_name}: {reason}",
                        severity="recommendation",
                        verb="nudged",
                        subject=session_name,
                    ),
                    "reason": reason,
                },
            )
        except Exception:  # noqa: BLE001
            pass

    def _build_contexts(self) -> list[HeartbeatSessionContext]:
        window_map = self.supervisor.window_map()
        contexts: list[HeartbeatSessionContext] = []
        for launch in self.supervisor.plan_launches():
            cursor = self.get_cursor(launch.session.name)
            source_path = launch.log_path
            try:
                source_bytes = source_path.stat().st_size
            except (FileNotFoundError, OSError):
                source_bytes = 0
            transcript_delta = self._read_transcript_delta(source_path, cursor.last_offset if cursor else 0)
            # #1096 — window_map keys by (tmux_session, window_name) so
            # same-named windows in co-tenant sessions can't collide.
            tmux_session = self.supervisor.tmux_session_for_launch(launch)
            window = window_map.get((tmux_session, launch.window_name))
            snapshot_path: str | None = None
            pane_text = ""
            current_snapshot_hash = ""
            pane_id: str | None = None
            pane_command: str | None = None
            pane_dead = False
            if window is not None:
                pane_id = window.pane_id
                pane_command = window.pane_current_command
                pane_dead = window.pane_dead
                try:
                    raw_snapshot_path, pane_text = self.supervisor.write_snapshot(window, self.snapshot_lines)
                except Exception:
                    window = None
                    pane_text = ""
                else:
                    snapshot_path = str(raw_snapshot_path)
                    current_snapshot_hash = snapshot_hash(pane_text)
            previous = self.supervisor.store.latest_heartbeat(launch.session.name)
            contexts.append(
                HeartbeatSessionContext(
                    session_name=launch.session.name,
                    role=launch.session.role,
                    project_key=launch.session.project,
                    provider=launch.session.provider.value,
                    account_name=launch.account.name,
                    cwd=str(launch.session.cwd),
                    tmux_session=self.supervisor.tmux_session_for_launch(launch),
                    window_name=launch.window_name,
                    source_path=str(source_path),
                    source_bytes=source_bytes,
                    transcript_delta=transcript_delta,
                    pane_text=pane_text,
                    snapshot_path=snapshot_path,
                    snapshot_hash=current_snapshot_hash,
                    pane_id=pane_id,
                    pane_command=pane_command,
                    pane_dead=pane_dead,
                    window_present=window is not None,
                    previous_log_bytes=previous.log_bytes if previous else None,
                    previous_snapshot_hash=previous.snapshot_hash if previous else None,
                    cursor=cursor,
                )
            )
        return contexts

    def _cursor_path(self) -> Path:
        path = self.supervisor.config.project.base_dir / "heartbeat" / "cursors.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _load_cursors(self) -> dict[str, HeartbeatCursor]:
        path = self._cursor_path()
        if not path.exists():
            return {}
        raw = json.loads(path.read_text())
        return {
            session_name: HeartbeatCursor(**payload)
            for session_name, payload in raw.items()
        }

    def _save_cursors(self, cursors: dict[str, HeartbeatCursor]) -> None:
        path = self._cursor_path()
        raw = {name: asdict(cursor) for name, cursor in cursors.items()}
        atomic_write_json(path, raw)

    _MAX_TRANSCRIPT_DELTA = 64 * 1024  # 64KB max per heartbeat cycle

    def _read_transcript_delta(self, path: Path, last_offset: int) -> str:
        try:
            size = path.stat().st_size
        except (FileNotFoundError, OSError):
            return ""
        start = min(max(0, last_offset), size)
        if start == size:
            return ""
        # Cap the read to prevent huge transcripts from stalling the sweep
        read_size = min(size - start, self._MAX_TRANSCRIPT_DELTA)
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as handle:
                handle.seek(start)
                return handle.read(read_size)
        except (FileNotFoundError, OSError):
            return ""
