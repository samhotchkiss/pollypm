"""CLI commands for the work-service-backed inbox view.

Exposes ``pm inbox`` and ``pm inbox show <task_id>``. Issue #341 migrated
the list reader onto the unified :class:`~pollypm.store.Store` messages
table — ``pm notify`` (the canonical escalation channel) writes rows
there via :meth:`Store.enqueue_message`, so the inbox must read from the
same surface or notify items would never appear. Work-service tasks with
``requester=user`` still participate (the cockpit flow emits them) and
are UNIONed in via the legacy bridge until #349 drains those writers.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import typer

from pollypm.cli_help import help_with_examples
from pollypm.work.cli import (
    _DB_OPTION,
    _JSON_OPTION,
    _PROJECT_OPTION,
    _project_from_task_id,
    _resolve_db_path,
    _svc,
    _task_to_dict,
    task_get,
)
from pollypm.work.inbox_view import inbox_tasks


inbox_app = typer.Typer(
    help=help_with_examples(
        "Work assigned to the user.",
        [
            ("pm inbox", "list open inbox items"),
            ("pm inbox show demo/1", "print one inbox task or message"),
            ("pm inbox --json", "emit the merged inbox view as JSON"),
        ],
    )
)


# ---------------------------------------------------------------------------
# Message-row rendering — ``pm notify`` rows land in the unified messages
# table (#340), so the inbox reader must surface them alongside the
# legacy work-service tasks the cockpit flow still emits.
# ---------------------------------------------------------------------------


def _message_row_to_display(row: dict[str, Any]) -> dict[str, Any]:
    """Project a :meth:`Store.query_messages` row into CLI display shape.

    The id string uses an ``msg:<id>`` prefix so it never collides with a
    ``project/number`` work-task id the same listing might include.
    """
    payload = row.get("payload") or {}
    scope = row.get("scope") or ""
    sender = row.get("sender") or ""
    project = payload.get("project") or scope or "inbox"
    # Priority inferred from tier — immediate lands open and is actionable.
    tier = row.get("tier") or "immediate"
    priority = "high" if tier == "immediate" and row.get("type") == "alert" else "normal"
    return {
        "id": f"msg:{row.get('id')}",
        "title": row.get("subject") or "(no subject)",
        "type": row.get("type") or "notify",
        "tier": tier,
        "priority": priority,
        "state": row.get("state") or "open",
        "sender": sender,
        "project": project,
        "created_at": str(row.get("created_at") or ""),
    }


@inbox_app.callback(invoke_without_command=True)
def inbox_root(
    ctx: typer.Context,
    project: Optional[str] = _PROJECT_OPTION,
    db: str = _DB_OPTION,
    output_json: bool = _JSON_OPTION,
) -> None:
    """Show messages + tasks waiting on the user.

    Post-#342 the inbox is the UNION of:

    * ``Store.query_messages(recipient='user', state='open',
      type=['notify', 'inbox_task', 'alert'])`` — every ``pm notify``
      row + everything the supervisor/heartbeat writers emit via the
      unified Store.
    * ``inbox_tasks(svc)`` — chat-flow tasks whose ``roles`` say ``user``
      is the requester. Plan-review + agent escalation flows still emit
      these; the merge keeps them visible alongside messages.

    The message rows dominate day-to-day usage (every ``pm notify`` lands
    there); the task rows are kept so the plan-review flow isn't
    invisible.
    """
    if ctx.invoked_subcommand is not None:
        return

    # --- Messages path (unified Store, #340 writers) -------------------
    db_path = _resolve_db_path(db, project=project)
    message_rows: list[dict[str, Any]] = []
    try:
        from pollypm.store import SQLAlchemyStore
        store = SQLAlchemyStore(f"sqlite:///{db_path}")
        try:
            filters: dict[str, Any] = dict(
                recipient="user",
                state="open",
                type=["notify", "inbox_task", "alert"],
            )
            if project:
                filters["scope"] = project
            message_rows = store.query_messages(**filters)
        finally:
            store.close()
    except Exception as exc:  # noqa: BLE001
        typer.echo(
            f"Warning: inbox messages query failed ({exc}); "
            f"falling back to work-service tasks only.",
            err=True,
        )

    display_messages = [_message_row_to_display(r) for r in message_rows]

    # --- Tasks path (work-service, chat flow) --------------------------
    svc = _svc(db, project=project)
    tasks = inbox_tasks(svc, project=project)

    if output_json:
        typer.echo(
            json.dumps(
                {
                    "assigned_count": len(tasks) + len(display_messages),
                    "messages": display_messages,
                    "tasks": [_task_to_dict(t) for t in tasks],
                },
                indent=2,
                default=str,
            )
        )
        return

    total = len(tasks) + len(display_messages)
    typer.echo(f"Inbox: {total} items")
    if total == 0:
        typer.echo("No messages waiting for you.")
        return

    typer.echo(f"{'ID':<20} {'Type':<10} {'Priority':<10} {'Title'}")
    typer.echo("-" * 70)
    for m in display_messages:
        title = m["title"]
        if len(title) > 38:
            title = title[:37] + "\u2026"
        typer.echo(
            f"{m['id']:<20} {m['type']:<10} "
            f"{m['priority']:<10} {title}"
        )
    for t in tasks:
        title = t.title or ""
        if len(title) > 38:
            title = title[:37] + "\u2026"
        typer.echo(
            f"{t.task_id:<20} {t.work_status.value:<10} "
            f"{t.priority.value:<10} {title}"
        )


@inbox_app.command("show")
def inbox_show(
    task_id: str = typer.Argument(..., help="Task ID (project/number)"),
    db: str = _DB_OPTION,
    output_json: bool = _JSON_OPTION,
) -> None:
    """Show full details of an inbox task. Alias for ``pm task get``."""
    # Delegate to the existing task get implementation so behaviour stays
    # identical (context loading, JSON shape, ...).
    task_get(task_id=task_id, db=db, output_json=output_json)


# ---------------------------------------------------------------------------
# Pass-through actions
#
# These commands exist so headless tests (and emergency operator scripts)
# can exercise the same work-service methods the cockpit TUI calls. The
# primary UX is always the Textual inbox screen — Sam shouldn't need the
# CLI day-to-day. Keep them small and focused.
# ---------------------------------------------------------------------------


@inbox_app.command("reply")
def inbox_reply(
    task_id: str = typer.Argument(..., help="Task ID (project/number)"),
    body: str = typer.Argument(..., help="Reply text. Pass '-' to read from stdin."),
    actor: str = typer.Option("user", "--actor", help="Actor to attribute the reply to."),
    db: str = _DB_OPTION,
) -> None:
    """Post a reply on an inbox task (mirrors the cockpit reply action)."""
    import sys

    if body == "-":
        body = sys.stdin.read()
    project = _project_from_task_id(task_id)
    svc = _svc(db, project=project)
    try:
        entry = svc.add_reply(task_id, body, actor=actor)
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"{task_id} reply @ {entry.timestamp.isoformat()}")


@inbox_app.command("archive")
def inbox_archive(
    task_id: str = typer.Argument(..., help="Task ID (project/number)"),
    actor: str = typer.Option("user", "--actor", help="Actor to attribute the archive to."),
    db: str = _DB_OPTION,
) -> None:
    """Archive an inbox task (mirrors the cockpit archive action)."""
    project = _project_from_task_id(task_id)
    svc = _svc(db, project=project)
    try:
        task = svc.archive_task(task_id, actor=actor)
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"{task.task_id} → {task.work_status.value}")
