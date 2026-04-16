"""CLI commands for the work service.

Provides ``pm task ...`` and ``pm flow ...`` subcommands via Typer.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import typer

from pollypm.work.flow_engine import parse_flow_yaml
from pollypm.work.models import (
    Artifact,
    ArtifactKind,
    OutputType,
    WorkOutput,
    WorkStatus,
)
from pollypm.work.sqlite_service import (
    SQLiteWorkService,
    InvalidTransitionError,
    TaskNotFoundError,
    ValidationError,
    WorkServiceError,
)

task_app = typer.Typer(help="Manage work tasks.")
flow_app = typer.Typer(help="Manage flow templates.")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DB_OPTION = typer.Option(".pollypm/state.db", "--db", help="Path to SQLite database.")
_PROJECT_OPTION = typer.Option(None, "--project", "-p", help="Project filter.")
_JSON_OPTION = typer.Option(False, "--json", help="Output as JSON.")


def _run(fn, *args, **kwargs):
    """Call a work service method, catching errors for clean CLI output."""
    try:
        return fn(*args, **kwargs)
    except WorkServiceError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


def _resolve_db_path(db: str, project: str | None = None) -> Path:
    """Resolve the database path, trying the pollypm config for project root.

    When *project* is given and the default ``--db`` wasn't overridden,
    resolve to that project's ``.pollypm/state.db``.

    When the default relative ``--db .pollypm/state.db`` doesn't exist in the
    cwd, fall back to the pollypm config's known project paths so that agents
    running in workspace-root (e.g. the operator in ``/Users/sam/dev``) find
    the project-level database.
    """
    is_default = db == ".pollypm/state.db"

    # If a specific project is requested, always use that project's db
    if project and is_default:
        try:
            from pollypm.config import load_config
            config = load_config()
            # Normalize hyphens to underscores for flexible matching
            normalized = project.replace("-", "_")
            if project in config.projects:
                pass  # exact match
            elif normalized in config.projects:
                project = normalized
            if project in config.projects:
                candidate = config.projects[project].path / ".pollypm" / "state.db"
                candidate.parent.mkdir(parents=True, exist_ok=True)
                return candidate
        except Exception:
            pass

    db_path = Path(db)
    if db_path.exists():
        return db_path

    # Fall back to any project with an existing db
    if is_default:
        try:
            from pollypm.config import load_config
            config = load_config()
            for proj in config.projects.values():
                candidate = proj.path / ".pollypm" / "state.db"
                if candidate.exists():
                    return candidate
        except Exception:
            pass

    db_path.parent.mkdir(parents=True, exist_ok=True)
    return db_path


def _project_from_task_id(task_id: str) -> str | None:
    """Extract project name from a task_id like 'project/number'."""
    if "/" in task_id:
        return task_id.split("/", 1)[0]
    return None


def _svc(db: str, project: str | None = None) -> SQLiteWorkService:
    import atexit

    from pollypm.work.sync import SyncManager
    from pollypm.work.sync_file import FileSyncAdapter

    db_path = _resolve_db_path(db, project=project)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    project_root = db_path.parent.parent
    sync = SyncManager()
    sync.register(FileSyncAdapter(issues_root=project_root / "issues"))

    svc = SQLiteWorkService(
        db_path=db_path, sync_manager=sync, project_path=project_root,
    )

    # Wire up the session manager for per-task worker lifecycle
    try:
        from pollypm.session_services import create_tmux_client
        from pollypm.work.session_manager import SessionManager
        if project_root.exists() and (project_root / ".git").exists():
            # Try to route through the configured SessionService so per-task
            # workers pick up stabilization, initial_input handling, and
            # storage-closet naming from config. Fall back to a raw
            # TmuxClient if config/plugin resolution fails.
            session_service = None
            storage_closet_name = "pollypm-storage-closet"
            try:
                from pollypm.config import load_config
                from pollypm.session_services.tmux import TmuxSessionService
                from pollypm.storage.state import StateStore
                config = load_config()
                storage_closet_name = (
                    f"{config.project.tmux_session}-storage-closet"
                )
                store = StateStore(config.project.state_db)
                session_service = TmuxSessionService(config=config, store=store)
            except Exception:  # noqa: BLE001
                pass
            session_mgr = SessionManager(
                tmux_client=create_tmux_client(),
                work_service=svc,
                project_path=project_root,
                session_service=session_service,
                storage_closet_name=storage_closet_name,
            )
            svc.set_session_manager(session_mgr)
    except Exception:  # noqa: BLE001
        pass  # SessionManager is optional — CLI still works without it

    atexit.register(svc.close)
    return svc


def _print_task(task, as_json: bool = False) -> None:
    """Print a single task."""
    if as_json:
        typer.echo(json.dumps(_task_to_dict(task), indent=2, default=str))
    else:
        typer.echo(f"ID:       {task.task_id}")
        typer.echo(f"Title:    {task.title}")
        typer.echo(f"Status:   {task.work_status.value}")
        typer.echo(f"Priority: {task.priority.value}")
        typer.echo(f"Project:  {task.project}")
        typer.echo(f"Type:     {task.type.value}")
        if task.assignee:
            typer.echo(f"Assignee: {task.assignee}")
        if task.current_node_id:
            typer.echo(f"Node:     {task.current_node_id}")
        if task.description:
            typer.echo(f"Desc:     {task.description}")
        if task.roles:
            typer.echo(f"Roles:    {json.dumps(task.roles)}")
        if task.executions:
            typer.echo("Executions:")
            for ex in task.executions:
                status = ex.status.value if hasattr(ex.status, "value") else ex.status
                line = f"  {ex.node_id} v{ex.visit}: {status}"
                if ex.decision:
                    dec = ex.decision.value if hasattr(ex.decision, "value") else ex.decision
                    line += f" ({dec})"
                typer.echo(line)
        if task.context:
            typer.echo("Context:")
            for c in task.context:
                typer.echo(f"  [{c.actor}] {c.text}")


def _task_to_dict(task) -> dict:
    """Serialize a task to a JSON-friendly dict."""
    return {
        "task_id": task.task_id,
        "project": task.project,
        "task_number": task.task_number,
        "title": task.title,
        "type": task.type.value,
        "work_status": task.work_status.value,
        "priority": task.priority.value,
        "assignee": task.assignee,
        "current_node_id": task.current_node_id,
        "description": task.description,
        "acceptance_criteria": task.acceptance_criteria,
        "constraints": task.constraints,
        "relevant_files": task.relevant_files,
        "requires_human_review": task.requires_human_review,
        "roles": task.roles,
        "labels": task.labels,
        "created_at": str(task.created_at) if task.created_at else None,
        "updated_at": str(task.updated_at) if task.updated_at else None,
        "executions": [
            {
                "node_id": ex.node_id,
                "visit": ex.visit,
                "status": ex.status.value if hasattr(ex.status, "value") else ex.status,
                "decision": (ex.decision.value if ex.decision and hasattr(ex.decision, "value") else ex.decision),
                "decision_reason": ex.decision_reason,
            }
            for ex in task.executions
        ],
    }


def _print_task_table(tasks, as_json: bool = False) -> None:
    """Print a list of tasks as a table or JSON."""
    if as_json:
        typer.echo(json.dumps([_task_to_dict(t) for t in tasks], indent=2, default=str))
        return

    if not tasks:
        typer.echo("No tasks found.")
        return

    # Simple table
    typer.echo(f"{'ID':<20} {'Status':<14} {'Priority':<10} {'Title'}")
    typer.echo("-" * 70)
    for t in tasks:
        typer.echo(f"{t.task_id:<20} {t.work_status.value:<14} {t.priority.value:<10} {t.title}")


def _parse_role(value: str) -> tuple[str, str]:
    """Parse 'key=value' into (key, value)."""
    if "=" not in value:
        raise typer.BadParameter(f"Role must be key=value, got: {value}")
    k, v = value.split("=", 1)
    return k.strip(), v.strip()


def _sync_commits_to_task_branch(task_id: str) -> None:
    """Sync worker commits to the task branch before signaling done.

    When a persistent worker picks up a task, it works in its own worktree
    (pa/worker_* branch) rather than the task worktree (task/* branch).
    This causes reviewers to find zero commits on the task branch.

    This function detects the mismatch and cherry-picks the worker's recent
    commits onto the task branch so the reviewer can find them.
    """
    import os
    import subprocess

    project, number = task_id.split("/", 1) if "/" in task_id else (task_id, "")
    if not number:
        return

    task_slug = f"{project}-{number}"
    task_branch = f"task/{task_slug}"
    cwd = os.getcwd()

    # Check if we're in a git repo
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return

    # Get current branch
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return
    current_branch = result.stdout.strip()

    # If we're already on the task branch, nothing to sync
    if current_branch == task_branch:
        return

    # Check if the task branch exists
    result = subprocess.run(
        ["git", "rev-parse", "--verify", task_branch],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return  # Task branch doesn't exist — nothing to sync to

    # Find commits on current branch that aren't on main
    result = subprocess.run(
        ["git", "log", f"main..{current_branch}", "--oneline", "--no-merges"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return  # No commits to sync

    # Check if the worker's latest commit is already on the task branch
    worker_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True, text=True, check=False,
    )
    if worker_head.returncode == 0:
        is_ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", worker_head.stdout.strip(), task_branch],
            capture_output=True, text=True, check=False,
        )
        if is_ancestor.returncode == 0:
            return  # Worker's commits are already on the task branch

    # Get the project root (top of git repo)
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return
    project_root = result.stdout.strip()

    # Check if task worktree exists
    task_worktree = os.path.join(project_root, ".pollypm", "worktrees", task_slug)
    if os.path.isdir(task_worktree):
        # Merge current branch into task worktree
        merge_result = subprocess.run(
            ["git", "-C", task_worktree, "merge", current_branch, "--no-edit"],
            capture_output=True, text=True, check=False,
        )
        if merge_result.returncode == 0:
            typer.echo(f"Synced commits from {current_branch} to {task_branch}")
        else:
            # Try cherry-pick of just the new commits instead
            commits = subprocess.run(
                ["git", "log", f"main..{current_branch}", "--format=%H", "--reverse", "--no-merges"],
                capture_output=True, text=True, check=False,
            )
            if commits.returncode == 0 and commits.stdout.strip():
                hashes = commits.stdout.strip().split("\n")
                cp = subprocess.run(
                    ["git", "-C", task_worktree, "cherry-pick"] + hashes,
                    capture_output=True, text=True, check=False,
                )
                if cp.returncode == 0:
                    typer.echo(f"Cherry-picked {len(hashes)} commit(s) to {task_branch}")
                else:
                    typer.echo(f"Warning: could not sync commits to {task_branch}: {cp.stderr.strip()}")
    else:
        # No task worktree — try updating the branch ref directly
        current_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, check=False,
        )
        if current_head.returncode == 0:
            update = subprocess.run(
                ["git", "update-ref", f"refs/heads/{task_branch}", current_head.stdout.strip()],
                capture_output=True, text=True, check=False,
            )
            if update.returncode == 0:
                typer.echo(f"Updated {task_branch} to match {current_branch}")


# ---------------------------------------------------------------------------
# Task commands
# ---------------------------------------------------------------------------


@task_app.command("create")
def task_create(
    title: str = typer.Argument(..., help="Task title"),
    project: str = typer.Option(..., "--project", "-p", help="Project name"),
    flow: str = typer.Option("standard", "--flow", "-f", help="Flow template name"),
    role: Optional[list[str]] = typer.Option(None, "--role", "-r", help="Role assignment (key=value)"),
    priority: str = typer.Option("normal", "--priority", help="Priority: critical, high, normal, low"),
    description: str = typer.Option("", "--description", "-d", help="Task description"),
    task_type: str = typer.Option("task", "--type", "-t", help="Task type: task, bug, spike, epic, subtask"),
    label: Optional[list[str]] = typer.Option(
        None, "--label", help="Label to attach (repeatable).",
    ),
    acceptance_criteria: Optional[list[str]] = typer.Option(
        None,
        "--acceptance-criteria",
        help="Acceptance criteria line. Repeatable — multiple values are joined with newlines.",
    ),
    constraints: Optional[list[str]] = typer.Option(
        None,
        "--constraints",
        help="Constraint line (what NOT to do). Repeatable — joined with newlines.",
    ),
    relevant_files: Optional[list[str]] = typer.Option(
        None,
        "--relevant-files",
        help="Explicit file path / pattern the worker should touch (repeatable).",
    ),
    requires_human_review: bool = typer.Option(
        False,
        "--requires-human-review",
        help="Gate queue() transition on human sign-off via inbox.",
    ),
    db: str = _DB_OPTION,
    output_json: bool = _JSON_OPTION,
) -> None:
    """Create a new task in draft state."""
    roles: dict[str, str] = {}
    for r in role or []:
        k, v = _parse_role(r)
        roles[k] = v

    ac_text = "\n".join(acceptance_criteria) if acceptance_criteria else None
    constraints_text = "\n".join(constraints) if constraints else None

    svc = _svc(db, project=project)
    task = svc.create(
        title=title,
        description=description,
        type=task_type,
        project=project,
        flow_template=flow,
        roles=roles,
        priority=priority,
        acceptance_criteria=ac_text,
        constraints=constraints_text,
        relevant_files=list(relevant_files) if relevant_files else None,
        labels=list(label) if label else None,
        requires_human_review=requires_human_review,
    )
    if output_json:
        typer.echo(json.dumps(_task_to_dict(task), indent=2, default=str))
    else:
        typer.echo(f"Created {task.task_id}")


@task_app.command("get")
def task_get(
    task_id: str = typer.Argument(..., help="Task ID (project/number)"),
    db: str = _DB_OPTION,
    output_json: bool = _JSON_OPTION,
) -> None:
    """Get full details of a task."""
    svc = _svc(db, project=_project_from_task_id(task_id))
    task = _run(svc.get, task_id)
    # Also load context
    task.context = svc.get_context(task_id)
    _print_task(task, as_json=output_json)


@task_app.command("list")
def task_list(
    status: Optional[str] = typer.Option(None, "--status", "-s", help="Filter by work_status"),
    project: Optional[str] = _PROJECT_OPTION,
    assignee: Optional[str] = typer.Option(None, "--assignee", "-a", help="Filter by assignee"),
    db: str = _DB_OPTION,
    output_json: bool = _JSON_OPTION,
) -> None:
    """List tasks with optional filters."""
    svc = _svc(db, project=project)
    tasks = svc.list_tasks(work_status=status, project=project, assignee=assignee)
    _print_task_table(tasks, as_json=output_json)


@task_app.command("update")
def task_update(
    task_id: str = typer.Argument(..., help="Task ID (project/number)"),
    title: Optional[str] = typer.Option(None, "--title", help="New title"),
    description: Optional[str] = typer.Option(None, "--description", "-d", help="New description"),
    priority: Optional[str] = typer.Option(None, "--priority", help="New priority: critical, high, normal, low"),
    label: Optional[list[str]] = typer.Option(None, "--label", help="Replace labels (repeatable)"),
    role: Optional[list[str]] = typer.Option(None, "--role", "-r", help="Replace roles (key=value, repeatable)"),
    acceptance_criteria: Optional[str] = typer.Option(None, "--acceptance-criteria", help="New acceptance criteria"),
    constraints: Optional[str] = typer.Option(None, "--constraints", help="New constraints"),
    relevant_files: Optional[list[str]] = typer.Option(None, "--relevant-files", help="Replace relevant files (repeatable)"),
    db: str = _DB_OPTION,
    output_json: bool = _JSON_OPTION,
) -> None:
    """Update mutable fields on a task.

    Each provided flag replaces the corresponding value. Omitted flags are
    left unchanged. Lists (labels, roles, relevant_files) are replaced
    wholesale — not merged.
    """
    fields: dict[str, object] = {}
    if title is not None:
        fields["title"] = title
    if description is not None:
        fields["description"] = description
    if priority is not None:
        fields["priority"] = priority
    if label is not None:
        fields["labels"] = list(label)
    if role is not None:
        roles: dict[str, str] = {}
        for r in role:
            k, v = _parse_role(r)
            roles[k] = v
        fields["roles"] = roles
    if acceptance_criteria is not None:
        fields["acceptance_criteria"] = acceptance_criteria
    if constraints is not None:
        fields["constraints"] = constraints
    if relevant_files is not None:
        fields["relevant_files"] = list(relevant_files)

    if not fields:
        typer.echo("Error: no updatable fields provided.", err=True)
        raise typer.Exit(code=1)

    svc = _svc(db, project=_project_from_task_id(task_id))
    task = _run(svc.update, task_id, **fields)
    if output_json:
        typer.echo(json.dumps(_task_to_dict(task), indent=2, default=str))
    else:
        typer.echo(f"Updated {task.task_id} — fields: {', '.join(sorted(fields))}")


@task_app.command("queue")
def task_queue(
    task_id: str = typer.Argument(..., help="Task ID (project/number)"),
    actor: str = typer.Option("cli", "--actor", help="Actor performing the action"),
    skip_gates: bool = typer.Option(False, "--skip-gates", help="Override gate checks (use with caution)"),
    db: str = _DB_OPTION,
    output_json: bool = _JSON_OPTION,
) -> None:
    """Move a task from draft to queued."""
    svc = _svc(db, project=_project_from_task_id(task_id))
    task = _run(svc.queue, task_id, actor, skip_gates=skip_gates)
    if output_json:
        typer.echo(json.dumps(_task_to_dict(task), indent=2, default=str))
    else:
        typer.echo(f"Queued {task.task_id}")


@task_app.command("claim")
def task_claim(
    task_id: str = typer.Argument(..., help="Task ID (project/number)"),
    actor: str = typer.Option("worker", "--actor", help="Actor claiming the task"),
    skip_gates: bool = typer.Option(False, "--skip-gates", help="Override gate checks (use with caution)"),
    db: str = _DB_OPTION,
    output_json: bool = _JSON_OPTION,
) -> None:
    """Claim a queued task and start the flow."""
    svc = _svc(db, project=_project_from_task_id(task_id))
    task = _run(svc.claim, task_id, actor, skip_gates=skip_gates)
    if output_json:
        typer.echo(json.dumps(_task_to_dict(task), indent=2, default=str))
    else:
        typer.echo(f"Claimed {task.task_id}")


@task_app.command("done")
def task_done(
    task_id: str = typer.Argument(..., help="Task ID (project/number)"),
    output: str = typer.Option(..., "--output", "-o", help="Work output as JSON string"),
    actor: str = typer.Option("worker", "--actor", help="Actor completing the node"),
    db: str = _DB_OPTION,
    output_json: bool = _JSON_OPTION,
) -> None:
    """Signal that the current work node is complete."""
    svc = _svc(db, project=_project_from_task_id(task_id))
    wo_dict = json.loads(output)
    # Sync worker commits to task branch before state transition
    _sync_commits_to_task_branch(task_id)
    task = _run(svc.node_done, task_id, actor, wo_dict)
    if output_json:
        typer.echo(json.dumps(_task_to_dict(task), indent=2, default=str))
    else:
        typer.echo(f"Node done on {task.task_id} — status: {task.work_status.value}")


@task_app.command("approve")
def task_approve(
    task_id: str = typer.Argument(..., help="Task ID (project/number)"),
    reason: Optional[str] = typer.Option(None, "--reason", help="Approval reason"),
    actor: str = typer.Option("cli", "--actor", help="Actor approving"),
    db: str = _DB_OPTION,
    output_json: bool = _JSON_OPTION,
) -> None:
    """Approve at a review node."""
    svc = _svc(db, project=_project_from_task_id(task_id))
    task = _run(svc.approve, task_id, actor, reason)
    if output_json:
        typer.echo(json.dumps(_task_to_dict(task), indent=2, default=str))
    else:
        typer.echo(f"Approved {task.task_id} — status: {task.work_status.value}")


@task_app.command("reject")
def task_reject(
    task_id: str = typer.Argument(..., help="Task ID (project/number)"),
    reason: str = typer.Option(..., "--reason", help="Rejection reason (required)"),
    actor: str = typer.Option("cli", "--actor", help="Actor rejecting"),
    db: str = _DB_OPTION,
    output_json: bool = _JSON_OPTION,
) -> None:
    """Reject at a review node."""
    svc = _svc(db, project=_project_from_task_id(task_id))
    task = _run(svc.reject, task_id, actor, reason)
    if output_json:
        typer.echo(json.dumps(_task_to_dict(task), indent=2, default=str))
    else:
        typer.echo(f"Rejected {task.task_id} — status: {task.work_status.value}")


@task_app.command("next")
def task_next(
    project: Optional[str] = _PROJECT_OPTION,
    agent: Optional[str] = typer.Option(None, "--agent", help="Filter by agent (worker role)"),
    db: str = _DB_OPTION,
    output_json: bool = _JSON_OPTION,
) -> None:
    """Return the highest-priority queued+unblocked task."""
    svc = _svc(db, project=project)
    task = svc.next(agent=agent, project=project)
    if task is None:
        if output_json:
            typer.echo("null")
        else:
            typer.echo("No tasks available.")
        return
    if output_json:
        typer.echo(json.dumps(_task_to_dict(task), indent=2, default=str))
    else:
        _print_task(task)


@task_app.command("cancel")
def task_cancel(
    task_id: str = typer.Argument(..., help="Task ID (project/number)"),
    reason: str = typer.Option(..., "--reason", help="Cancellation reason (required)"),
    actor: str = typer.Option("cli", "--actor", help="Actor cancelling"),
    db: str = _DB_OPTION,
    output_json: bool = _JSON_OPTION,
) -> None:
    """Cancel a task."""
    svc = _svc(db, project=_project_from_task_id(task_id))
    task = _run(svc.cancel, task_id, actor, reason)
    if output_json:
        typer.echo(json.dumps(_task_to_dict(task), indent=2, default=str))
    else:
        typer.echo(f"Cancelled {task.task_id}")


@task_app.command("hold")
def task_hold(
    task_id: str = typer.Argument(..., help="Task ID (project/number)"),
    actor: str = typer.Option("cli", "--actor", help="Actor"),
    db: str = _DB_OPTION,
    output_json: bool = _JSON_OPTION,
) -> None:
    """Put a task on hold."""
    svc = _svc(db, project=_project_from_task_id(task_id))
    task = _run(svc.hold, task_id, actor)
    if output_json:
        typer.echo(json.dumps(_task_to_dict(task), indent=2, default=str))
    else:
        typer.echo(f"On hold: {task.task_id}")


@task_app.command("resume")
def task_resume(
    task_id: str = typer.Argument(..., help="Task ID (project/number)"),
    actor: str = typer.Option("cli", "--actor", help="Actor"),
    db: str = _DB_OPTION,
    output_json: bool = _JSON_OPTION,
) -> None:
    """Resume an on-hold task."""
    svc = _svc(db, project=_project_from_task_id(task_id))
    task = _run(svc.resume, task_id, actor)
    if output_json:
        typer.echo(json.dumps(_task_to_dict(task), indent=2, default=str))
    else:
        typer.echo(f"Resumed {task.task_id}")


@task_app.command("link")
def task_link(
    from_id: str = typer.Argument(..., help="Source task ID"),
    to_id: str = typer.Argument(..., help="Target task ID"),
    kind: str = typer.Option("blocks", "--kind", "-k", help="Link kind: blocks, relates_to, supersedes, parent"),
    db: str = _DB_OPTION,
) -> None:
    """Create a relationship between two tasks."""
    svc = _svc(db, project=_project_from_task_id(from_id))
    _run(svc.link, from_id, to_id, kind)
    typer.echo(f"Linked {from_id} --{kind}--> {to_id}")


@task_app.command("unlink")
def task_unlink(
    to_id: str = typer.Argument(..., help="Target task ID (the relationship's destination)"),
    from_id: str = typer.Option(..., "--from", help="Source task ID (the relationship's origin)"),
    kind: str = typer.Option("blocks", "--kind", "-k", help="Link kind: blocks, relates_to, supersedes, parent"),
    db: str = _DB_OPTION,
) -> None:
    """Remove a relationship between two tasks."""
    svc = _svc(db, project=_project_from_task_id(from_id))
    _run(svc.unlink, from_id, to_id, kind)
    typer.echo(f"Unlinked {from_id} --{kind}--> {to_id}")


@task_app.command("block")
def task_block(
    task_id: str = typer.Argument(..., help="Task ID to mark as blocked"),
    blocker: str = typer.Option(..., "--blocker", help="Blocker task ID"),
    actor: str = typer.Option("cli", "--actor", help="Actor performing the block"),
    db: str = _DB_OPTION,
    output_json: bool = _JSON_OPTION,
) -> None:
    """Mark a task as blocked by another task."""
    svc = _svc(db, project=_project_from_task_id(task_id))
    task = _run(svc.block, task_id, actor, blocker)
    if output_json:
        typer.echo(json.dumps(_task_to_dict(task), indent=2, default=str))
    else:
        typer.echo(f"Blocked {task.task_id} on {blocker}")


@task_app.command("dependents")
def task_dependents(
    task_id: str = typer.Argument(..., help="Task ID"),
    db: str = _DB_OPTION,
    output_json: bool = _JSON_OPTION,
) -> None:
    """Show tasks transitively blocked by this task."""
    svc = _svc(db, project=_project_from_task_id(task_id))
    deps = _run(svc.dependents, task_id)
    if output_json:
        typer.echo(json.dumps([_task_to_dict(t) for t in deps], indent=2, default=str))
        return
    if not deps:
        typer.echo(f"No dependents for {task_id}.")
        return
    typer.echo(f"Dependents of {task_id}:")
    for t in deps:
        typer.echo(f"  {t.task_id:<20} {t.work_status.value:<14} {t.title}")


@task_app.command("get-execution")
def task_get_execution(
    task_id: str = typer.Argument(..., help="Task ID (project/number)"),
    node: Optional[str] = typer.Option(None, "--node", help="Filter by node_id"),
    visit: Optional[int] = typer.Option(None, "--visit", help="Filter by visit number"),
    db: str = _DB_OPTION,
    output_json: bool = _JSON_OPTION,
) -> None:
    """Show flow node execution records for a task."""
    svc = _svc(db, project=_project_from_task_id(task_id))
    executions = _run(svc.get_execution, task_id, node, visit)

    def _exec_to_dict(ex) -> dict:
        status = ex.status.value if hasattr(ex.status, "value") else ex.status
        decision = (
            ex.decision.value
            if ex.decision and hasattr(ex.decision, "value")
            else ex.decision
        )
        wo = None
        if ex.work_output:
            wo = {
                "type": ex.work_output.type.value
                if hasattr(ex.work_output.type, "value")
                else ex.work_output.type,
                "summary": ex.work_output.summary,
                "artifacts": [
                    {
                        "kind": a.kind.value if hasattr(a.kind, "value") else a.kind,
                        "description": a.description,
                        "ref": a.ref,
                        "path": a.path,
                        "external_ref": a.external_ref,
                    }
                    for a in ex.work_output.artifacts
                ],
            }
        return {
            "node_id": ex.node_id,
            "visit": ex.visit,
            "status": status,
            "decision": decision,
            "decision_reason": ex.decision_reason,
            "started_at": str(ex.started_at) if ex.started_at else None,
            "completed_at": str(ex.completed_at) if ex.completed_at else None,
            "work_output": wo,
        }

    if output_json:
        typer.echo(
            json.dumps([_exec_to_dict(e) for e in executions], indent=2, default=str)
        )
        return

    if not executions:
        typer.echo("No execution records.")
        return

    for ex in executions:
        status = ex.status.value if hasattr(ex.status, "value") else ex.status
        line = f"{ex.node_id} v{ex.visit}: {status}"
        if ex.decision:
            dec = ex.decision.value if hasattr(ex.decision, "value") else ex.decision
            line += f" ({dec})"
            if ex.decision_reason:
                line += f" — {ex.decision_reason}"
        typer.echo(line)
        if ex.work_output:
            typer.echo(f"  output: {ex.work_output.summary}")


@task_app.command("validate-advance")
def task_validate_advance(
    task_id: str = typer.Argument(..., help="Task ID (project/number)"),
    actor: str = typer.Option(..., "--actor", help="Actor attempting to advance"),
    db: str = _DB_OPTION,
    output_json: bool = _JSON_OPTION,
) -> None:
    """Dry-run: would this actor be allowed to advance the current node?

    Evaluates all gates on the current node plus an actor-vs-role check
    without modifying any state. Exits non-zero if any hard gate fails.
    """
    svc = _svc(db, project=_project_from_task_id(task_id))
    results = _run(svc.validate_advance, task_id, actor)

    def _result_to_dict(r) -> dict:
        return {
            "gate_name": r.gate_name,
            "gate_type": r.gate_type,
            "passed": r.passed,
            "reason": r.reason,
        }

    hard_fails = [r for r in results if not r.passed and r.gate_type != "soft"]

    if output_json:
        typer.echo(
            json.dumps(
                {
                    "task_id": task_id,
                    "actor": actor,
                    "results": [_result_to_dict(r) for r in results],
                    "would_advance": not hard_fails,
                },
                indent=2,
            )
        )
    else:
        if not results:
            typer.echo("No active node — nothing to validate.")
            return
        for r in results:
            mark = "PASS" if r.passed else "FAIL"
            gtype = f" ({r.gate_type})" if r.gate_type else ""
            name = r.gate_name or "(unnamed)"
            typer.echo(f"  {mark} {name}{gtype}: {r.reason}")
        if hard_fails:
            typer.echo(f"Would NOT advance: {len(hard_fails)} hard gate(s) failing.")
        else:
            typer.echo("Would advance.")

    if hard_fails:
        raise typer.Exit(code=1)


@task_app.command("context")
def task_context(
    task_id: str = typer.Argument(..., help="Task ID (project/number)"),
    text: str = typer.Argument(..., help="Context message text"),
    actor: str = typer.Option("worker", "--actor", help="Actor"),
    db: str = _DB_OPTION,
    output_json: bool = _JSON_OPTION,
) -> None:
    """Add a context entry to a task."""
    svc = _svc(db, project=_project_from_task_id(task_id))
    entry = svc.add_context(task_id, actor, text)
    if output_json:
        typer.echo(json.dumps({
            "actor": entry.actor,
            "text": entry.text,
            "timestamp": str(entry.timestamp),
        }, indent=2))
    else:
        typer.echo(f"Added context to {task_id}")


@task_app.command("status")
def task_status(
    task_id: str = typer.Argument(..., help="Task ID (project/number)"),
    db: str = _DB_OPTION,
    output_json: bool = _JSON_OPTION,
) -> None:
    """Pretty-printed summary: node, owner, status, context, executions."""
    svc = _svc(db, project=_project_from_task_id(task_id))
    task = _run(svc.get, task_id)
    task.context = svc.get_context(task_id, limit=5)
    owner = svc.derive_owner(task)

    if output_json:
        d = _task_to_dict(task)
        d["owner"] = owner
        d["recent_context"] = [
            {"actor": c.actor, "text": c.text, "timestamp": str(c.timestamp)}
            for c in task.context
        ]
        typer.echo(json.dumps(d, indent=2, default=str))
    else:
        typer.echo(f"Task:   {task.task_id} — {task.title}")
        typer.echo(f"Status: {task.work_status.value}")
        typer.echo(f"Node:   {task.current_node_id or '(none)'}")
        typer.echo(f"Owner:  {owner or '(none)'}")
        if task.executions:
            typer.echo("Executions:")
            for ex in task.executions:
                status = ex.status.value if hasattr(ex.status, "value") else ex.status
                line = f"  {ex.node_id} v{ex.visit}: {status}"
                if ex.decision:
                    dec = ex.decision.value if hasattr(ex.decision, "value") else ex.decision
                    line += f" ({dec})"
                typer.echo(line)
        if task.context:
            typer.echo("Recent context:")
            for c in task.context:
                typer.echo(f"  [{c.actor}] {c.text}")


@task_app.command("counts")
def task_counts(
    project: Optional[str] = _PROJECT_OPTION,
    db: str = _DB_OPTION,
    output_json: bool = _JSON_OPTION,
) -> None:
    """Show task counts by status."""
    svc = _svc(db, project=project)
    counts = svc.state_counts(project=project)
    if output_json:
        typer.echo(json.dumps(counts, indent=2))
    else:
        typer.echo(f"{'Status':<14} {'Count':>6}")
        typer.echo("-" * 22)
        for status, count in sorted(counts.items()):
            typer.echo(f"{status:<14} {count:>6}")


@task_app.command("mine")
def task_mine(
    agent: str = typer.Option(..., "--agent", help="Agent name"),
    db: str = _DB_OPTION,
    output_json: bool = _JSON_OPTION,
) -> None:
    """Show tasks where agent owns the current node."""
    svc = _svc(db)
    tasks = svc.my_tasks(agent)
    _print_task_table(tasks, as_json=output_json)


@task_app.command("blocked")
def task_blocked(
    project: Optional[str] = _PROJECT_OPTION,
    db: str = _DB_OPTION,
    output_json: bool = _JSON_OPTION,
) -> None:
    """Show blocked tasks."""
    svc = _svc(db, project=project)
    tasks = svc.blocked_tasks(project=project)
    _print_task_table(tasks, as_json=output_json)


# ---------------------------------------------------------------------------
# Flow commands
# ---------------------------------------------------------------------------


@flow_app.command("list")
def flow_list(
    project: Optional[str] = _PROJECT_OPTION,
    db: str = _DB_OPTION,
    output_json: bool = _JSON_OPTION,
) -> None:
    """List available flow templates."""
    svc = _svc(db, project=project)
    flows = svc.available_flows(project=project)
    if output_json:
        typer.echo(json.dumps(
            [{"name": f.name, "description": f.description} for f in flows],
            indent=2,
        ))
    else:
        if not flows:
            typer.echo("No flows found.")
            return
        typer.echo(f"{'Name':<20} {'Description'}")
        typer.echo("-" * 60)
        for f in flows:
            typer.echo(f"{f.name:<20} {f.description}")


@flow_app.command("validate")
def flow_validate(
    path: str = typer.Argument(..., help="Path to a flow YAML file"),
    output_json: bool = _JSON_OPTION,
) -> None:
    """Validate a flow YAML file."""
    from pollypm.work.flow_engine import FlowValidationError

    p = Path(path)
    if not p.is_file():
        if output_json:
            typer.echo(json.dumps({"valid": False, "error": f"File not found: {path}"}))
        else:
            typer.echo(f"Error: file not found: {path}")
        raise typer.Exit(1)

    text = p.read_text(encoding="utf-8")
    try:
        template = parse_flow_yaml(text)
        if output_json:
            typer.echo(json.dumps({"valid": True, "name": template.name}))
        else:
            typer.echo(f"Valid: {template.name} — {template.description}")
    except FlowValidationError as e:
        if output_json:
            typer.echo(json.dumps({"valid": False, "error": str(e)}))
        else:
            typer.echo(f"Invalid: {e}")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Sync / Migration commands
# ---------------------------------------------------------------------------


@task_app.command("sync")
def task_sync(
    project: Optional[str] = _PROJECT_OPTION,
    issues_dir: str = typer.Option("issues", "--issues-dir", help="Path to issues directory for file sync"),
    db: str = _DB_OPTION,
    output_json: bool = _JSON_OPTION,
) -> None:
    """Run all registered sync adapters for all tasks (or project-filtered)."""
    from pollypm.work.sync import SyncManager
    from pollypm.work.sync_file import FileSyncAdapter

    svc = _svc(db)
    tasks = svc.list_tasks(project=project)

    manager = SyncManager()
    manager.register(FileSyncAdapter(issues_root=Path(issues_dir)))

    synced = 0
    for task in tasks:
        manager.on_create(task)
        synced += 1

    if output_json:
        typer.echo(json.dumps({"synced": synced}))
    else:
        typer.echo(f"Synced {synced} task(s).")


@task_app.command("migrate")
def task_migrate(
    issues_dir: str = typer.Argument(..., help="Path to issues directory"),
    project: str = typer.Option(..., "--project", "-p", help="Target project name"),
    flow: str = typer.Option("standard", "--flow", "-f", help="Flow template name"),
    db: str = _DB_OPTION,
    output_json: bool = _JSON_OPTION,
) -> None:
    """Import existing issues/ directories into the work service."""
    from pollypm.work.migrate import migrate_issues

    svc = _svc(db)
    result = migrate_issues(Path(issues_dir), svc, project=project, flow=flow)

    if output_json:
        typer.echo(json.dumps({
            "created": result.created,
            "skipped": result.skipped,
            "errors": result.errors,
        }, indent=2))
    else:
        typer.echo(f"Created: {result.created}")
        typer.echo(f"Skipped: {result.skipped}")
        if result.errors:
            typer.echo(f"Errors:  {len(result.errors)}")
            for err in result.errors:
                typer.echo(f"  - {err}")
