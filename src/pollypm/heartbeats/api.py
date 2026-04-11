from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pollypm.checkpoints import record_checkpoint, snapshot_hash, write_mechanical_checkpoint
from pollypm.heartbeats.base import HeartbeatCursor, HeartbeatSessionContext, HeartbeatUnmanagedWindow

if TYPE_CHECKING:
    from pollypm.supervisor import Supervisor


class SupervisorHeartbeatAPI:
    def __init__(self, supervisor: Supervisor, *, snapshot_lines: int = 200) -> None:
        self.supervisor = supervisor
        self.snapshot_lines = snapshot_lines
        self._contexts = self._build_contexts()

    def list_sessions(self) -> list[HeartbeatSessionContext]:
        return list(self._contexts)

    def list_unmanaged_windows(self) -> list[HeartbeatUnmanagedWindow]:
        expected_window_names = {launch.window_name for launch in self.supervisor.plan_launches()}
        expected_window_names.add(self.supervisor.console_window_name())
        unmanaged: list[HeartbeatUnmanagedWindow] = []
        for window in self.supervisor._window_map().values():
            if window.name in expected_window_names:
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
        launch = self.supervisor._launch_by_session(context.session_name)
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

    def record_event(self, session_name: str, event_type: str, message: str) -> None:
        self.supervisor.store.record_event(session_name, event_type, message)

    def raise_alert(self, session_name: str, alert_type: str, severity: str, message: str) -> None:
        self.supervisor.store.upsert_alert(session_name, alert_type, severity, message)

    def clear_alert(self, session_name: str, alert_type: str) -> None:
        self.supervisor.store.clear_alert(session_name, alert_type)

    def open_alerts(self):
        return self.supervisor.store.open_alerts()

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
        launch = self.supervisor._launch_by_session(session_name)
        self.supervisor._maybe_recover_session(launch, failure_type=failure_type, failure_message=message)

    def send_session_message(self, session_name: str, text: str, *, owner: str = "heartbeat") -> None:
        try:
            self.supervisor.send_input(session_name, text, owner=owner)
        except Exception:  # noqa: BLE001
            # Session may be dead or missing — don't crash the sweep.
            pass

    def queue_polly_followup(self, session_name: str, reason: str) -> None:
        try:
            self.supervisor.send_input(
                "operator",
                (
                    f"H: Heartbeat alert for {session_name}. {reason}. "
                    f"Options: (1) send a nudge via `pm send {session_name} 'continue'`, "
                    f"(2) check the pane with `tmux capture-pane -t pollypm-storage-closet:{session_name} -p | tail -20`, "
                    f"(3) reassign if stuck."
                ),
                owner="heartbeat",
            )
        except Exception:  # noqa: BLE001
            # Operator may be dead — don't crash the sweep.
            pass

    def _build_contexts(self) -> list[HeartbeatSessionContext]:
        window_map = self.supervisor._window_map()
        contexts: list[HeartbeatSessionContext] = []
        for launch in self.supervisor.plan_launches():
            cursor = self.get_cursor(launch.session.name)
            source_path = launch.log_path
            source_bytes = source_path.stat().st_size if source_path.exists() else 0
            transcript_delta = self._read_transcript_delta(source_path, cursor.last_offset if cursor else 0)
            window = window_map.get(launch.window_name)
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
                    raw_snapshot_path, pane_text = self.supervisor._write_snapshot(window, self.snapshot_lines)
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
                    tmux_session=self.supervisor._tmux_session_for_launch(launch),
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
        path.write_text(json.dumps(raw, indent=2) + "\n")

    def _read_transcript_delta(self, path: Path, last_offset: int) -> str:
        if not path.exists():
            return ""
        size = path.stat().st_size
        start = min(max(0, last_offset), size)
        if start == size:
            return ""
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            handle.seek(start)
            return handle.read()
