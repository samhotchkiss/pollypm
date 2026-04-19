"""Issue, report, and deploy CLI groups.

Contract:
- Inputs: Typer arguments/options for issue-tracker and itsalive flows.
- Outputs: three Typer apps exported as ``issue_app``, ``report_app``,
  and ``itsalive_app``.
- Side effects: task-backend mutations and itsalive deploy requests via
  ``PollyPMService``.
- Invariants: issue/deploy command behavior stays out of ``pollypm.cli``.
"""

from __future__ import annotations

from pathlib import Path

import typer

from pollypm.config import DEFAULT_CONFIG_PATH
from pollypm.service_api import PollyPMService


issue_app = typer.Typer(
    help=(
        "Manage project issues through the configured backend.\n\n"
        "Examples:\n\n"
        "• pm issue list                      — list issues for the active project\n"
        "• pm issue show <id>                 — print a single issue\n"
    )
)

report_app = typer.Typer(
    help=(
        "Report project status summaries.\n\n"
        "Examples:\n\n"
        "• pm report status                   — summarize all projects\n"
        "• pm report status --project <name>  — summarize one project\n"
    )
)

itsalive_app = typer.Typer(
    help=(
        "Manage itsalive deployments.\n\n"
        "Examples:\n\n"
        "• pm itsalive status                 — show deployment state\n"
        "• pm itsalive deploy                 — deploy the project\n"
    )
)


@issue_app.command("list")
def issue_list(
    project: str = typer.Option(..., "--project", help="Project key."),
    state: list[str] | None = typer.Option(None, "--state", help="Optional tracker state filter."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    service = PollyPMService(config_path)
    tasks = service.list_tasks(project, states=state)
    if not tasks:
        typer.echo("No issues found.")
        return
    for task in tasks:
        typer.echo(f"{task.task_id} [{task.state}] {task.title}")


@issue_app.command("info")
def issue_info(
    task_id: str = typer.Argument(..., help="Issue id."),
    project: str = typer.Option(..., "--project", help="Project key."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    service = PollyPMService(config_path)
    task = service.get_task(project, task_id)
    typer.echo(f"{task.task_id} [{task.state}] {task.title}")


@issue_app.command("next")
def issue_next(
    project: str = typer.Option(..., "--project", help="Project key."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    service = PollyPMService(config_path)
    task = service.next_available_task(project)
    if task is None:
        typer.echo("No ready issue found.")
        return
    typer.echo(f"{task.task_id} [{task.state}] {task.title}")


@issue_app.command("history")
def issue_history(
    task_id: str = typer.Argument(..., help="Issue id."),
    project: str = typer.Option(..., "--project", help="Project key."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    service = PollyPMService(config_path)
    entries = service.task_history(project, task_id)
    if not entries:
        typer.echo("No history found.")
        return
    for entry in entries:
        typer.echo(entry)


@issue_app.command("create")
def issue_create(
    project: str = typer.Option(..., "--project", help="Project key."),
    title: str = typer.Option(..., "--title", help="Issue title."),
    body: str = typer.Option("", "--body", help="Issue body."),
    state: str = typer.Option("01-ready", "--state", help="Initial tracker state."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    service = PollyPMService(config_path)
    task = service.create_task(project, title=title, body=body, state=state)
    typer.echo(f"Created issue {task.task_id} [{task.state}] {task.title}")


@issue_app.command("transition")
def issue_transition(
    task_id: str = typer.Argument(..., help="Issue id."),
    to_state: str = typer.Argument(..., help="Destination tracker state."),
    project: str = typer.Option(..., "--project", help="Project key."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    service = PollyPMService(config_path)
    try:
        task = service.move_task(project, task_id, to_state=to_state)
    except ValueError as exc:
        typer.echo(f"Cannot transition {task_id}: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Moved issue {task.task_id} to {task.state}")


@issue_app.command("comment")
def issue_comment(
    task_name: str = typer.Argument(..., help="Issue id or note target."),
    text: str = typer.Option(..., "--text", help="Comment text."),
    project: str = typer.Option(..., "--project", help="Project key."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    service = PollyPMService(config_path)
    path = service.append_task_note(project, task_name, text=text)
    typer.echo(f"Updated {path}")


@issue_app.command("handoff")
def issue_handoff(
    task_name: str = typer.Argument(..., help="Issue id or note target."),
    what_done: str = typer.Option(..., "--done", help="Summary of what was completed."),
    how_to_test: str = typer.Option(..., "--test", help="How to verify the work."),
    branch_or_pr: str = typer.Option("", "--branch-or-pr", help="Branch name or PR link for review."),
    deviations: str = typer.Option("", "--deviations", help="Any spec deviations and why."),
    project: str = typer.Option(..., "--project", help="Project key."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    service = PollyPMService(config_path)
    path = service.append_task_handoff(
        project,
        task_name,
        what_done=what_done,
        how_to_test=how_to_test,
        branch_or_pr=branch_or_pr,
        deviations=deviations,
    )
    typer.echo(f"Updated {path}")


@issue_app.command("approve")
def issue_approve(
    task_id: str = typer.Argument(..., help="Issue id."),
    summary: str = typer.Option(..., "--summary", help="Review summary."),
    verification: str = typer.Option(..., "--verification", help="Independent verification performed."),
    project: str = typer.Option(..., "--project", help="Project key."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    service = PollyPMService(config_path)
    try:
        task = service.review_task(
            project,
            task_id,
            approved=True,
            summary=summary,
            verification=verification,
        )
    except ValueError as exc:
        typer.echo(f"Cannot approve {task_id}: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Approved issue {task.task_id} to {task.state}")


@issue_app.command("request-changes")
def issue_request_changes(
    task_id: str = typer.Argument(..., help="Issue id."),
    summary: str = typer.Option(..., "--summary", help="Review summary."),
    verification: str = typer.Option(..., "--verification", help="Independent verification performed."),
    changes: str = typer.Option(..., "--changes", help="Specific requested changes."),
    project: str = typer.Option(..., "--project", help="Project key."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    service = PollyPMService(config_path)
    try:
        task = service.review_task(
            project,
            task_id,
            approved=False,
            summary=summary,
            verification=verification,
            changes_requested=changes,
        )
    except ValueError as exc:
        typer.echo(f"Cannot request changes on {task_id}: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Returned issue {task.task_id} to {task.state}")


@issue_app.command("counts")
def issue_counts(
    project: str = typer.Option(..., "--project", help="Project key."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    service = PollyPMService(config_path)
    counts = service.task_state_counts(project)
    for state, count in counts.items():
        typer.echo(f"{state}: {count}")


@issue_app.command("report")
def issue_report(
    project: str = typer.Option(..., "--project", help="Project key."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    issue_counts(project=project, config_path=config_path)


@report_app.command("status")
def report_status(
    project: str = typer.Option(..., "--project", help="Project key."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    issue_counts(project=project, config_path=config_path)


@itsalive_app.command("deploy")
def itsalive_deploy(
    project: str = typer.Option(..., "--project", help="Project key."),
    subdomain: str | None = typer.Option(None, "--subdomain", help="itsalive subdomain for first deploy."),
    email: str | None = typer.Option(None, "--email", help="Email for first deploy if not already verified."),
    publish_dir: str = typer.Option(".", "--dir", help="Directory to deploy relative to the project root."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    service = PollyPMService(config_path)
    outcome = service.itsalive_deploy(
        project_key=project,
        subdomain=subdomain,
        email=email,
        publish_dir=publish_dir,
    )
    typer.echo(f"status={outcome.status}")
    typer.echo(f"message={outcome.message}")
    typer.echo(f"subdomain={outcome.subdomain}")
    if outcome.url:
        typer.echo(f"url={outcome.url}")
    if outcome.pending_path:
        typer.echo(f"pending={outcome.pending_path}")
    if outcome.expires_at:
        typer.echo(f"expires_at={outcome.expires_at}")


@itsalive_app.command("status")
def itsalive_status(
    project: str = typer.Option(..., "--project", help="Project key."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    service = PollyPMService(config_path)
    items = service.itsalive_pending(project_key=project)
    if not items:
        typer.echo("No pending itsalive deployments.")
        return
    for item in items:
        typer.echo(
            f"{item.subdomain} deploy_id={item.deploy_id} "
            f"expires_at={item.expires_at} email={item.email}"
        )


@itsalive_app.command("sweep")
def itsalive_sweep(
    project: str = typer.Option(..., "--project", help="Project key."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    service = PollyPMService(config_path)
    outcomes = service.itsalive_sweep(project_key=project)
    if not outcomes:
        typer.echo("No itsalive deployment updates.")
        return
    for outcome in outcomes:
        typer.echo(f"{outcome.subdomain}: {outcome.status} {outcome.message}")
        if outcome.url:
            typer.echo(f"  {outcome.url}")


@issue_app.command("validate")
def issue_validate(
    project: str = typer.Option(..., "--project", help="Project key."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    service = PollyPMService(config_path)
    result = service.validate_task_backend(project)
    if getattr(result, "passed", False):
        typer.echo("Task backend validation passed.")
    else:
        typer.echo("Task backend validation failed.")
    for check in getattr(result, "checks", []):
        typer.echo(f"check: {check}")
    for error in getattr(result, "errors", []):
        typer.echo(f"error: {error}")
    if not getattr(result, "passed", False):
        raise typer.Exit(code=1)

