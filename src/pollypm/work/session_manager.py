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

from pollypm.tmux.client import TmuxClient

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
        tmux_client: TmuxClient,
        work_service: object,
        project_path: Path,
    ) -> None:
        self._tmux = tmux_client
        self._svc = work_service
        self._project_path = project_path
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
        """Create worktree + tmux pane for a task. Idempotent."""
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

        # Launch Claude in the worktree with the task prompt
        window_name = f"task-{task_slug}"
        if prompt_path.exists():
            claude_cmd = (
                f"cd {_shell_quote(str(worktree_path))} && "
                f"claude --dangerously-skip-permissions "
                f"-p {_shell_quote(str(prompt_path))}"
            )
        else:
            claude_cmd = (
                f"cd {_shell_quote(str(worktree_path))} && "
                f"claude --dangerously-skip-permissions"
            )

        # Use the storage closet session for task worker windows
        session_name = "pollypm-storage-closet"
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

        # Kill tmux pane
        if pane_id:
            try:
                self._tmux.kill_pane(pane_id)
            except subprocess.CalledProcessError:
                logger.debug("Pane %s already gone during teardown", pane_id)

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
        """Send rejection feedback to the worker's session.

        If the session is alive, sends feedback directly. If the session
        has exited (Claude -p mode exits after processing), spawns a new
        worker session with the rejection context in its prompt.

        Returns True if feedback was delivered.
        """
        session = self.session_for_task(task_id)

        # Try to send to existing session
        if session is not None and self._tmux.is_pane_alive(session.pane_id):
            message = (
                f"Your work on this task was rejected by the reviewer. "
                f"Reason: {reason}. "
                f"Please address the feedback and signal done when ready."
            )
            try:
                self._tmux.send_keys(session.pane_id, message)
                return True
            except Exception:
                logger.debug("Failed to send rejection to pane %s", session.pane_id)

        # Session is dead or doesn't exist — spawn a new worker with rejection context
        project, task_number = _parse_task_id(task_id)
        task_slug = f"{project}-{task_number}"

        # Reuse existing worktree if it still exists
        worktree_path = self._project_path / ".pollypm" / "worktrees" / task_slug
        if not worktree_path.exists():
            try:
                worktree_path = self._create_worktree(task_id, task_slug, self._project_path)
            except Exception:
                logger.warning("Could not create worktree for rework on %s", task_id)
                return False

        # Build rework prompt with rejection context
        rework_prompt = self._build_rework_prompt(task_id, reason, worktree_path)
        prompt_path = worktree_path / ".pollypm-task-prompt.md"
        try:
            prompt_path.parent.mkdir(parents=True, exist_ok=True)
            prompt_path.write_text(rework_prompt)
        except OSError:
            logger.warning("Could not write rework prompt for %s", task_id)
            return False

        # Launch new Claude session
        window_name = f"task-{task_slug}"
        claude_cmd = (
            f"cd {_shell_quote(str(worktree_path))} && "
            f"claude --dangerously-skip-permissions "
            f"-p {_shell_quote(str(prompt_path))}"
        )

        session_name = "pollypm-storage-closet"
        try:
            # Kill old window if it exists
            for win in self._tmux.list_windows(session_name):
                if win.name == window_name:
                    self._tmux.kill_window(f"{session_name}:{win.index}")
                    break
            self._tmux.create_window(session_name, window_name, claude_cmd, detached=True)
            # Update session record
            windows = self._tmux.list_windows(session_name)
            pane_id = "%0"
            for w in windows:
                if w.name == window_name:
                    pane_id = w.pane_id
                    break
            conn = self._get_conn()
            conn.execute(
                "UPDATE work_sessions SET pane_id = ?, ended_at = NULL "
                "WHERE task_project = ? AND task_number = ?",
                (pane_id, project, task_number),
            )
            conn.commit()
            return True
        except Exception:
            logger.warning("Failed to spawn rework session for %s", task_id)
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

    def _build_rework_prompt(self, task_id: str, reason: str, worktree_path: Path) -> str:
        """Build a prompt for rework after rejection."""
        base = self._build_task_prompt(task_id, worktree_path)
        return (
            f"{base}\n\n"
            f"## REWORK REQUIRED\n\n"
            f"Your previous submission was **rejected** by the reviewer.\n\n"
            f"**Rejection reason:** {reason}\n\n"
            f"Review the feedback carefully. Fix the specific issues mentioned. "
            f"The reviewer will check that the issues are actually resolved — "
            f"submitting without changes will be rejected again.\n\n"
            f"Check the existing code in this worktree, fix the problems, "
            f"commit your changes, then signal done.\n"
        )

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
