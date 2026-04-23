"""CLI commands for the work service.

Provides ``pm task ...`` and ``pm flow ...`` subcommands via Typer.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import typer

from pollypm.cli_help import help_with_examples
from pollypm.errors import (
    format_cli_error,
    format_invalid_task_id_error,
    format_task_not_found_error,
    render_cli_error,
)

_TASK_APP_HELP = help_with_examples(
    "Manage work tasks.",
    [
        ("pm task next", "find work"),
        (
            "pm task claim shortlink_gen/1",
            "pick it up",
        ),
        (
            "pm task done shortlink_gen/1 --output '{\"summary\":\"done\"}'",
            "hand to review",
        ),
    ],
    trailing=(
        "Run `pm help worker` for the full worker guide with --output payload "
        "templates and common failure modes."
    ),
)
from pollypm.work.service_support import TaskNotFoundError, ValidationError

if TYPE_CHECKING:
    from pollypm.work.sqlite_service import SQLiteWorkService

task_app = typer.Typer(help=_TASK_APP_HELP)
flow_app = typer.Typer(
    help=help_with_examples(
        "Manage flow templates.",
        [
            ("pm flow list", "show registered flows"),
            ("pm flow validate standard", "check a flow template"),
        ],
    )
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DB_OPTION = typer.Option(".pollypm/state.db", "--db", help="Path to SQLite database.")
_PROJECT_OPTION = typer.Option(None, "--project", "-p", help="Project filter.")
_JSON_OPTION = typer.Option(False, "--json", help="Output as JSON.")

_TASK_NOT_FOUND_RE = re.compile(r"Task '([^']+)' not found\.")
_INVALID_TASK_ID_RE = re.compile(r"Invalid task_id '([^']+)'\.")


def _nearest_task_id(service: object | None, task_id: str) -> str | None:
    """Return the closest existing task id in the same project, if any."""
    if service is None or "/" not in task_id:
        return None
    project, raw_number = task_id.rsplit("/", 1)
    if not project or not raw_number.isdigit():
        return None
    try:
        tasks = service.list_tasks(project=project)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - best-effort error formatting path
        return None
    if not tasks:
        return None
    requested = int(raw_number)
    nearest = min(tasks, key=lambda task: (abs(task.task_number - requested), task.task_number))
    return getattr(nearest, "task_id", None)


def _render_work_service_error(exc: Exception, fn) -> str:
    """Format work-service exceptions for CLI output."""
    message = str(exc)
    service = getattr(fn, "__self__", None)

    if isinstance(exc, TaskNotFoundError):
        match = _TASK_NOT_FOUND_RE.search(message)
        if match:
            task_id = match.group(1)
            project, _, raw_number = task_id.rpartition("/")
            if project and raw_number.isdigit():
                suggestion = _nearest_task_id(service, task_id)
                return format_task_not_found_error(
                    task_id,
                    why=f"project '{project}' does not have task number {raw_number}.",
                    fix=f"run `pm task list --project {project}` to see available task ids.",
                    suggestion=suggestion if suggestion != task_id else None,
                )
            return format_task_not_found_error(
                task_id,
                why="the work-service database does not contain a task with that id.",
                fix="run `pm task list` to see available task ids.",
            )

    if isinstance(exc, ValidationError):
        match = _INVALID_TASK_ID_RE.search(message)
        if match:
            task_id = match.group(1)
            if "Expected format" in message:
                why = "work-service task ids must use the form `project/number`."
            elif "must be an integer" in message:
                why = "the text after `/` must be a whole-number task id."
            else:
                why = message
            return format_invalid_task_id_error(task_id, why=why)

    return render_cli_error(message)


def _run(fn, *args, **kwargs):
    """Call a work service method, catching errors for clean CLI output."""
    from pollypm.work.sqlite_service import WorkServiceError

    try:
        return fn(*args, **kwargs)
    except WorkServiceError as exc:
        typer.echo(_render_work_service_error(exc, fn), err=True)
        raise typer.Exit(code=1) from exc


def _resolve_db_path(db: str, project: str | None = None) -> Path:
    """Resolve the work-service database path.

    Issue #339 collapsed PollyPM's on-disk layout to exactly two scopes.
    This resolver only ever returns one of three things:

    1. An explicit ``--db`` override — a caller passing anything other
       than the default path gets that path back unchanged. This is the
       escape hatch tests and CI paths rely on to stage their own DB.
    2. ``<workspace_root>/.pollypm/state.db`` — the canonical default
       for ``pm notify``, ``pm inbox``, and every workspace-scoped task
       command. Resolved from the loaded PollyPM config so the same DB
       is returned regardless of cwd (#271). The cockpit inbox
       aggregator scans this same file, so notifications stay visible.
    3. The literal default path resolved against cwd, as a last-resort
       fallback when the config can't be loaded (fresh install, or the
       config file is missing / malformed).

    The old project-registered-lookup branch (which redirected to
    ``<project>/.pollypm/work/work.db``) is gone — project isolation is
    row-level now via the ``scope`` column on the workspace-scope DB,
    not per-file.
    """
    is_default = db == ".pollypm/state.db"

    # Explicit override wins — return the caller's path unchanged.
    if not is_default:
        db_path = Path(db)
        if db_path.exists():
            return db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        return db_path

    # Reviewer regression (tonight): tasks created via the architect's
    # per-project emit flow live in ``<project>/.pollypm/state.db`` — the
    # workspace-root default never sees them, so ``pm task get
    # polly_remote/9`` raises "not found" from Russell's session even
    # though the task exists. When a ``project`` hint is supplied AND
    # that project is registered with a real per-project state.db, we
    # route there. The workspace-root default stays in place for every
    # non-project command (notify / inbox / etc.).
    if project:
        try:
            from pollypm.config import load_config

            config = load_config()
            known = getattr(config, "projects", {}) or {}
            project_cfg = known.get(project)
            if project_cfg is not None:
                project_db = (
                    Path(project_cfg.path) / ".pollypm" / "state.db"
                )
                if project_db.exists():
                    return project_db
        except Exception:
            pass

    # Default resolution: workspace-root DB. Every `pm notify`/`pm inbox`
    # call without an explicit --db lands here, so items are always
    # visible regardless of which worktree or directory the caller
    # invoked from.
    try:
        from pollypm.config import load_config
        config = load_config()
        workspace_root = getattr(config.project, "workspace_root", None)
        if workspace_root is not None:
            candidate = Path(workspace_root) / ".pollypm" / "state.db"
            candidate.parent.mkdir(parents=True, exist_ok=True)
            return candidate
    except Exception:
        pass

    # Fallback: cwd-relative default when no config is loadable.
    db_path = Path(db)
    if db_path.exists():
        return db_path
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
    from pollypm.work.sqlite_service import SQLiteWorkService

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
            config = None
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
                config=config,
                session_service=session_service,
                storage_closet_name=storage_closet_name,
            )
            svc.set_session_manager(session_mgr)
    except Exception:  # noqa: BLE001
        pass  # SessionManager is optional — CLI still works without it

    atexit.register(svc.close)
    return svc


def _resolve_actor_for_task(
    svc: SQLiteWorkService,
    task_id: str,
    actor: str | None,
) -> str:
    """Use the task's bound actor when the CLI caller omits ``--actor``."""
    if actor:
        return actor
    try:
        task = svc.get(task_id)
    except WorkServiceError:
        return "cli"
    return svc.derive_owner(task) or "cli"


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
        # Per-task token usage aggregated across worker sessions (#86)
        typer.echo(
            f"Tokens:   in={task.total_input_tokens} "
            f"out={task.total_output_tokens} "
            f"sessions={task.session_count}"
        )
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
        # Per-task token aggregates (#86)
        "tokens_in": task.total_input_tokens,
        "tokens_out": task.total_output_tokens,
        "session_count": task.session_count,
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


def _print_task_table(
    tasks,
    as_json: bool = False,
    with_tokens: bool = False,
) -> None:
    """Print a list of tasks as a table or JSON.

    When ``with_tokens`` is set, three extra columns (tokens in, tokens
    out, session count) are appended to the text table. The JSON mode
    always includes these fields via ``_task_to_dict``.
    """
    if as_json:
        typer.echo(json.dumps([_task_to_dict(t) for t in tasks], indent=2, default=str))
        return

    if not tasks:
        typer.echo("No tasks found.")
        return

    if with_tokens:
        header = (
            f"{'ID':<20} {'Status':<14} {'Priority':<10} "
            f"{'TokIn':>8} {'TokOut':>8} {'Sess':>5}  Title"
        )
        typer.echo(header)
        typer.echo("-" * max(90, len(header)))
        for t in tasks:
            typer.echo(
                f"{t.task_id:<20} {t.work_status.value:<14} "
                f"{t.priority.value:<10} "
                f"{t.total_input_tokens:>8} {t.total_output_tokens:>8} "
                f"{t.session_count:>5}  {t.title}"
            )
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
    with_tokens: bool = typer.Option(
        False,
        "--with-tokens",
        help="Include per-task token usage columns (in/out/sessions). #86",
    ),
    db: str = _DB_OPTION,
    output_json: bool = _JSON_OPTION,
) -> None:
    """List tasks with optional filters."""
    svc = _svc(db, project=project)
    tasks = svc.list_tasks(work_status=status, project=project, assignee=assignee)
    _print_task_table(tasks, as_json=output_json, with_tokens=with_tokens)


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
        typer.echo(
            format_cli_error(
                "No updatable fields provided.",
                why="`pm task update` only changes fields you pass as flags.",
                fix=(
                    "pass one or more of `--title`, `--description`, "
                    "`--priority`, `--label`, `--role`, "
                    "`--acceptance-criteria`, `--constraints`, or "
                    "`--relevant-files`."
                ),
            ),
            err=True,
        )
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
    # Surface provisioning trouble instead of silently reporting success
    # (#243). The DB claim is authoritative — the worker can proceed
    # from an existing session — but the user needs to know the
    # dedicated session didn't spin up.
    provision_error = getattr(svc, "last_provision_error", None)
    if output_json:
        payload = _task_to_dict(task)
        if provision_error:
            payload["_provision_warning"] = provision_error
        typer.echo(json.dumps(payload, indent=2, default=str))
    else:
        typer.echo(f"Claimed {task.task_id}")
        if provision_error:
            typer.echo(
                f"\nWARNING: task claim recorded, but worker session "
                f"provisioning failed:\n"
                f"  {provision_error}\n"
                f"\n"
                f"What to do:\n"
                f"  (a) If you're already inside a worker session for this project "
                f"(provisioned by `pm task claim` or an architect started via "
                f"`pm worker-start --role architect`), just continue working — "
                f"the DB claim is in effect.\n"
                f"  (b) Otherwise, put the task on hold and retry after fixing the "
                f"underlying provisioning issue:\n"
                f"      pm task hold {task.task_id} --reason 'provision failed'\n"
                f"      pm task resume {task.task_id}",
                err=True,
            )


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
    actor: Optional[str] = typer.Option(
        None,
        "--actor",
        help="Actor approving (defaults to the task's bound reviewer/human approver)",
    ),
    db: str = _DB_OPTION,
    output_json: bool = _JSON_OPTION,
) -> None:
    """Approve at a review node."""
    svc = _svc(db, project=_project_from_task_id(task_id))
    task = _run(
        svc.approve,
        task_id,
        _resolve_actor_for_task(svc, task_id, actor),
        reason,
    )
    if output_json:
        typer.echo(json.dumps(_task_to_dict(task), indent=2, default=str))
    else:
        typer.echo(f"Approved {task.task_id} — status: {task.work_status.value}")


@task_app.command("reject")
def task_reject(
    task_id: str = typer.Argument(..., help="Task ID (project/number)"),
    reason: str = typer.Option(..., "--reason", help="Rejection reason (required)"),
    actor: Optional[str] = typer.Option(
        None,
        "--actor",
        help="Actor rejecting (defaults to the task's bound reviewer/human approver)",
    ),
    db: str = _DB_OPTION,
    output_json: bool = _JSON_OPTION,
) -> None:
    """Reject at a review node."""
    svc = _svc(db, project=_project_from_task_id(task_id))
    task = _run(
        svc.reject,
        task_id,
        _resolve_actor_for_task(svc, task_id, actor),
        reason,
    )
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
            typer.echo(
                format_cli_error(
                    f"Flow file {path} not found.",
                    why="`pm flow validate` reads a YAML file from disk.",
                    fix="pass the path to an existing `.yaml` flow file.",
                ),
                err=True,
            )
        raise typer.Exit(1)

    text = p.read_text(encoding="utf-8")
    try:
        from pollypm.work.flow_engine import parse_flow_yaml

        template = parse_flow_yaml(text)
        if output_json:
            typer.echo(json.dumps({"valid": True, "name": template.name}))
        else:
            typer.echo(f"Valid: {template.name} — {template.description}")
    except FlowValidationError as e:
        if output_json:
            typer.echo(json.dumps({"valid": False, "error": str(e)}))
        else:
            typer.echo(
                format_cli_error(
                    f"Flow {path} is invalid.",
                    why=str(e),
                    fix=f"edit {path} to satisfy the reported constraint, then rerun `pm flow validate {path}`.",
                ),
                err=True,
            )
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


# ---------------------------------------------------------------------------
# pm task pickup-log — inspect task-assignment notification history (#244)
# ---------------------------------------------------------------------------


def _parse_since(value: str | None) -> int | None:
    """Parse a ``--since`` duration like ``24h`` / ``90m`` / ``7d``.

    Returns ``None`` when ``value`` is ``None``. Raises ``typer.BadParameter``
    for unparseable input so the CLI surfaces a clean error.
    """
    if value is None:
        return None
    raw = value.strip().lower()
    if not raw:
        return None
    # Bare integer = seconds.
    if raw.isdigit():
        return int(raw)
    unit = raw[-1]
    try:
        magnitude = int(raw[:-1])
    except ValueError as exc:
        raise typer.BadParameter(
            f"--since must be an int, or a number followed by s/m/h/d (got {value!r})"
        ) from exc
    if unit == "s":
        return magnitude
    if unit == "m":
        return magnitude * 60
    if unit == "h":
        return magnitude * 3600
    if unit == "d":
        return magnitude * 86400
    raise typer.BadParameter(
        f"--since unit must be one of s/m/h/d (got {unit!r})"
    )


@task_app.command("pickup-log")
def task_pickup_log(
    since: Optional[str] = typer.Option(
        None, "--since",
        help="Only show entries newer than this (e.g. 24h, 90m, 7d).",
    ),
    project: Optional[str] = typer.Option(
        None, "--project", "-p", help="Filter by project key.",
    ),
    task_id: Optional[str] = typer.Option(
        None, "--task-id", help="Filter by task id (project/number).",
    ),
    limit: int = typer.Option(
        200, "--limit", help="Max rows to return.",
    ),
    output_json: bool = _JSON_OPTION,
) -> None:
    """Show task-assignment notifications (who got pinged about what, when).

    Reads the ``task_notifications`` dedupe table written by the
    ``task_assignment_notify`` plugin. Helpful for auditing worker /
    reviewer pickup after the E2E gap closed by issue #244.
    """
    since_seconds = _parse_since(since)

    # Resolve state_db from config — same pattern as _svc.
    try:
        from pollypm.config import DEFAULT_CONFIG_PATH, load_config, resolve_config_path
        from pollypm.storage.state import StateStore

        config_path = resolve_config_path(DEFAULT_CONFIG_PATH)
        if not config_path.exists():
            typer.echo(
                format_cli_error(
                    "No PollyPM config found.",
                    why="`pm task pickup-log` reads the state store path from your PollyPM config.",
                    fix="run `pm onboard`, `pm init`, or pass `--config <path>` before retrying.",
                ),
                err=True,
            )
            raise typer.Exit(code=1)
        config = load_config(config_path)
        store = StateStore(config.project.state_db)
    except Exception as exc:  # noqa: BLE001
        typer.echo(
            format_cli_error(
                "Could not load the PollyPM state store.",
                why=str(exc),
                fix="verify your config/state DB path, then rerun `pm task pickup-log`.",
            ),
            err=True,
        )
        raise typer.Exit(code=1) from exc

    try:
        rows = store.recent_notifications(
            since_seconds=since_seconds,
            project=project,
            task_id=task_id,
            limit=limit,
        )
    finally:
        try:
            store.close()
        except Exception:  # noqa: BLE001
            pass

    if output_json:
        typer.echo(json.dumps(rows, indent=2, default=str))
        return

    if not rows:
        typer.echo("No pickup notifications recorded.")
        return

    typer.echo(
        f"{'Notified At':<28} {'Session':<24} {'Task':<22} {'Status':<10} Project"
    )
    typer.echo("-" * 100)
    for row in rows:
        status = (row.get("delivery_status") or "")[:10]
        typer.echo(
            f"{row['notified_at']:<28} {row['session_name']:<24} "
            f"{row['task_id']:<22} {status:<10} {row.get('project', '')}"
        )
