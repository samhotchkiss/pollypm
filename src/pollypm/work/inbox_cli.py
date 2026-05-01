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
from pollypm.inbox_message_refs import unknown_project_refs
from pollypm.work.cli import (
    _DB_OPTION,
    _JSON_OPTION,
    _PROJECT_OPTION,
    _project_from_task_id,
    _render_work_service_error,
    _resolve_db_path,
    _svc,
    _task_to_dict,
    task_get,
)
from pollypm.work.inbox_view import inbox_tasks


def _format_inbox_title(title: str) -> str:
    """Trim the title for the CLI list view.

    Strips the leading title-contract bracket tag (``[Action]``,
    ``[FYI]``, ``[Audit]``, ``[Alert]``, ``[Task]``, ``[Note]``) so
    the dedicated Type / Priority columns aren't restated inside
    every row. Without this, every row reads ``[Action] …`` and the
    bracket tag eats the same characters of horizontal space the
    user actually wants for the subject. Then truncates to fit the
    column.
    """
    text = (title or "").strip()
    # Match the title-contract grammar: ``[Foo] `` at the very start.
    # Keeps custom bracketed prefixes the caller chose intact when
    # they're more than just the tag (e.g. the body starts with
    # ``[Done] milestone 02``).
    import re as _re
    match = _re.match(
        r"^\s*\[(?:Action|FYI|Audit|Alert|Task|Note)\]\s+",
        text,
    )
    if match:
        text = text[match.end():]
    if len(text) > 38:
        text = text[:37] + "…"
    return text


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


def _message_has_channel_label(row: dict[str, Any], channel: str) -> bool:
    """Return True if ``row`` carries the ``channel:<channel>`` label.

    The default channel is ``inbox`` — messages without any explicit
    channel label are treated as inbox-channel so existing callers
    keep working. See #754.
    """
    import json as _json
    raw = row.get("labels")
    labels: list[str] = []
    if isinstance(raw, list):
        labels = [str(x) for x in raw]
    elif isinstance(raw, str) and raw:
        try:
            parsed = _json.loads(raw)
            if isinstance(parsed, list):
                labels = [str(x) for x in parsed]
        except ValueError:
            labels = []
    explicit = [lab[len("channel:"):] for lab in labels if lab.startswith("channel:")]
    actual = explicit[0] if explicit else "inbox"
    return actual == channel


@inbox_app.callback(invoke_without_command=True)
def inbox_root(
    ctx: typer.Context,
    project: Optional[str] = _PROJECT_OPTION,
    db: str = _DB_OPTION,
    output_json: bool = _JSON_OPTION,
    channel: str = typer.Option(
        "inbox", "--channel",
        help=(
            "Filter messages by delivery channel (#754). ``inbox`` "
            "(default) shows real user-facing notifications. Pass "
            "``dev`` to surface developer / test-harness traffic "
            "that's normally hidden. Pass ``all`` to show every channel."
        ),
    ),
    include_inbox: bool = typer.Option(
        False,
        "--include-inbox",
        help=(
            "Also list ``pm notify``-backed inbox tasks (chat-flow rows "
            "carrying the ``notify`` label). Hidden by default because "
            "they're stub announcements with no node-level transition "
            "affordance — the architect's plan_review handoff lands as "
            "one of these and clutters the listing without giving the "
            "user anything actionable to type. The cockpit inbox pane "
            "still surfaces them via its own structured action affordances. "
            "(#1013, mirrors the ``pm task list`` opt-in shipped in #1003.)"
        ),
    ),
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

    channel_filter = (channel or "inbox").strip().lower()
    if channel_filter not in {"inbox", "dev", "all"}:
        typer.echo(
            f"Error: --channel must be 'inbox', 'dev', or 'all' (got {channel!r}).",
            err=True,
        )
        raise typer.Exit(code=1)

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

    # Channel filter (#754): ``inbox`` (default) hides dev-channel
    # messages, ``dev`` shows only dev-channel, ``all`` shows both.
    if channel_filter != "all":
        message_rows = [
            r for r in message_rows
            if _message_has_channel_label(r, channel_filter)
        ]

    display_messages = [_message_row_to_display(r) for r in message_rows]

    # --- Tasks path (work-service, chat flow) --------------------------
    svc = _svc(db, project=project)
    tasks = inbox_tasks(svc, project=project)

    # #1013 — hide ``pm notify``-backed stub tasks (chat-flow rows
    # carrying the ``notify`` label) by default. They're announcements
    # with no node-level transition affordance and the architect's
    # plan_review handoff lands as one of them. The cockpit inbox pane
    # still surfaces them via its specialised actions; the CLI listing
    # has no equivalent affordance, so listing them just buries the
    # genuinely actionable rows.
    if not include_inbox:
        from pollypm.notify_task import is_notify_inbox_task
        tasks = [task for task in tasks if not is_notify_inbox_task(task)]

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
    item_word = "item" if total == 1 else "items"
    typer.echo(f"Inbox: {total} {item_word}")
    if total == 0:
        typer.echo("No messages waiting for you.")
        return

    typer.echo(f"{'ID':<20} {'Type':<10} {'Priority':<10} {'Title'}")
    typer.echo("-" * 70)
    for m in display_messages:
        title = _format_inbox_title(m["title"])
        typer.echo(
            f"{m['id']:<20} {m['type']:<10} "
            f"{m['priority']:<10} {title}"
        )
    for t in tasks:
        title = _format_inbox_title(t.title or "")
        typer.echo(
            f"{t.task_id:<20} {t.work_status.value:<10} "
            f"{t.priority.value:<10} {title}"
        )


@inbox_app.command("show")
def inbox_show(
    task_id: str = typer.Argument(
        ...,
        help="Task ID (``project/number``) or message ID (``msg:N``)",
    ),
    db: str = _DB_OPTION,
    output_json: bool = _JSON_OPTION,
) -> None:
    """Show full details of an inbox task or message.

    Accepts both ID forms that ``pm inbox --json`` emits:
    - ``project/number`` — delegates to ``pm task get``.
    - ``msg:N`` — loads a row from the unified messages store
      (``pm notify`` writes, heartbeat alerts, etc.). #760.
    """
    if task_id.startswith("msg:"):
        _show_message_by_id(db=db, msg_id_str=task_id, output_json=output_json)
        return
    task_get(task_id=task_id, db=db, output_json=output_json)


def _show_message_by_id(*, db: str, msg_id_str: str, output_json: bool) -> None:
    """Render a single message row identified by ``msg:<N>``."""
    try:
        msg_id = int(msg_id_str.split(":", 1)[1])
    except (IndexError, ValueError):
        typer.echo(f"Error: invalid message id {msg_id_str!r}.", err=True)
        raise typer.Exit(code=2)

    db_path = _resolve_db_path(db, project=None)
    try:
        from pollypm.store import SQLAlchemyStore
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"Error: unified store unavailable ({exc}).", err=True)
        raise typer.Exit(code=1)

    store = SQLAlchemyStore(f"sqlite:///{db_path}")
    try:
        # query_messages has no id filter; scan recent rows and pick the
        # match. Inbox messages stay under a few hundred thousand rows in
        # practice and this command is ad-hoc, so a linear scan is fine.
        rows = store.query_messages(recipient="user")
        match = next((row for row in rows if row.get("id") == msg_id), None)
    finally:
        store.close()

    if match is None:
        typer.echo(
            f"Error: no message with id {msg_id_str!r} (user recipient, any state).",
            err=True,
        )
        raise typer.Exit(code=1)

    if output_json:
        import json as _json

        typer.echo(_json.dumps(_serialize_message(match), indent=2, default=str))
        return

    for line in _render_message_display(match):
        typer.echo(line)


def _serialize_message(row: dict[str, Any]) -> dict[str, Any]:
    """JSON-ready projection of a messages-table row."""
    out = dict(row)
    for key in ("created_at", "updated_at", "closed_at"):
        value = out.get(key)
        if value is not None and not isinstance(value, str):
            out[key] = str(value)
    return out


def _render_message_display(row: dict[str, Any]) -> list[str]:
    """Human-readable lines for ``pm inbox show msg:N`` on a terminal."""
    mid = row.get("id")
    subject = row.get("subject") or "(no subject)"
    # Strip notify/supervisor routing tags ("[Action]", "[Alert]") so
    # the user-facing CLI matches the cockpit-pane inbox detail
    # rendering. Raw tags are routing artefacts, not natural language.
    try:
        from pollypm.cockpit_ui import _strip_action_subject_prefix
        subject = _strip_action_subject_prefix(subject)
    except Exception:  # noqa: BLE001
        pass
    sender = row.get("sender") or "(unknown)"
    recipient = row.get("recipient") or "user"
    scope = row.get("scope") or "-"
    msg_type = row.get("type") or "notify"
    tier = row.get("tier") or "immediate"
    state = row.get("state") or "open"
    created = row.get("created_at") or ""
    labels = row.get("labels")
    if isinstance(labels, str):
        import json as _json

        try:
            labels = _json.loads(labels)
        except Exception:  # noqa: BLE001
            labels = []
    # Producer always serialises a list, but a corrupt row could land
    # a dict / string / null — without coercion ``for label in labels``
    # would iterate dict keys or string characters as fake "labels".
    if not isinstance(labels, list):
        labels = []
    lines = [
        f"msg:{mid}",
        f"  subject:   {subject}",
        f"  type:      {msg_type} / {tier}",
        f"  state:     {state}",
        f"  sender:    {sender}",
        f"  recipient: {recipient}",
        f"  scope:     {scope}",
        f"  created:   {created}",
    ]
    if labels:
        lines.append(f"  labels:    {', '.join(str(label) for label in labels)}")
    payload = row.get("payload") or {}
    if isinstance(payload, dict):
        prompt_lines = _render_user_prompt_lines(payload.get("user_prompt"))
        if prompt_lines:
            lines.append("")
            lines.append("  user_prompt:")
            for prompt_line in prompt_lines:
                lines.append(f"    {prompt_line}")
    body = (row.get("body") or "").rstrip()
    if body:
        lines.append("")
        lines.append("  body:")
        for body_line in body.splitlines():
            lines.append(f"    {body_line}")
    return lines


def _render_user_prompt_lines(prompt: object) -> list[str]:
    """Plain-text rendering of a structured ``user_prompt`` payload.

    Mirrors the cockpit detail-pane block (see
    ``cockpit_ui._render_user_prompt_block``) so a CLI inspector and
    the TUI surface the same plain-English summary, steps, and
    decision question. Without this, ``pm inbox show msg:N`` only
    prints the raw worker body and the operator never sees the
    structured copy the architect/PM authored.
    """
    if not isinstance(prompt, dict):
        return []
    summary = str(prompt.get("summary") or "").strip()
    question = str(prompt.get("question") or "").strip()
    raw_steps = prompt.get("steps") or prompt.get("required_actions") or []
    if not isinstance(raw_steps, list):
        raw_steps = []
    steps = [str(step).strip() for step in raw_steps if str(step).strip()][:5]
    if not (summary or question or steps):
        return []
    out: list[str] = []
    if summary:
        out.append(f"summary:  {summary}")
    if steps:
        heading = str(prompt.get("steps_heading") or "").strip() or "What to do"
        out.append(f"{heading}:")
        for idx, step in enumerate(steps, start=1):
            out.append(f"  {idx}. {step}")
    if question:
        out.append(f"decision: {question}")
    return out


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
        typer.echo(_render_work_service_error(exc, svc.add_reply), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"{task_id} reply @ {entry.timestamp.isoformat()}")


@inbox_app.command("archive")
def inbox_archive(
    task_id: Optional[str] = typer.Argument(
        None,
        help=(
            "Task ID (``project/number``) or message ID (``msg:N``). "
            "Omit when using ``--match``."
        ),
    ),
    match: Optional[str] = typer.Option(
        None,
        "--match",
        help=(
            "Glob pattern matched against message titles. "
            "Archives every open user-recipient message whose title "
            "matches. Useful for cleaning up test-harness noise like "
            "``--match 'loop-test-*'`` (#754)."
        ),
    ),
    deleted_projects: bool = typer.Option(
        False,
        "--deleted-projects",
        help=(
            "Archive open user-recipient messages whose structured project "
            "references point at projects no longer registered in config."
        ),
    ),
    read: bool = typer.Option(
        False,
        "--read",
        help=(
            "Bulk \"mark all read\" — archive every open user-recipient "
            "notify message in scope. Pinned notifies (label ``pinned``) "
            "are exempt. Use ``--dry-run`` to preview. (#1013)"
        ),
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help=(
            "With --match / --deleted-projects / --read: print what would "
            "be archived without changing state."
        ),
    ),
    actor: str = typer.Option("user", "--actor", help="Actor to attribute the archive to."),
    db: str = _DB_OPTION,
) -> None:
    """Archive an inbox task or message (mirrors the cockpit archive action).

    Four modes:

    - ``pm inbox archive demo/1`` — archive a single work-service task
      (the original behavior).
    - ``pm inbox archive msg:628`` — archive a single notify/alert
      message in the unified messages store.
    - ``pm inbox archive --match 'loop-test-*'`` — bulk archive every
      open user-recipient message whose title matches the glob. Add
      ``--dry-run`` to preview.
    - ``pm inbox archive --deleted-projects`` — archive stale messages
      whose structured project refs no longer exist in the active config.
    """
    bulk_modes = sum(
        1 for enabled in (match is not None, deleted_projects, read) if enabled
    )
    if bulk_modes > 1:
        typer.echo(
            "Error: use only one bulk archive mode: --match, "
            "--deleted-projects, or --read.",
            err=True,
        )
        raise typer.Exit(code=2)

    if match is not None:
        _bulk_archive_by_match(db=db, pattern=match, dry_run=dry_run)
        return

    if deleted_projects:
        _bulk_archive_deleted_project_messages(db=db, dry_run=dry_run)
        return

    if read:
        _bulk_archive_all_notifies(db=db, dry_run=dry_run)
        return

    if task_id is None:
        typer.echo(
            "Error: pass a task_id/message-id, OR use --match '<pattern>', "
            "--deleted-projects, or --read.",
            err=True,
        )
        raise typer.Exit(code=2)

    # Message IDs (from `pm inbox --json`) use the ``msg:<n>`` prefix.
    if task_id.startswith("msg:"):
        _archive_message_by_id(db=db, msg_id_str=task_id)
        return

    project = _project_from_task_id(task_id)
    svc = _svc(db, project=project)
    try:
        task = svc.archive_task(task_id, actor=actor)
    except Exception as exc:  # noqa: BLE001
        typer.echo(_render_work_service_error(exc, svc.archive_task), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"{task.task_id} → {task.work_status.value}")


def _archive_message_by_id(*, db: str, msg_id_str: str) -> None:
    """Close a single message row by its ``msg:N`` ID."""
    try:
        raw = msg_id_str.split(":", 1)[1]
        msg_id = int(raw)
    except (IndexError, ValueError):
        typer.echo(f"Error: invalid message id {msg_id_str!r}.", err=True)
        raise typer.Exit(code=2)

    db_path = _resolve_db_path(db, project=None)
    try:
        from pollypm.store import SQLAlchemyStore
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"Error: unified store unavailable ({exc}).", err=True)
        raise typer.Exit(code=1)

    store = SQLAlchemyStore(f"sqlite:///{db_path}")
    try:
        store.close_message(msg_id)
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"Error: failed to archive msg:{msg_id} ({exc}).", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        store.close()
    typer.echo(f"msg:{msg_id} → archived")


def _bulk_archive_by_match(*, db: str, pattern: str, dry_run: bool) -> None:
    """Archive every open user-recipient message whose title matches ``pattern``."""
    import fnmatch

    db_path = _resolve_db_path(db, project=None)
    try:
        from pollypm.store import SQLAlchemyStore
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"Error: unified store unavailable ({exc}).", err=True)
        raise typer.Exit(code=1)

    store = SQLAlchemyStore(f"sqlite:///{db_path}")
    try:
        rows = store.query_messages(
            recipient="user", state="open",
            type=["notify", "inbox_task", "alert"],
        )
    except Exception as exc:  # noqa: BLE001
        store.close()
        typer.echo(f"Error: query_messages failed ({exc}).", err=True)
        raise typer.Exit(code=1) from exc

    matches = []
    for row in rows:
        subject = row.get("subject") or row.get("title") or ""
        if fnmatch.fnmatch(subject, pattern):
            matches.append(row)

    if not matches:
        store.close()
        typer.echo(f"No open messages matched {pattern!r}.")
        return

    if dry_run:
        n = len(matches)
        word = "message" if n == 1 else "messages"
        typer.echo(f"Would archive {n} {word}:")
        for row in matches[:20]:
            mid = row.get("id") or row.get("message_id")
            subject = row.get("subject") or row.get("title") or ""
            typer.echo(f"  msg:{mid}  {subject[:80]}")
        if len(matches) > 20:
            typer.echo(f"  … ({len(matches) - 20} more)")
        store.close()
        return

    closed = 0
    failures: list[tuple[int, str]] = []
    for row in matches:
        mid = row.get("id") or row.get("message_id")
        if mid is None:
            continue
        try:
            store.close_message(int(mid))
            closed += 1
        except Exception as exc:  # noqa: BLE001
            failures.append((int(mid), str(exc)))
    store.close()

    word = "message" if closed == 1 else "messages"
    typer.echo(f"Archived {closed} {word} matching {pattern!r}.")
    if failures:
        typer.echo(f"Failed to archive {len(failures)}:", err=True)
        for mid, reason in failures[:5]:
            typer.echo(f"  msg:{mid}: {reason}", err=True)


def _bulk_archive_all_notifies(*, db: str, dry_run: bool) -> None:
    """Archive every open user-recipient notify message — bulk \"mark all read\".

    The companion to :func:`pollypm.inbox_sweep.sweep_stale_notifies`,
    which runs automatically from the heartbeat tick. This is the
    operator-facing escape hatch for the case the user just wants
    inbox zero now: ``pm inbox archive --read``. Pinned notifies
    (label ``pinned``) are exempt so the operator can flag a notify
    that should survive bulk cleanup. (#1013)
    """
    db_path = _resolve_db_path(db, project=None)
    try:
        from pollypm.store import SQLAlchemyStore
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"Error: unified store unavailable ({exc}).", err=True)
        raise typer.Exit(code=1)

    store = SQLAlchemyStore(f"sqlite:///{db_path}")
    try:
        rows = store.query_messages(
            type="notify", state="open", recipient="user",
        )
    except Exception as exc:  # noqa: BLE001
        store.close()
        typer.echo(f"Error: query_messages failed ({exc}).", err=True)
        raise typer.Exit(code=1) from exc

    # Skip pinned items so the operator's explicit save survives the
    # bulk sweep. The pinning convention is a string label literal
    # (kept consistent with the heartbeat sweep helper).
    matches = []
    for row in rows:
        labels_raw = row.get("labels") or []
        labels = labels_raw if isinstance(labels_raw, list) else []
        if "pinned" in labels:
            continue
        matches.append(row)

    if not matches:
        store.close()
        typer.echo("No open notifies to archive.")
        return

    if dry_run:
        n = len(matches)
        word = "notify" if n == 1 else "notifies"
        typer.echo(f"Would archive {n} {word}:")
        for row in matches[:20]:
            mid = row.get("id") or row.get("message_id")
            subject = row.get("subject") or row.get("title") or ""
            typer.echo(f"  msg:{mid}  {subject[:80]}")
        if len(matches) > 20:
            typer.echo(f"  … ({len(matches) - 20} more)")
        store.close()
        return

    closed = 0
    failures: list[tuple[int, str]] = []
    for row in matches:
        mid = row.get("id") or row.get("message_id")
        if mid is None:
            continue
        try:
            store.close_message(int(mid))
            closed += 1
        except Exception as exc:  # noqa: BLE001
            failures.append((int(mid), str(exc)))
    store.close()

    word = "notify" if closed == 1 else "notifies"
    typer.echo(f"Archived {closed} open {word}.")
    if failures:
        typer.echo(f"Failed to archive {len(failures)}:", err=True)
        for mid, reason in failures[:5]:
            typer.echo(f"  msg:{mid}: {reason}", err=True)


def _known_project_keys() -> set[str]:
    try:
        from pollypm.config import load_config

        config = load_config()
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"Error: failed to load PollyPM config ({exc}).", err=True)
        raise typer.Exit(code=1) from exc
    return set((getattr(config, "projects", {}) or {}).keys())


def _bulk_archive_deleted_project_messages(*, db: str, dry_run: bool) -> None:
    """Archive open user-recipient messages for projects removed from config."""
    known_projects = _known_project_keys()
    db_path = _resolve_db_path(db, project=None)
    try:
        from pollypm.store import SQLAlchemyStore
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"Error: unified store unavailable ({exc}).", err=True)
        raise typer.Exit(code=1)

    store = SQLAlchemyStore(f"sqlite:///{db_path}")
    try:
        rows = store.query_messages(
            recipient="user", state="open",
            type=["notify", "inbox_task", "alert"],
        )
    except Exception as exc:  # noqa: BLE001
        store.close()
        typer.echo(f"Error: query_messages failed ({exc}).", err=True)
        raise typer.Exit(code=1) from exc

    matches: list[tuple[dict[str, Any], set[str]]] = []
    for row in rows:
        missing = unknown_project_refs(row, known_projects)
        if missing:
            matches.append((row, missing))

    if not matches:
        store.close()
        typer.echo("No open messages referenced deleted projects.")
        return

    if dry_run:
        n = len(matches)
        word = "message" if n == 1 else "messages"
        typer.echo(f"Would archive {n} deleted-project {word}:")
        for row, missing in matches[:20]:
            mid = row.get("id") or row.get("message_id")
            subject = row.get("subject") or row.get("title") or ""
            refs = ", ".join(sorted(missing))
            typer.echo(f"  msg:{mid}  [{refs}]  {subject[:80]}")
        if len(matches) > 20:
            typer.echo(f"  ... ({len(matches) - 20} more)")
        store.close()
        return

    closed = 0
    failures: list[tuple[int, str]] = []
    for row, _missing in matches:
        mid = row.get("id") or row.get("message_id")
        if mid is None:
            continue
        try:
            store.close_message(int(mid))
            closed += 1
        except Exception as exc:  # noqa: BLE001
            failures.append((int(mid), str(exc)))
    store.close()

    word = "message" if closed == 1 else "messages"
    typer.echo(f"Archived {closed} deleted-project {word}.")
    if failures:
        typer.echo(f"Failed to archive {len(failures)}:", err=True)
        for mid, reason in failures[:5]:
            typer.echo(f"  msg:{mid}: {reason}", err=True)
