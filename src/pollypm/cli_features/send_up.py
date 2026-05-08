"""``pm send-up`` — workers post mid-task milestones to the operator inbox.

A worker invokes ``pm send-up "outline drafted, starting render"`` from
its task pane (or from a script the worker spawns). One inbox card with
that message lands in the operator's view.

Boundary discipline: this module is a CLI shim. The emit logic lives in
:mod:`pollypm.worker_milestone` so non-CLI callers can reuse it without
making the CLI module a runtime dependency target.
"""

from __future__ import annotations

import functools
import inspect
import os
from pathlib import Path

import typer

from pollypm.worker_milestone import emit_worker_milestone


def _load_work_service(db: str, task_id: str):
    """Construct a SQLiteWorkService scoped to ``task_id``'s project.

    Reuses :func:`pollypm.work.cli._svc` because that helper already
    centralizes db-path resolution, project-root discovery, and the
    sync-manager wiring. CLI-to-CLI imports are allowed by the
    boundary rules; non-CLI code must continue to use the typed work
    service or the service_api facade directly.
    """
    from pollypm.work.cli import _project_from_task_id, _svc

    project = _project_from_task_id(task_id)
    return _svc(db, project=project)


def _bind_send_up_command(callback, helpers):
    """Bind ``helpers`` as the first arg of the callback while preserving
    Typer's introspectable signature.

    Mirrors the ``_bind_session_command`` helper from the session_runtime
    feature module after #1502 (functools.partial on the registered
    callable trips ``typing.get_type_hints``).
    """
    sig = inspect.signature(callback)
    parameters = list(sig.parameters.values())
    if not parameters:
        bound_param_name = None
        new_sig = sig
    else:
        bound_param_name = parameters[0].name
        new_sig = sig.replace(parameters=parameters[1:])

    @functools.wraps(callback)
    def wrapper(*args, **kwargs):
        return callback(helpers, *args, **kwargs)

    wrapper.__signature__ = new_sig
    if bound_param_name is not None:
        wrapper.__annotations__ = {
            name: annotation
            for name, annotation in callback.__annotations__.items()
            if name != bound_param_name
        }
    return wrapper


def send_up(
    helpers,
    message: str = typer.Argument(
        ...,
        help=(
            "One-line milestone to surface to the operator inbox "
            "(e.g. 'outline drafted, starting render')."
        ),
    ),
    task: str | None = typer.Option(
        None,
        "--task",
        help=(
            "Task id (project/number). Falls back to the "
            "POLLYPM_TASK_ID env var when omitted."
        ),
    ),
    actor: str = typer.Option(
        "worker",
        "--actor",
        help="Sender label recorded on the inbox card (defaults to 'worker').",
    ),
    db: str = typer.Option(
        ".pollypm/state.db",
        "--db",
        help="Path to SQLite database.",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit structured JSON."
    ),
) -> None:
    """Post a milestone update from a worker to the operator inbox.

    Designed for mid-task progress reports. The card is informational —
    it does NOT block, alert, or wake polly's heartbeat-supervisor.
    Polly sees it the next time she scans her inbox.
    """
    task_id = task or os.environ.get("POLLYPM_TASK_ID") or ""
    task_id = task_id.strip()
    if not task_id:
        typer.echo(
            "send-up: no task id — pass --task <project/N> or set "
            "POLLYPM_TASK_ID before invoking.",
            err=True,
        )
        raise typer.Exit(code=1)

    text = (message or "").strip()
    if not text:
        typer.echo("send-up: message is empty.", err=True)
        raise typer.Exit(code=1)

    # Load the work service. Mirrors the construction in
    # ``pollypm.work.cli._svc`` but kept self-contained so this CLI
    # module doesn't depend on another CLI module (boundary rule:
    # CLI modules are not runtime dependency targets for non-CLI
    # code).
    try:
        svc = _load_work_service(db, task_id)
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"send-up: could not open work service: {exc}", err=True)
        raise typer.Exit(code=1)

    new_card_id = emit_worker_milestone(svc, task_id, text, actor=actor)

    if json_output:
        import json as _json

        typer.echo(
            _json.dumps(
                {
                    "task_id": task_id,
                    "message": text,
                    "card_id": new_card_id,
                    "deduped": new_card_id is None,
                }
            )
        )
        return

    if new_card_id is None:
        typer.echo(
            "send-up: nothing emitted (recent duplicate, missing task, "
            "or store transient — see debug log)."
        )
        return
    typer.echo(f"send-up: emitted {new_card_id} for {task_id}.")


def register_send_up_commands(app: typer.Typer, *, helpers) -> None:
    """Register ``pm send-up`` on the root Typer app."""
    app.command(
        "send-up",
        help=(
            "Workers post mid-task milestone updates to the operator "
            "inbox. Use --task or POLLYPM_TASK_ID."
        ),
    )(_bind_send_up_command(send_up, helpers))
