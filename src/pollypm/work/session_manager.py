"""Worker session lifecycle manager.

Binds task state transitions to deterministic tmux/worktree operations.
No LLM involved — pure infrastructure code.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from subprocess import CalledProcessError as _CalledProcessError

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
        # Ensure the sessions table exists. The work service owns the
        # persistence layer — previously we reached into ``_svc._conn``
        # and executed DDL directly (#105).
        self._svc.ensure_worker_session_schema()

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

        Concurrent provision calls for the same ``task_id`` are
        serialized via a per-task session lock so two racing ``claim()``
        invocations can't both run ``git worktree add`` against the
        same path.
        """
        project, task_number = _parse_task_id(task_id)

        # Check for existing active session
        existing = self.session_for_task(task_id)
        if existing is not None:
            return existing

        # Derive slug from task_id for branch/path naming
        task_slug = f"{project}-{task_number}"
        branch_name = f"task/{task_slug}"

        # Serialize concurrent provisions of the same task. The lock
        # lives in the worktree's parent directory so both the lock and
        # the worktree-add race on the same filesystem target.
        worktree_parent = self._project_path / ".pollypm" / "worktrees"
        session_id = f"task-{task_slug}"
        lock_acquired = False
        try:
            from pollypm.projects import ensure_session_lock, release_session_lock

            try:
                ensure_session_lock(worktree_parent, session_id)
                lock_acquired = True
            except Exception as exc:  # noqa: BLE001
                # Another provision is in-flight or the lock is stuck.
                # Re-check: if the other provision won, just return its
                # session. Otherwise surface the lock failure.
                logger.warning(
                    "provision_worker[%s]: session lock conflict: %s",
                    task_id, exc,
                )
                existing = self.session_for_task(task_id)
                if existing is not None:
                    return existing
                raise

            # Re-check after acquiring the lock in case another provision
            # completed while we were waiting.
            existing = self.session_for_task(task_id)
            if existing is not None:
                return existing

            worker_session = self._provision_locked(
                task_id=task_id,
                agent_name=agent_name,
                project=project,
                task_number=task_number,
                task_slug=task_slug,
                branch_name=branch_name,
            )
            return worker_session
        finally:
            if lock_acquired:
                try:
                    release_session_lock(worktree_parent, session_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "provision_worker[%s]: failed to release session lock: %s",
                        task_id, exc,
                    )

    def _provision_locked(
        self,
        *,
        task_id: str,
        agent_name: str,
        project: str,
        task_number: int,
        task_slug: str,
        branch_name: str,
    ) -> WorkerSession:
        """Inner provision body — runs under the per-task session lock."""
        # Create worktree
        worktree_path = self._create_worktree(task_id, task_slug, self._project_path)

        # Build task prompt for the worker
        task_prompt = self._build_task_prompt(task_id, worktree_path)
        prompt_path = worktree_path / ".pollypm-task-prompt.md"
        try:
            prompt_path.parent.mkdir(parents=True, exist_ok=True)
            prompt_path.write_text(task_prompt)
        except OSError as exc:
            # Worker's kickoff references this file — if we can't write
            # it, the worker will fail to bootstrap. Surface the reason.
            logger.warning(
                "Could not write task prompt to %s: %s", prompt_path, exc,
            )

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

        # Store binding through the work service. Upsert so a re-claim
        # after cancel reuses the row (teardown stamps ended_at but
        # doesn't delete) instead of hitting the PK constraint.
        self._svc.upsert_worker_session(
            task_project=project,
            task_number=task_number,
            agent_name=agent_name,
            pane_id=pane_id,
            worktree_path=str(worktree_path),
            branch_name=branch_name,
            started_at=now.isoformat(),
        )

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
            logger.warning(
                "_kill_stale_task_window: has_session(%s) failed: %s",
                session_name,
                exc,
            )
            return

        try:
            windows = self._tmux.list_windows(session_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
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
        """Archive JSONL, record tokens, kill session, clean worktree.

        Idempotent. Each phase (archive, kill window, remove worktree)
        is isolated so a failure in one doesn't prevent the others from
        running. ``ended_at`` is only stamped if the tmux window was
        confirmed killed (or there was no pane to begin with); that way
        a future pass can detect and re-attempt teardown of a session
        whose pane we failed to kill.
        """
        project, task_number = _parse_task_id(task_id)

        record = self._svc.get_worker_session(
            task_project=project, task_number=task_number,
        )
        if record is None or record.ended_at is not None:
            # Already torn down or never existed
            return TeardownResult(
                task_id=task_id,
                jsonl_archived=False,
                archive_path=None,
                total_input_tokens=0,
                total_output_tokens=0,
                worktree_removed=False,
            )

        pane_id = record.pane_id
        worktree_path = Path(record.worktree_path) if record.worktree_path else None

        # Phase 1: Archive JSONL. Don't let a copy/parse failure block
        # the rest of teardown.
        archive_path: Path | None = None
        input_tokens = 0
        output_tokens = 0
        jsonl_archived = False
        if worktree_path is not None:
            try:
                archive_path, input_tokens, output_tokens = self._archive_jsonl(
                    task_id, worktree_path
                )
                jsonl_archived = archive_path is not None
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "teardown_worker[%s]: archive phase failed: %s",
                    task_id, exc,
                )

        # Phase 2: Kill tmux window (not just the pane). With
        # remain-on-exit=on on the storage-closet session, kill_pane
        # alone leaves a dead pane in a named window that blocks
        # subsequent create_window calls.
        task_slug = f"{project}-{task_number}"
        window_name = f"task-{task_slug}"
        window_target = f"{self._storage_closet_name}:{window_name}"
        pane_killed = False
        if not pane_id:
            # Nothing to kill — treat as already dead.
            pane_killed = True
        else:
            try:
                self._tmux.kill_window(window_target)
                pane_killed = True
            except _CalledProcessError:
                # tmux returned non-zero, typically "window not found"
                # which means it's already gone.
                logger.debug(
                    "Window %s already gone during teardown", window_target,
                )
                pane_killed = True
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "teardown_worker[%s]: kill_window phase failed for %s: %s",
                    task_id, window_target, exc,
                )

        # Phase 3: Remove worktree.
        worktree_removed = False
        if worktree_path is not None:
            try:
                worktree_removed = self._remove_worktree(worktree_path)
                if not worktree_removed:
                    logger.warning(
                        "teardown_worker[%s]: worktree removal returned "
                        "False for %s (see earlier warnings)",
                        task_id, worktree_path,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "teardown_worker[%s]: remove_worktree phase failed "
                    "for %s: %s",
                    task_id, worktree_path, exc,
                )

        # Only stamp ended_at when the pane was confirmed killed. If the
        # kill phase failed, leave ended_at=NULL so a future pass can
        # retry — but still record the archive results for observability.
        archive_str = str(archive_path) if archive_path else None
        if pane_killed:
            self._svc.end_worker_session(
                task_project=project,
                task_number=task_number,
                ended_at=_now(),
                total_input_tokens=input_tokens,
                total_output_tokens=output_tokens,
                archive_path=archive_str,
            )
        else:
            self._svc.update_worker_session_tokens(
                task_project=project,
                task_number=task_number,
                total_input_tokens=input_tokens,
                total_output_tokens=output_tokens,
                archive_path=archive_str,
            )

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
        records = self._svc.list_worker_sessions(project=project, active_only=True)
        return [self._record_to_session(r) for r in records]

    def session_for_task(self, task_id: str) -> WorkerSession | None:
        """Get the worker session bound to a task, or None."""
        project, task_number = _parse_task_id(task_id)
        record = self._svc.get_worker_session(
            task_project=project, task_number=task_number, active_only=True,
        )
        if record is None:
            return None
        return self._record_to_session(record)

    # ------------------------------------------------------------------
    # JSONL archival
    # ------------------------------------------------------------------

    def _archive_jsonl(
        self, task_id: str, worktree_path: Path
    ) -> tuple[Path | None, int, int]:
        """Copy Claude JSONL to archive location. Parse token counts.

        Claude Code writes transcripts to
        ``$CLAUDE_CONFIG_DIR/projects/<encoded-cwd>/*.jsonl`` (default
        ``~/.claude/projects/...``), not inside the worktree. The cwd
        is encoded by replacing every ``/`` and ``.`` with ``-``.

        Returns (archive_path, input_tokens, output_tokens).
        """
        jsonl_files = _find_claude_jsonl_files(worktree_path)
        if not jsonl_files:
            return None, 0, 0

        # Create archive directory
        archive_dir = (
            self._project_path / ".pollypm" / "transcripts" / "tasks" / task_id
        )
        archive_dir.mkdir(parents=True, exist_ok=True)

        total_input = 0
        total_output = 0

        for src in jsonl_files:
            dst = archive_dir / src.name
            try:
                shutil.copy2(src, dst)
            except OSError as exc:
                logger.warning("Failed to archive %s: %s", src, exc)
                continue

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
        """``git worktree add``. Returns the worktree path. Idempotent.

        Verifies the path is actually registered with git before the
        exists() fast-path returns — a crash on a previous run can leave
        a dangling directory that isn't a real worktree. All subprocess
        calls have timeouts so a hung git op can't wedge claim().
        """
        worktree_path = project_path / ".pollypm" / "worktrees" / task_slug
        branch_name = f"task/{task_slug}"

        if worktree_path.exists():
            if _worktree_is_registered(project_path, worktree_path):
                return worktree_path
            # Dangling directory from a crashed run. Prune stale metadata
            # and remove the directory so `git worktree add` can succeed.
            logger.warning(
                "Worktree path %s exists but is not registered; pruning "
                "and re-adding",
                worktree_path,
            )
            subprocess.run(
                ["git", "-C", str(project_path), "worktree", "prune"],
                check=False,
                text=True,
                capture_output=True,
                timeout=60,
            )
            if worktree_path.exists():
                # Directory is still there after prune — nuke it.
                try:
                    shutil.rmtree(worktree_path)
                except OSError as exc:
                    raise RuntimeError(
                        f"Could not remove dangling worktree dir "
                        f"{worktree_path}: {exc}"
                    ) from exc

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
            timeout=300,
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
                timeout=300,
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
            timeout=300,
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
            timeout=60,
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
    def _record_to_session(record) -> WorkerSession:
        return WorkerSession(
            task_id=f"{record.task_project}/{record.task_number}",
            agent_name=record.agent_name,
            pane_id=record.pane_id or "",
            worktree_path=Path(record.worktree_path) if record.worktree_path else Path(),
            branch_name=record.branch_name or "",
            started_at=datetime.fromisoformat(record.started_at),
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


def _worktree_is_registered(project_path: Path, worktree_path: Path) -> bool:
    """Return True if ``worktree_path`` is a registered git worktree of
    ``project_path``.

    Uses ``git worktree list --porcelain`` and compares resolved paths so
    a directory left over from a crashed run (present but unregistered)
    can be detected and cleaned up before we try to ``git worktree add``.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(project_path), "worktree", "list", "--porcelain"],
            check=False,
            text=True,
            capture_output=True,
            timeout=60,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning(
            "git worktree list failed for %s: %s", project_path, exc,
        )
        return False

    if result.returncode != 0:
        logger.warning(
            "git worktree list exit %d for %s: %s",
            result.returncode, project_path, result.stderr.strip(),
        )
        return False

    try:
        target = worktree_path.resolve()
    except OSError:
        target = worktree_path

    for line in result.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        registered = line[len("worktree "):].strip()
        try:
            registered_resolved = Path(registered).resolve()
        except OSError:
            registered_resolved = Path(registered)
        if registered_resolved == target:
            return True
    return False


def _encode_claude_cwd(cwd: Path) -> str:
    """Encode a cwd path the way Claude Code does for project JSONL dirs.

    Claude replaces every ``/`` and ``.`` in the absolute cwd with ``-``,
    e.g. ``/Users/sam/dev/foo/.pollypm/worktrees/foo-1`` becomes
    ``-Users-sam-dev-foo--pollypm-worktrees-foo-1``.
    """
    s = str(cwd)
    return s.replace("/", "-").replace(".", "-")


def _claude_config_dir() -> Path:
    """Resolve the Claude config directory.

    Honors ``$CLAUDE_CONFIG_DIR`` (set per-account by the runtime) and
    falls back to ``~/.claude``.
    """
    env_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if env_dir:
        return Path(env_dir)
    return Path.home() / ".claude"


def _find_claude_jsonl_files(worktree_path: Path) -> list[Path]:
    """Return JSONL files Claude wrote for a worktree cwd.

    Looks in ``$CLAUDE_CONFIG_DIR/projects/<encoded-cwd>/*.jsonl``.
    Returns an empty list if the directory doesn't exist.
    """
    projects_root = _claude_config_dir() / "projects"
    if not projects_root.exists():
        return []

    encoded = _encode_claude_cwd(worktree_path.resolve())
    project_dir = projects_root / encoded
    if not project_dir.exists():
        return []

    return sorted(project_dir.glob("*.jsonl"))


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
    except OSError as exc:
        logger.warning(
            "Could not read JSONL file %s for token parsing: %s",
            jsonl_path, exc,
        )

    return input_tokens, output_tokens
