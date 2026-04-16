"""Worker session lifecycle manager.

Binds task state transitions to deterministic tmux/worktree operations.
No LLM involved — pure infrastructure code.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class WorkerSession:
    """Active binding between a task and a tmux/worktree session."""

    task_id: str
    agent_name: str
    pane_id: str
    worktree_path: Path
    branch_name: str
    started_at: datetime


@dataclass(slots=True)
class TeardownResult:
    """Result of tearing down a worker session."""

    task_id: str
    jsonl_archived: bool
    archive_path: Path | None
    total_input_tokens: int
    total_output_tokens: int
    worktree_removed: bool


# ---------------------------------------------------------------------------
# Schema extension
# ---------------------------------------------------------------------------

WORK_SESSIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS work_sessions (
    task_project TEXT NOT NULL,
    task_number INTEGER NOT NULL,
    agent_name TEXT NOT NULL,
    pane_id TEXT,
    worktree_path TEXT,
    branch_name TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    total_input_tokens INTEGER DEFAULT 0,
    total_output_tokens INTEGER DEFAULT 0,
    archive_path TEXT,
    PRIMARY KEY (task_project, task_number),
    FOREIGN KEY (task_project, task_number) REFERENCES work_tasks(project, task_number)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# SessionManager
# ---------------------------------------------------------------------------


class SessionManager:
    """Deterministic session manager that binds worker sessions to tasks.

    Coordinates git worktrees, tmux panes, and JSONL archival around
    task lifecycle transitions.
    """

    def __init__(
        self,
        tmux_client: object,
        work_service: object,
        project_path: Path,
        *,
        session_service: object | None = None,
        storage_closet_name: str = "pollypm-storage-closet",
    ) -> None:
        self._tmux = tmux_client
        self._session_service = session_service
        self._svc = work_service
        self._project_path = project_path
        self._storage_closet_name = storage_closet_name
        # Ensure the sessions table exists
        conn = self._get_conn()
        conn.executescript(WORK_SESSIONS_SCHEMA)

    def _get_conn(self):
        """Access the work service's SQLite connection."""
        return self._svc._conn  # type: ignore[attr-access]

    # ------------------------------------------------------------------
    # Provisioning
    # ------------------------------------------------------------------

    def provision_worker(self, task_id: str, agent_name: str) -> WorkerSession:
        """Create worktree + tmux window for a task. Idempotent.

        Launches an *interactive* Claude session in the named tmux window
        (via the ``SessionService`` when provided). The task prompt is
        written to ``.pollypm-task-prompt.md`` inside the worktree and
        a short kickoff string is fed as keystrokes so the worker reads
        the file and gets to work. Relies on ``teardown_worker`` to kill
        the window on approval/cancel.
        """
        project, task_number = _parse_task_id(task_id)

        # Check for existing active session
        existing = self.session_for_task(task_id)
        if existing is not None:
            return existing

        # Derive slug from task_id for branch/path naming
        task_slug = f"{project}-{task_number}"
        branch_name = f"task/{task_slug}"

        # Create worktree
        worktree_path = self._create_worktree(task_id, task_slug, self._project_path)

        # Build task prompt for the worker
        task_prompt = self._build_task_prompt(task_id, worktree_path)
        prompt_path = worktree_path / ".pollypm-task-prompt.md"
        try:
            prompt_path.parent.mkdir(parents=True, exist_ok=True)
            prompt_path.write_text(task_prompt)
        except OSError:
            logger.warning("Could not write task prompt to %s", prompt_path)

        window_name = f"task-{task_slug}"
        session_name = self._storage_closet_name

        # Kickoff string sent after stabilization so the worker reads the
        # prompt file and signals done when finished.
        kickoff = (
            f"Read .pollypm-task-prompt.md, follow the instructions, "
            f"commit your work, then run `pm task done {task_id}`."
        )

        pane_id = self._launch_worker_window(
            session_name=session_name,
            window_name=window_name,
            worktree_path=worktree_path,
            agent_name=agent_name,
            kickoff=kickoff,
        )

        now = _now_dt()

        # Store binding in SQLite
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO work_sessions "
            "(task_project, task_number, agent_name, pane_id, worktree_path, "
            "branch_name, started_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (project, task_number, agent_name, pane_id, str(worktree_path),
             branch_name, now.isoformat()),
        )
        conn.commit()

        return WorkerSession(
            task_id=task_id,
            agent_name=agent_name,
            pane_id=pane_id,
            worktree_path=worktree_path,
            branch_name=branch_name,
            started_at=now,
        )

    # ------------------------------------------------------------------
    # Worker launch helpers
    # ------------------------------------------------------------------

    def _launch_worker_window(
        self,
        *,
        session_name: str,
        window_name: str,
        worktree_path: Path,
        agent_name: str,
        kickoff: str,
    ) -> str:
        """Launch an interactive Claude window and return its pane id.

        When a ``SessionService`` is configured, route through it so the
        window picks up stabilization, initial-input verification and
        other robustness we already implemented there. Otherwise fall
        back to raw tmux operations (used by unit tests that inject a
        mock TmuxClient).
        """
        claude_cmd = (
            f"cd {_shell_quote(str(worktree_path))} && "
            f"claude --dangerously-skip-permissions"
        )

        # Clear any stale window of the same name (e.g. a dead pane from
        # a previous run that lingered because remain-on-exit=on). Without
        # this, create_window is a no-op when the name already exists.
        self._kill_stale_task_window(session_name, window_name)

        if self._session_service is not None:
            # Use a fresh_launch_marker so SessionService.create() knows
            # to send the kickoff as initial input.
            marker_dir = self._project_path / ".pollypm" / "worker-markers"
            marker_dir.mkdir(parents=True, exist_ok=True)
            marker_path = marker_dir / f"{window_name}.fresh"
            try:
                marker_path.write_text(_now())
            except OSError:
                logger.warning("Could not write fresh_launch_marker for %s", window_name)

            handle = self._session_service.create(
                name=window_name,
                provider="claude",
                account=agent_name,
                cwd=worktree_path,
                command=claude_cmd,
                window_name=window_name,
                tmux_session=session_name,
                stabilize=True,
                initial_input=kickoff,
                fresh_launch_marker=marker_path,
                session_role="worker",
            )
            return handle.pane_id or ""

        # Fallback: raw tmux client path. Used by tests that construct
        # SessionManager without a session_service.
        if not self._tmux.has_session(session_name):
            self._tmux.create_session(session_name, window_name, claude_cmd)
            windows = self._tmux.list_windows(session_name)
            pane_id = windows[0].pane_id if windows else "%0"
        else:
            self._tmux.create_window(session_name, window_name, claude_cmd, detached=True)
            windows = self._tmux.list_windows(session_name)
            pane_id = "%0"
            for w in windows:
                if w.name == window_name:
                    pane_id = w.pane_id
                    break
        return pane_id

    def _kill_stale_task_window(self, session_name: str, window_name: str) -> None:
        """Kill a stale ``task-<slug>`` window before re-provisioning.

        Because the storage-closet session is created with
        ``remain-on-exit=on``, a prior worker's dead pane can linger in
        the window and cause ``create_window`` to no-op. We defensively
        clear it here.
        """
        try:
            if not self._tmux.has_session(session_name):
                return
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "_kill_stale_task_window: has_session(%s) failed: %s",
                session_name,
                exc,
            )
            return

        try:
            windows = self._tmux.list_windows(session_name)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "_kill_stale_task_window: list_windows(%s) failed: %s",
                session_name,
                exc,
            )
            return

        for w in windows:
            if getattr(w, "name", None) == window_name:
                target = f"{session_name}:{window_name}"
                try:
                    self._tmux.kill_window(target)
                    logger.debug("Cleared stale task window %s", target)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Failed to clear stale task window %s: %s", target, exc,
                    )
                return

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def teardown_worker(self, task_id: str) -> TeardownResult:
        """Archive JSONL, record tokens, kill session, clean worktree. Idempotent."""
        project, task_number = _parse_task_id(task_id)

        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM work_sessions WHERE task_project = ? AND task_number = ?",
            (project, task_number),
        ).fetchone()

        if row is None or row["ended_at"] is not None:
            # Already torn down or never existed
            return TeardownResult(
                task_id=task_id,
                jsonl_archived=False,
                archive_path=None,
                total_input_tokens=0,
                total_output_tokens=0,
                worktree_removed=False,
            )

        pane_id = row["pane_id"]
        worktree_path = Path(row["worktree_path"]) if row["worktree_path"] else None

        # Archive JSONL
        archive_path = None
        input_tokens = 0
        output_tokens = 0
        jsonl_archived = False
        if worktree_path is not None:
            archive_path, input_tokens, output_tokens = self._archive_jsonl(
                task_id, worktree_path
            )
            jsonl_archived = archive_path is not None

        # Kill tmux window (not just the pane). With remain-on-exit=on on
        # the storage-closet session, kill_pane alone leaves a dead pane
        # in a named window that blocks subsequent create_window calls.
        task_slug = f"{project}-{task_number}"
        window_name = f"task-{task_slug}"
        window_target = f"{self._storage_closet_name}:{window_name}"
        try:
            self._tmux.kill_window(window_target)
        except subprocess.CalledProcessError:
            logger.debug("Window %s already gone during teardown", window_target)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "kill_window %s failed during teardown: %s", window_target, exc,
            )

        # Remove worktree
        worktree_removed = False
        if worktree_path is not None:
            worktree_removed = self._remove_worktree(worktree_path)

        # Update session record
        conn.execute(
            "UPDATE work_sessions SET ended_at = ?, total_input_tokens = ?, "
            "total_output_tokens = ?, archive_path = ? "
            "WHERE task_project = ? AND task_number = ?",
            (_now(), input_tokens, output_tokens,
             str(archive_path) if archive_path else None,
             project, task_number),
        )
        conn.commit()

        return TeardownResult(
            task_id=task_id,
            jsonl_archived=jsonl_archived,
            archive_path=archive_path,
            total_input_tokens=input_tokens,
            total_output_tokens=output_tokens,
            worktree_removed=worktree_removed,
        )

    # ------------------------------------------------------------------
    # Rejection
    # ------------------------------------------------------------------

    def notify_rejection(self, task_id: str, reason: str) -> bool:
        """Send rejection feedback to the worker's live pane.

        With interactive-launch workers the pane is still alive after a
        rejection, so we simply feed the feedback as keystrokes. No more
        spawning a fresh ``claude -p`` session — that workaround existed
        only because headless workers exited before we could talk to
        them.

        Returns True if feedback was delivered.
        """
        session = self.session_for_task(task_id)
        if session is None:
            logger.warning(
                "notify_rejection: no active worker session for %s", task_id,
            )
            return False

        if not self._tmux.is_pane_alive(session.pane_id):
            logger.warning(
                "notify_rejection: pane %s for task %s is not alive; "
                "cannot deliver feedback",
                session.pane_id,
                task_id,
            )
            return False

        message = (
            f"Your work on this task was rejected by the reviewer. "
            f"Reason: {reason}. "
            f"Please address the feedback and signal done when ready."
        )
        try:
            self._tmux.send_keys(session.pane_id, message)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "notify_rejection: failed to send rejection to pane %s for %s: %s",
                session.pane_id,
                task_id,
                exc,
            )
            return False

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def active_sessions(self, project: str | None = None) -> list[WorkerSession]:
        """List all active worker sessions, optionally filtered by project."""
        conn = self._get_conn()
        if project is not None:
            rows = conn.execute(
                "SELECT * FROM work_sessions WHERE ended_at IS NULL AND task_project = ?",
                (project,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM work_sessions WHERE ended_at IS NULL",
            ).fetchall()

        return [self._row_to_session(r) for r in rows]

    def session_for_task(self, task_id: str) -> WorkerSession | None:
        """Get the worker session bound to a task, or None."""
        project, task_number = _parse_task_id(task_id)
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM work_sessions "
            "WHERE task_project = ? AND task_number = ? AND ended_at IS NULL",
            (project, task_number),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_session(row)

    # ------------------------------------------------------------------
    # JSONL archival
    # ------------------------------------------------------------------

    def _archive_jsonl(
        self, task_id: str, worktree_path: Path
    ) -> tuple[Path | None, int, int]:
        """Copy Claude JSONL to archive location. Parse token counts.

        Returns (archive_path, input_tokens, output_tokens).
        """
        # Look for .jsonl files in the worktree's .claude directory
        claude_dir = worktree_path / ".claude"
        if not claude_dir.exists():
            return None, 0, 0

        jsonl_files = list(claude_dir.rglob("*.jsonl"))
        if not jsonl_files:
            return None, 0, 0

        # Create archive directory
        project, task_number = _parse_task_id(task_id)
        archive_dir = (
            self._project_path / ".pollypm" / "transcripts" / "tasks" / task_id
        )
        archive_dir.mkdir(parents=True, exist_ok=True)

        total_input = 0
        total_output = 0

        for src in jsonl_files:
            dst = archive_dir / src.name
            shutil.copy2(src, dst)

            # Parse token counts
            input_t, output_t = _parse_token_usage(src)
            total_input += input_t
            total_output += output_t

        return archive_dir, total_input, total_output

    # ------------------------------------------------------------------
    # Worktree management
    # ------------------------------------------------------------------

    def _create_worktree(
        self, task_id: str, task_slug: str, project_path: Path
    ) -> Path:
        """``git worktree add``. Returns the worktree path. Idempotent."""
        worktree_path = project_path / ".pollypm" / "worktrees" / task_slug
        branch_name = f"task/{task_slug}"

        if worktree_path.exists():
            return worktree_path

        worktree_path.parent.mkdir(parents=True, exist_ok=True)

        result = subprocess.run(
            [
                "git", "-C", str(project_path),
                "worktree", "add",
                str(worktree_path),
                "-b", branch_name,
            ],
            check=False,
            text=True,
            capture_output=True,
        )
        if result.returncode != 0:
            # Branch may already exist; try without -b
            result = subprocess.run(
                [
                    "git", "-C", str(project_path),
                    "worktree", "add",
                    str(worktree_path),
                    branch_name,
                ],
                check=False,
                text=True,
                capture_output=True,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"git worktree add failed: {result.stderr.strip()}"
                )

        return worktree_path

    def _remove_worktree(self, worktree_path: Path) -> bool:
        """``git worktree remove``. Returns True if removed. Idempotent."""
        if not worktree_path.exists():
            return True

        result = subprocess.run(
            [
                "git", "-C", str(self._project_path),
                "worktree", "remove", "--force",
                str(worktree_path),
            ],
            check=False,
            text=True,
            capture_output=True,
        )
        if result.returncode != 0:
            logger.warning(
                "Failed to remove worktree %s: %s",
                worktree_path,
                result.stderr.strip(),
            )
            return False

        # Prune stale worktree metadata
        subprocess.run(
            ["git", "-C", str(self._project_path), "worktree", "prune"],
            check=False,
            text=True,
            capture_output=True,
        )
        return True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_task_prompt(self, task_id: str, worktree_path: Path) -> str:
        """Build a clear operating prompt for a per-task worker session."""
        # Get task details from the work service
        task_info = ""
        try:
            task = self._svc.get(task_id)
            task_info = (
                f"## Your Assignment\n\n"
                f"**Task {task.task_id}**: {task.title}\n\n"
            )
            if task.description:
                task_info += f"**Description:**\n{task.description}\n\n"
            if task.acceptance_criteria:
                task_info += f"**Acceptance Criteria:**\n{task.acceptance_criteria}\n\n"
        except Exception:
            task_info = f"## Your Assignment\n\nTask ID: {task_id}\n\n"

        return (
            f"You are a PollyPM task worker. You have one job: complete the task below.\n\n"
            f"You are working in a git worktree at: {worktree_path}\n"
            f"This is an isolated branch — your changes won't affect other workers.\n\n"
            f"{task_info}"
            f"## How to Work\n\n"
            f"1. Read the task description carefully\n"
            f"2. Implement the work: read code, write code, run tests, commit\n"
            f"3. When done, signal completion:\n"
            f"   ```\n"
            f"   pm task done {task_id} -o '{{"
            f'"type": "code_change", '
            f'"summary": "what you did", '
            f'"artifacts": [{{"kind": "commit", "ref": "<hash>", "description": "..."}}]'
            f"}}'\n"
            f"   ```\n\n"
            f"## Work Output Types\n\n"
            f"- **type**: code_change | action | document | mixed\n"
            f"- **artifacts**: list of concrete outputs\n"
            f"  - commit: `{{\"kind\": \"commit\", \"ref\": \"<hash>\", \"description\": \"...\"}}`\n"
            f"  - file_change: `{{\"kind\": \"file_change\", \"path\": \"...\", \"description\": \"...\"}}`\n"
            f"  - action: `{{\"kind\": \"action\", \"description\": \"...\"}}`\n\n"
            f"## Rules\n\n"
            f"- Stay focused on this task only\n"
            f"- Commit your work before signaling done\n"
            f"- The quality bar is high — test before shipping\n"
            f"- If blocked, add a context note: `pm task context {task_id} \"blocked on X\"`\n\n"
            f"## Common Review Failures (avoid these)\n\n"
            f"- pyproject.toml: always use `build-backend = \"setuptools.build_meta\"` "
            f"(NOT setuptools.backends._legacy:_Backend)\n"
            f"- pyproject.toml: if using setuptools, add "
            f"`[tool.setuptools.packages.find]` with `exclude = [\"issues*\", \"docs*\", \"tests*\"]` "
            f"to prevent the issues/ directory from being treated as a package\n"
            f"- Verify your code actually runs (`uv run python -m <package>`) before signaling done\n"
            f"- Check every acceptance criterion individually\n"
            f"- Include proper error handling for CLI tools\n"
        )

    @staticmethod
    def _row_to_session(row) -> WorkerSession:
        project = row["task_project"]
        number = row["task_number"]
        return WorkerSession(
            task_id=f"{project}/{number}",
            agent_name=row["agent_name"],
            pane_id=row["pane_id"] or "",
            worktree_path=Path(row["worktree_path"]) if row["worktree_path"] else Path(),
            branch_name=row["branch_name"] or "",
            started_at=datetime.fromisoformat(row["started_at"]),
        )


# ---------------------------------------------------------------------------
# Free helpers
# ---------------------------------------------------------------------------


def _parse_task_id(task_id: str) -> tuple[str, int]:
    """Parse ``'project/number'`` into (project, task_number)."""
    parts = task_id.rsplit("/", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid task_id: {task_id}")
    return parts[0], int(parts[1])


def _shell_quote(s: str) -> str:
    """Shell-quote a string for embedding in a tmux command."""
    import shlex
    return shlex.quote(s)


def _parse_token_usage(jsonl_path: Path) -> tuple[int, int]:
    """Parse token usage from a Claude JSONL file.

    Looks for lines containing token_usage information.
    Returns (input_tokens, output_tokens).
    """
    input_tokens = 0
    output_tokens = 0

    try:
        with open(jsonl_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Look for usage in message responses
                usage = None
                if isinstance(data, dict):
                    # Direct token_usage event
                    if data.get("type") == "token_usage":
                        usage = data
                    # Nested in message.usage
                    elif "usage" in data:
                        usage = data["usage"]
                    # Nested in message response
                    elif "message" in data and isinstance(data["message"], dict):
                        usage = data["message"].get("usage")

                if usage and isinstance(usage, dict):
                    input_tokens += usage.get("input_tokens", 0)
                    output_tokens += usage.get("output_tokens", 0)
    except OSError:
        logger.debug("Could not read JSONL file: %s", jsonl_path)

    return input_tokens, output_tokens
