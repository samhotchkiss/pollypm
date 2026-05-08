"""``pm project`` CLI — planner-backed project lifecycle entry points.

Spec: ``docs/planner-plugin-spec.md`` §9–§10.

Subcommands:

* ``pm project new <path>`` — register a project (delegates to the core
  ``pollypm.projects.register_project``) and then prompt "Run the
  planner now? (Y/n)". Declining exits cleanly; accepting triggers the
  same path as ``pm project plan``.
* ``pm project plan [project]`` — create a task with ``flow=plan_project``.
* ``pm project replan [project]`` — create a task with ``flow=plan_project``;
  the architect's stage-0 research loop reads the existing plan and
  runs drift analysis (see ``replan.py``).

Implementation detail: the ``pm project new`` flow delegates to the
same ``_plan_project_task`` helper that the ``plan`` subcommand uses,
so both exercises the same code path end-to-end.

Issue #274: ``pm project new`` auto-classifies the target directory as
``greenfield`` or ``existing`` and routes to the drift-aware replan
flow in the ``existing`` case so cold-start decompositions don't fight
against prior code. Use ``--force-cold-start`` to override.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Literal

import typer

from pollypm.cli_help import help_with_examples
from pollypm.config import (
    DEFAULT_CONFIG_PATH,
    load_config,
    resolve_config_path,
)
from pollypm.project_guides import (
    init_project_guide,
    list_project_guides,
    render_project_guide_diff,
)


project_app = typer.Typer(
    help=help_with_examples(
        "Planner-backed project lifecycle (new / plan / replan / guides).",
        [
            ("pm project new ~/dev/my-app", "register a project and offer to plan it"),
            ("pm project plan my_app", "queue a fresh planning task"),
            ("pm project init-guide architect --project my_app", "fork the architect guide into the project"),
        ],
    ),
    no_args_is_help=True,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _require_config(config_path: Path) -> Path:
    """Resolve + verify the pollypm config exists. Exit(1) otherwise."""
    path = resolve_config_path(config_path)
    if not path.exists():
        typer.echo(
            f"No PollyPM config at {path}. Run `pm init` or `pm onboard` first.",
            err=True,
        )
        raise typer.Exit(code=1)
    return path


def _resolve_project_key(
    config_path: Path,
    project: str | None,
) -> tuple[str, Path]:
    """Resolve a project key + path from a user-supplied identifier.

    Accepts an explicit project key, a normalized (hyphens-to-underscores)
    key, or a filesystem path. When ``project`` is ``None``, prefer the
    cwd if it matches a registered project. Exits cleanly with a helpful
    message when no match is found.
    """
    config = load_config(config_path)

    # No explicit project — try cwd.
    if project is None:
        cwd = Path.cwd().resolve()
        for key, known in config.projects.items():
            if Path(known.path).resolve() == cwd:
                return key, Path(known.path)
        typer.echo(
            "No project specified and the current directory is not a "
            "registered project. Provide the project key explicitly, or run "
            "from inside a project's root.",
            err=True,
        )
        raise typer.Exit(code=1)

    # Explicit key or alias.
    if project in config.projects:
        return project, Path(config.projects[project].path)

    normalized = project.replace("-", "_")
    if normalized in config.projects:
        return normalized, Path(config.projects[normalized].path)

    # Filesystem path fallback.
    as_path = Path(project).expanduser().resolve()
    for key, known in config.projects.items():
        if Path(known.path).resolve() == as_path:
            return key, Path(known.path)

    typer.echo(
        f"Unknown project '{project}'. Known: "
        + (", ".join(config.projects) or "(none)"),
        err=True,
    )
    raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# Project-state classification (issue #274)
# ---------------------------------------------------------------------------


# Source-file suffixes we treat as "this directory already has code".
# Conservative but covers the major languages users currently run
# PollyPM against. A ``README`` alone is not enough — docs without code
# or history usually land in the greenfield bucket.
_SOURCE_SUFFIXES: frozenset[str] = frozenset(
    {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".rb"}
)


def _git_commit_count(project_path: Path) -> int:
    """Return the number of commits reachable from HEAD, or 0 on error.

    Returns 0 for a non-git directory, a freshly-``git init``-ed
    directory with no commits, or any other error (network, permissions,
    git binary missing). Deliberately conservative — a read failure must
    never force the existing-project branch on an actually-fresh dir.
    """
    if not (project_path / ".git").exists():
        return 0
    try:
        result = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=str(project_path),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    if result.returncode != 0:
        return 0
    try:
        return int(result.stdout.strip() or "0")
    except ValueError:
        return 0


def _has_source_files(project_path: Path, *, max_scan: int = 2000) -> bool:
    """Return True iff ``project_path`` contains at least one source file.

    Walks the tree (skipping ``.git``, ``.pollypm``, and common venv /
    node_modules dirs) and bails on the first match. Capped at
    ``max_scan`` entries to keep the classifier fast on giant repos —
    the ``rglob`` itself is cheap, but an uncooperative filesystem can
    still stall us.
    """
    skip_dirs = {".git", ".pollypm", "node_modules",
                 "__pycache__", ".venv", "venv", "dist", "build", ".tox"}
    count = 0
    try:
        for entry in project_path.rglob("*"):
            # Cheap prune: any parent hit on the skip list → move on.
            if any(part in skip_dirs for part in entry.parts):
                continue
            if not entry.is_file():
                continue
            if entry.suffix in _SOURCE_SUFFIXES:
                return True
            count += 1
            if count >= max_scan:
                break
    except OSError:
        return False
    return False


def _planner_db_path(
    project_path: Path,
    *,
    config_path: Path | None = None,
    create_parent: bool = False,
) -> Path:
    """Return the planner/work-task DB for project-planning entry points.

    Post-#339, planner-created tasks belong in the workspace-scope DB.
    Fall back to the project-local path only when no config is available,
    which keeps older isolated tests and recovery paths working.
    """
    db_path = project_path / ".pollypm" / "state.db"
    try:
        cfg = load_config(config_path) if config_path is not None else load_config()
        workspace_root = getattr(cfg.project, "workspace_root", None)
        if workspace_root is not None:
            db_path = Path(workspace_root) / ".pollypm" / "state.db"
    except Exception:  # noqa: BLE001
        pass
    if create_parent:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    return db_path


def _has_work_tasks(
    project_path: Path,
    *,
    project_key: str | None = None,
    config_path: Path | None = None,
) -> bool:
    """Return True iff *this* project's DB has ≥1 work_tasks row.

    Reads the sqlite table directly — ``SQLiteWorkService`` is a heavier
    entrypoint and this path runs on the happy path for every
    ``pm project new`` invocation, so we keep it lean. Fails closed:
    any DB / IO error returns False so we don't accidentally flag a
    greenfield project as existing on a transient error.

    Post-#339 the workspace-scope DB holds tasks for *every* registered
    project — so counting "any row in the workspace DB" for an unkeyed
    call would leak state from sibling projects into the classification
    of a fresh directory. When ``project_key`` is None we therefore
    look only at the project-local DB (``<path>/.pollypm/state.db``);
    callers that know their key pass it through so the workspace DB
    filter is safe.
    """
    if project_key:
        db_path = _planner_db_path(project_path, config_path=config_path)
    else:
        db_path = project_path / ".pollypm" / "state.db"
    if not db_path.exists():
        return False
    try:
        import sqlite3

        from pollypm.storage.sqlite_pragmas import apply_workspace_pragmas

        with sqlite3.connect(str(db_path)) as conn:
            # #1018 — short busy_timeout so a planner probe doesn't
            # race the heartbeat writer holding the same workspace DB.
            apply_workspace_pragmas(conn)
            cur = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='work_tasks' LIMIT 1"
            )
            if cur.fetchone() is None:
                return False
            if project_key:
                cur = conn.execute(
                    "SELECT 1 FROM work_tasks WHERE project = ? LIMIT 1",
                    (project_key,),
                )
            else:
                cur = conn.execute("SELECT 1 FROM work_tasks LIMIT 1")
            return cur.fetchone() is not None
    except Exception:  # noqa: BLE001
        return False


def _classify_project_state(
    project_path: Path,
    project_key: str | None = None,
    config_path: Path | None = None,
) -> Literal["greenfield", "existing"]:
    """Classify a project directory for the ``pm project new`` routing.

    Returns ``"existing"`` if ANY of the following hold:

    * ``git rev-list --count HEAD`` > 1 (i.e. more than just an initial
      "init" commit).
    * ``<path>/docs/plan/plan.md`` exists.
    * Any source file (``.py``/``.ts``/``.js``/``.go``/``.rs``/
      ``.java``/``.rb``/``.tsx``/``.jsx``) exists at any depth.
    * The work-service DB already has ≥ 1 row in ``work_tasks``.

    Otherwise ``"greenfield"``. The heuristic is intentionally loose —
    the user can still override with ``--force-cold-start`` when the
    auto-classification guesses wrong.
    """
    if _git_commit_count(project_path) > 1:
        return "existing"
    if (project_path / "docs" / "plan" / "plan.md").exists():
        return "existing"
    if _has_source_files(project_path):
        return "existing"
    if _has_work_tasks(
        project_path,
        project_key=project_key,
        config_path=config_path,
    ):
        return "existing"
    return "greenfield"


def _plan_project_task(
    project_key: str,
    project_path: Path,
    *,
    config_path: Path | None = None,
    title_prefix: str = "Plan",
    description: str = "",
    actor: str = "architect",
) -> Any:
    """Create a ``flow=plan_project`` task on the project's work service.

    Mirrors the ``project.created`` observer (``plugin.py``): create the
    task and immediately auto-queue it so the architect's assignment
    sweep finds real work instead of leaving the task ``draft`` forever
    (issue #993 / savethenovel forensic). Best-effort: a failure in the
    queue gate keeps the draft so the user can fix it manually.

    Returns the created ``Task`` (post-queue when auto-queue succeeds).
    The ``work_status`` on the returned task tells the caller whether
    auto-queue fired (``queued``) or fell back to ``draft``. Caller owns
    output formatting.
    """
    # Local imports to keep the CLI importable in tests that haven't
    # wired a full SQLite environment yet.
    import logging

    from pollypm.work import create_work_service

    log = logging.getLogger(__name__)

    db_path = _planner_db_path(
        project_path,
        config_path=config_path,
        create_parent=True,
    )

    with create_work_service(db_path=db_path, project_path=project_path) as svc:
        task = svc.create(
            title=f"{title_prefix} {project_key}",
            description=description or (
                f"Run the architect + 5-critic planning pipeline on "
                f"{project_key}."
            ),
            type="task",
            project=project_key,
            flow_template="plan_project",
            roles={"architect": actor},
            priority="high",
        )
        # Parity with ``_on_project_created`` (plugin.py:249-258): the
        # architect spawn relies on the assignment sweep finding a
        # ``queued`` task. Leaving the explicitly-requested task in
        # ``draft`` recreates the "ready, sit idle forever" pattern
        # from #993 — a regression seen in the savethenovel forensic.
        # Best-effort: keep the draft on gate failure so the user can
        # recover via ``pm task queue`` rather than crashing the CLI.
        try:
            task = svc.queue(task.task_id, actor="planner")
        except Exception as queue_exc:  # noqa: BLE001
            log.warning(
                "project_planning: auto-queue of %s failed (%s); "
                "task left in draft. Run `pm task queue %s` manually.",
                task.task_id, queue_exc, task.task_id,
            )
    return task


# ---------------------------------------------------------------------------
# pm project blocker-summary  (#779)
# ---------------------------------------------------------------------------


@project_app.command("blocker-summary")
def blocker_summary_cmd(
    project: str = typer.Argument(
        ...,
        help="Project key the blocker summary applies to.",
    ),
    reason: str = typer.Option(
        ...,
        "--reason",
        help="Plain-language description of why the project is blocked.",
    ),
    owner: str = typer.Option(
        ...,
        "--owner",
        help="Who owns the next action: 'user' to create a follow-up "
             "task assigned to the user, otherwise an agent name "
             "(e.g. 'polly', 'archie').",
    ),
    action: list[str] = typer.Option(
        [],
        "--action",
        help="Concrete required action — repeat for multiple steps.",
    ),
    affected: list[str] = typer.Option(
        [],
        "--affected",
        help="Task ID this blocker affects (repeatable, e.g. proj/3).",
    ),
    unblock_when: str = typer.Option(
        "",
        "--unblock-when",
        help="The condition that clears the blocker. Defaults to empty.",
    ),
    actor: str = typer.Option(
        "polly",
        "--actor",
        help="Author of the summary. Defaults to polly (the PM).",
    ),
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path.",
    ),
) -> None:
    """Record a structured blocker summary for a project (#779).

    The PM authors this when a project enters a user-blocked state so
    the dashboard, inbox, and downstream consumers stop having to
    infer the blocker reason from free-form notify messages.

    Examples
    --------
    Record a user-blocked summary that creates an inbox task:

        pm project blocker-summary booktalk \\
            --reason "Plan needs your Phase A approval before workers can claim." \\
            --owner user \\
            --action "Read docs/project-plan.md" \\
            --action "Approve via 'pm task approve booktalk/3' or reject" \\
            --affected booktalk/3 \\
            --unblock-when "Plan task transitions out of user_review"

    Record a project-internal blocker (no user task created):

        pm project blocker-summary widgets \\
            --reason "Vendor API down — waiting on upstream." \\
            --owner polly \\
            --action "Polly retries the probe every 30 minutes" \\
            --unblock-when "Vendor reports the outage cleared"
    """
    from pollypm.project_status_summary import (
        ProjectBlockerSummary,
        record_project_blocker_summary,
    )

    cfg = load_config(config_path)
    project_cfg = cfg.projects.get(project)
    if project_cfg is None:
        typer.echo(
            f"Project {project!r} is not registered in the workspace.",
            err=True,
        )
        raise typer.Exit(code=1)
    project_path = Path(project_cfg.path)
    db_path = _planner_db_path(project_path, config_path=config_path)
    if not db_path.exists():
        typer.echo(
            f"Project {project!r} has no work-service DB at {db_path}. "
            "Initialize the project first with `pm project plan` or "
            "`pm task new`.",
            err=True,
        )
        raise typer.Exit(code=1)

    summary = ProjectBlockerSummary(
        project=project,
        reason=reason,
        owner=owner,
        required_actions=list(action),
        affected_tasks=list(affected),
        unblock_condition=unblock_when,
    )

    from pollypm.store import SQLAlchemyStore
    from pollypm.work import create_work_service

    store = SQLAlchemyStore(f"sqlite:///{db_path}")
    try:
        with create_work_service(
            db_path=db_path, project_path=project_path,
        ) as svc:
            result = record_project_blocker_summary(
                store=store,
                work_service=svc,
                summary=summary,
                actor=actor,
            )
    finally:
        try:
            store.close()
        except Exception:  # noqa: BLE001
            pass

    typer.echo(f"Recorded blocker summary for {project} (event {result['event_id']}).")
    if result.get("task_id"):
        typer.echo(f"Created user-facing unblock task: {result['task_id']}")


# ---------------------------------------------------------------------------
# pm project init-guide / list-guides
# ---------------------------------------------------------------------------


@project_app.command("init-guide")
def init_guide_cmd(
    role: str = typer.Argument(
        ...,
        help="Role guide to fork: architect, reviewer, or worker.",
    ),
    project: str | None = typer.Option(
        None,
        "--project",
        help=(
            "Project key, alias, or path. Defaults to the project whose "
            "root matches the current directory."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite an existing project-local guide.",
    ),
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path.",
    ),
) -> None:
    """Fork a built-in role guide into ``.pollypm/project-guides``."""
    path = _require_config(config_path)
    key, project_path = _resolve_project_key(path, project)
    try:
        guide = init_project_guide(project_path, role, force=force)
    except (FileExistsError, RuntimeError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"Wrote project-local {guide.role} guide for '{key}' at "
        f"{guide.path} (forked_from: {guide.forked_from})."
    )


@project_app.command("list-guides")
def list_guides_cmd(
    project: str | None = typer.Option(
        None,
        "--project",
        help=(
            "Project key, alias, or path. Defaults to the project whose "
            "root matches the current directory."
        ),
    ),
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path.",
    ),
) -> None:
    """List project-local role guides for a project."""
    path = _require_config(config_path)
    key, project_path = _resolve_project_key(path, project)
    guides = list_project_guides(project_path)
    if not guides:
        typer.echo(f"No project-local guides for '{key}'.")
        return
    typer.echo(f"Project-local guides for '{key}':")
    for guide in guides:
        forked = guide.forked_from or "unknown"
        typer.echo(f"- {guide.role}: {guide.path} (forked_from: {forked})")


@project_app.command("guide-diff")
def guide_diff_cmd(
    role: str = typer.Argument(
        ...,
        help="Role guide to diff: architect, reviewer, or worker.",
    ),
    project: str | None = typer.Option(
        None,
        "--project",
        help=(
            "Project key, alias, or path. Defaults to the project whose "
            "root matches the current directory."
        ),
    ),
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path.",
    ),
) -> None:
    """Print a unified diff between the local guide and current built-in."""
    path = _require_config(config_path)
    _key, project_path = _resolve_project_key(path, project)
    try:
        diff_text = render_project_guide_diff(project_path, role)
    except (FileExistsError, RuntimeError, ValueError, FileNotFoundError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    if not diff_text:
        typer.echo("No drift detected; the project-local guide matches the current built-in.")
        return
    typer.echo(diff_text)


# ---------------------------------------------------------------------------
# pm project rename (#766)
# ---------------------------------------------------------------------------


@project_app.command("rename")
def rename_cmd(
    old_slug: str = typer.Argument(
        ...,
        help="Current project slug (e.g. 'polly_remote').",
    ),
    new_slug: str = typer.Argument(
        ...,
        help="New slug (lowercase, underscores only — same shape as existing keys).",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Preview changes without mutating config.",
    ),
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path.",
    ),
) -> None:
    """Rename a project's canonical slug (#766).

    Updates the ``[projects.<old>]`` config block and every
    ``[sessions.*]`` entry whose ``project`` field matches. Tmux
    window names, worktree directories, and existing work-service
    task IDs are NOT auto-updated — those are live state. The
    command reports what needs manual cleanup.

    Re-run live sessions (``pm reset``, then relaunch workers) to
    pick up the new session names and window titles.
    """
    from pollypm.projects import rename_project
    path = _require_config(config_path)
    try:
        renamed, warnings = rename_project(
            path, old_slug, new_slug, dry_run=dry_run,
        )
    except (typer.BadParameter, Exception) as exc:  # noqa: BLE001
        if isinstance(exc, typer.BadParameter):
            typer.echo(f"Error: {exc.message}", err=True)
        else:
            typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    prefix = "Would rename" if dry_run else "Renamed"
    typer.echo(f"{prefix} {old_slug!r} → {new_slug!r}")
    typer.echo(f"  path:     {renamed.path}")
    typer.echo(f"  name:     {renamed.name}")
    if warnings:
        typer.echo("")
        typer.echo("Heads up — these were NOT auto-updated:")
        for warning in warnings:
            typer.echo(f"  - {warning}")
    if dry_run:
        typer.echo("")
        typer.echo("Re-run without --dry-run to apply.")


# ---------------------------------------------------------------------------
# pm project plan
# ---------------------------------------------------------------------------


@project_app.command("plan")
def plan_cmd(
    project: str | None = typer.Argument(
        None,
        help=(
            "Project key, alias, or path. Defaults to the project whose "
            "root matches the current directory."
        ),
    ),
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path.",
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Emit machine-readable output.",
    ),
) -> None:
    """Run the architecture planner on ``project``.

    Creates a task with ``flow=plan_project`` on the project's work
    service. The flow engine drives the architect through research,
    decomposition, critic panel, and synthesis (§3 of the planner spec).
    """
    path = _require_config(config_path)
    key, project_path = _resolve_project_key(path, project)
    task = _plan_project_task(
        key,
        project_path,
        config_path=path,
        title_prefix="Plan project",
    )

    if as_json:
        typer.echo(json.dumps({
            "project": key,
            "task_id": task.task_id,
            "flow": task.flow_template_id,
            "work_status": task.work_status.value,
        }, indent=2))
        return
    typer.echo(
        f"Created planning task {task.task_id} on project '{key}' "
        f"(flow={task.flow_template_id})."
    )
    if task.work_status.value == "queued":
        typer.echo("Auto-queued for the architect.")
    else:
        typer.echo(
            "Auto-queue failed — run `pm task queue " + task.task_id
            + "` to hand it off to the architect worker."
        )


# ---------------------------------------------------------------------------
# pm project replan
# ---------------------------------------------------------------------------


@project_app.command("replan")
def replan_cmd(
    project: str | None = typer.Argument(
        None,
        help=(
            "Project key, alias, or path. Defaults to the project whose "
            "root matches the current directory."
        ),
    ),
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path.",
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Emit machine-readable output.",
    ),
) -> None:
    """Re-run the planner on ``project`` with drift analysis.

    Uses the same ``plan_project`` flow; the architect's stage-0 research
    loop reads the existing ``docs/project-plan.md`` + Risk Ledger +
    ``docs/planning-session-log.md`` and produces a drift analysis
    (``replan.py``) before re-opening decomposition.
    """
    path = _require_config(config_path)
    key, project_path = _resolve_project_key(path, project)
    task = _plan_project_task(
        key,
        project_path,
        config_path=path,
        title_prefix="Replan project",
        description=(
            f"Re-run the architecture planner on {key}. Stage-0 research "
            "should read the existing plan and produce a drift analysis "
            "before proposing changes."
        ),
    )

    if as_json:
        typer.echo(json.dumps({
            "project": key,
            "task_id": task.task_id,
            "flow": task.flow_template_id,
            "mode": "replan",
            "work_status": task.work_status.value,
        }, indent=2))
        return
    typer.echo(
        f"Created replan task {task.task_id} on project '{key}' "
        f"(flow={task.flow_template_id}, mode=replan)."
    )
    if task.work_status.value == "queued":
        typer.echo("Auto-queued for the architect.")
    else:
        typer.echo(
            "Auto-queue failed — run `pm task queue " + task.task_id
            + "` to hand it off to the architect worker."
        )


# ---------------------------------------------------------------------------
# pm project new
# ---------------------------------------------------------------------------


def _prompt_run_planner(*, default_yes: bool = True) -> bool:
    """Prompt 'Run the planner now? (Y/n)'. Default yes.

    Isolated so tests can monkey-patch ``typer.confirm``.
    """
    return typer.confirm("Run the planner now?", default=default_yes)


@project_app.command("new")
def new_cmd(
    repo_path: Path = typer.Argument(
        ..., help="Path to the project folder (must be a git repo).",
    ),
    name: str | None = typer.Option(
        None, "--name", help="Optional display name.",
    ),
    skip_planner: bool = typer.Option(
        False, "--skip-planner",
        help="Register the project without prompting for the planner.",
    ),
    skip_plan: bool = typer.Option(
        False, "--skip-plan",
        help=(
            "Suppress the project_planning auto-fire for this project "
            "(issue #255). Equivalent to setting "
            "`[planner] auto_on_project_created = false` globally, but "
            "scoped to this invocation. Independent of the interactive "
            "`--skip-planner` prompt toggle."
        ),
    ),
    force_cold_start: bool = typer.Option(
        False, "--force-cold-start",
        help=(
            "Override the existing-project auto-detection (issue #274) "
            "and run the cold-start planner even when the target "
            "directory already has commits, a prior plan, source files, "
            "or work-service rows."
        ),
    ),
    slug: str | None = typer.Option(
        None, "--slug",
        help=(
            "Explicit project slug (key). When omitted, the slug is "
            "derived from the repo directory name. Pass this to avoid "
            "getting stuck with a bad auto-slug (#766). Must be lowercase "
            "with underscores only."
        ),
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Non-interactive: auto-accept the planner prompt.",
    ),
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path.",
    ),
) -> None:
    """Register a project and optionally kick off the planner.

    After registration, prompts "Run the planner now? (Y/n)" (default
    yes). ``--skip-planner`` registers the project without prompting;
    ``--yes`` accepts the prompt non-interactively. ``--skip-plan``
    suppresses the project_planning plugin's auto-fire on the emitted
    ``project.created`` event (#255). ``--slug`` pins an explicit
    project slug instead of auto-deriving from the path (#766).

    Issue #274: when the target directory already looks like an
    existing project (commits, ``docs/plan/plan.md``, source files, or
    prior work_tasks rows) the auto-fired planner task uses the
    drift-aware replan description instead of cold-start decomposition.
    Pass ``--force-cold-start`` to override.
    """
    from pollypm.projects import register_project, normalize_project_path

    path = _require_config(config_path)

    # Determine whether the path is already registered before we call
    # ``register_project`` — the function silently returns the existing
    # entry on a re-register, and we must not re-fire ``project.created``
    # for a project that already exists (issue #255 acceptance: "second
    # add-project with same name doesn't double-fire").
    was_preexisting = False
    try:
        from pollypm.config import load_config as _load_config
        normalized_new = normalize_project_path(repo_path)
        existing = _load_config(path)
        for known in existing.projects.values():
            if normalize_project_path(Path(known.path)) == normalized_new:
                was_preexisting = True
                break
    except Exception:  # noqa: BLE001
        was_preexisting = False

    project = register_project(path, repo_path, name=name, slug=slug)
    typer.echo(
        f"Registered project {project.name or project.key} at {project.path}"
    )
    if slug is not None:
        typer.echo(f"Using explicit slug: {project.key}")

    # Issue #274: classify the project directory so we can choose
    # between cold-start and drift-aware replan. Only meaningful when a
    # planner task is actually going to fire — suppress the classifier
    # echo when the user explicitly opted out.
    suppress_all_planning = bool(skip_plan or skip_planner)
    if suppress_all_planning:
        mode: Literal["greenfield", "existing"] = "greenfield"
    elif force_cold_start:
        mode = "greenfield"
        typer.echo("Fresh project — running cold-start planner.")
    else:
        mode = _classify_project_state(
            project.path,
            project.key,
            path,
        )
        if mode == "existing":
            typer.echo(
                "Detected existing project — running drift-aware replan."
            )
        else:
            typer.echo("Fresh project — running cold-start planner.")

    # Fire the ``project.created`` observer chain so plugins (including
    # project_planning itself) can react. Best-effort — never block the
    # CLI on observer failures. Only fire on genuinely-new registrations
    # so a re-run of ``pm project new`` on the same path is idempotent.
    auto_fired = False
    if not was_preexisting:
        try:
            from pollypm.plugin_host import extension_host_for_root

            host = extension_host_for_root(str(project.path))
            host.run_observers(
                "project.created",
                {
                    "project_key": project.key,
                    "path": str(project.path),
                    # ``skip_plan`` travels through the event payload so
                    # the observer can suppress auto-fire without the
                    # CLI having to reach into plugin internals.
                    # ``--skip-planner`` (legacy flag that suppresses the
                    # interactive prompt + the explicit ``_plan_project_task``
                    # call below) also suppresses auto-fire so the combined
                    # UX stays "no planner task".
                    "skip_plan": suppress_all_planning,
                    # Issue #274: mode hint lets the observer pick
                    # cold-start vs. drift-aware replan phrasing when
                    # auto-firing the plan task. Defaults to
                    # ``greenfield`` so observers from other plugins
                    # that don't know about this payload key behave
                    # identically to the pre-#274 world.
                    "mode": mode,
                },
                metadata={
                    "source": "pm project new",
                    # Forward the effective config path so the observer
                    # can honour `[planner] auto_on_project_created`
                    # from a non-default config (notably in tests).
                    "config_path": str(path),
                },
            )
            # Detect whether the observer actually created a plan_project
            # task on the project's work service. If it did, the
            # interactive prompt below becomes redundant — auto-fire
            # already satisfied the user's "plan this project" intent.
            auto_fired = _plan_task_exists(
                project.path,
                project.key,
                config_path=path,
            )
        except Exception as exc:  # noqa: BLE001
            # Auto-fire is best-effort, but a silent swallow meant users
            # couldn't tell when the planner failed to boot. Emit a
            # stderr breadcrumb so the interactive prompt below is still
            # reached and the user at least knows why it wasn't skipped.
            typer.echo(
                f"Warning: project.created hook failed ({exc}). The project "
                f"is registered, but the planner auto-fire did not run. Run "
                f"`pm project plan {project.key}` to start planning manually.",
                err=True,
            )

    if auto_fired:
        mode_label = "replan" if mode == "existing" else "plan_project"
        typer.echo(
            f"Auto-created {mode_label} task for '{project.key}' via "
            "project.created hook (see `[planner] auto_on_project_created`)."
        )
        _auto_spawn_architect(path, project.key)
        return

    # ``--skip-plan`` or ``--skip-planner`` both short-circuit the prompt +
    # legacy task-creation path. This keeps the CLI idempotent with the
    # event payload we sent to observers above.
    if suppress_all_planning:
        typer.echo(
            "Skipped planner. Run `pm project plan "
            + project.key + "` later to start planning."
        )
        return

    if yes:
        run_it = True
    else:
        run_it = _prompt_run_planner()

    if not run_it:
        typer.echo(
            "Planner skipped. Run `pm project plan "
            + project.key + "` whenever you're ready."
        )
        return

    # Issue #274: even on the legacy (auto-fire-disabled) path, route
    # to the replan description when the directory looks existing.
    if mode == "existing":
        task = _plan_project_task(
            project.key, project.path,
            config_path=path,
            title_prefix="Replan project",
            description=(
                f"Re-run the architecture planner on {project.key}. Stage-0 "
                "research should read the existing plan and produce a "
                "drift analysis before proposing changes."
            ),
        )
    else:
        task = _plan_project_task(
            project.key,
            project.path,
            config_path=path,
            title_prefix="Plan project",
        )
    typer.echo(
        f"Created planning task {task.task_id} on project "
        f"'{project.key}' (flow={task.flow_template_id})."
    )
    if task.work_status.value == "queued":
        typer.echo("Auto-queued for the architect.")
    else:
        typer.echo(
            "Auto-queue failed — run `pm task queue " + task.task_id
            + "` to hand it off to the architect worker."
        )
    _auto_spawn_architect(path, project.key)


def _auto_spawn_architect(config_path: Path, project_key: str) -> None:
    """Best-effort: spawn a project-scoped architect session for ``project_key``.

    The planner's ``plan_project`` flow parks every stage on
    ``actor_role: architect``. Without a live session named
    ``architect_<project>`` the task-assignment sweeper (see
    ``pollypm.work.task_assignment``) cannot resolve a recipient and the
    pipeline stalls silently at the ``research`` node.

    Called from ``pm project new`` after a plan_project task has been
    created (either via the ``project.created`` observer or the legacy
    interactive prompt). Honours the ``--skip-planner`` / ``--skip-plan``
    flags at the callsite — this helper assumes a planner task was
    actually created and the user wants the flow to run.

    Failures here are swallowed: the task is already parked in queued
    state, the user can always spawn the architect manually with
    ``pm worker-start --role architect --profile architect <project>``.
    """
    import logging

    log = logging.getLogger(__name__)
    try:
        # Local import — keeps the planner CLI importable in environments
        # that haven't wired the full session/supervisor stack (tests
        # running against ``pm project new`` with a minimal config).
        from pollypm.config import load_config
        from pollypm.workers import create_worker_session, launch_worker_session

        config = load_config(config_path)
        # Don't double-spawn if the caller (or an earlier invocation)
        # already registered an architect session for this project.
        for existing in config.sessions.values():
            if (
                existing.role == "architect"
                and existing.project == project_key
                and existing.enabled
            ):
                log.info(
                    "project_planning: architect session %s already "
                    "registered for '%s' — skipping auto-spawn.",
                    existing.name, project_key,
                )
                return

        session = create_worker_session(
            config_path,
            project_key=project_key,
            prompt=None,
            role="architect",
            agent_profile="architect",
        )
        typer.echo(
            f"Spawned architect session {session.name} for "
            f"project '{project_key}'."
        )
        try:
            launch_worker_session(config_path, session.name)
        except Exception as exc:  # noqa: BLE001
            log.info(
                "project_planning: architect session %s registered but "
                "not launched (%s). Start it manually with "
                "`pm worker-start --role architect --profile architect %s`.",
                session.name, exc, project_key,
            )
    except Exception as exc:  # noqa: BLE001
        # Most common failure is no accounts configured yet (fresh
        # install running ``pm project new`` before ``pm onboard``).
        log.info(
            "project_planning: architect auto-spawn skipped (%s). "
            "Start it manually with "
            "`pm worker-start --role architect --profile architect %s`.",
            exc, project_key,
        )


def _plan_task_exists(
    project_path: Path,
    project_key: str,
    *,
    config_path: Path | None = None,
) -> bool:
    """Return True if a ``plan_project`` task already exists on the
    project's work service.

    Used by ``new_cmd`` after emitting ``project.created`` to detect
    whether the observer auto-fired a planning task — in which case the
    interactive prompt becomes redundant. Safe to call on a project
    that never opened its work DB (returns False without creating one).
    """
    db_path = _planner_db_path(project_path, config_path=config_path)
    if not db_path.exists():
        return False
    try:
        from pollypm.work import create_work_service

        with create_work_service(
            db_path=db_path, project_path=project_path,
        ) as svc:
            for task in svc.list_tasks(project=project_key):
                if task.flow_template_id == "plan_project":
                    return True
    except Exception:  # noqa: BLE001
        return False
    return False
