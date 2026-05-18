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
import re
from types import SimpleNamespace
from typing import Any

import typer

from pollypm.cli_help import help_with_examples
from pollypm.inbox import awaits_user
from pollypm.inbox.backfill_heuristics import (
    Classification,
    classify_legacy,
    classify_legacy_task,
)
from pollypm.inbox.kind import InboxItemKind, coerce_kind as _coerce_inbox_kind
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
            (
                "pm inbox --awaits-user",
                "filter to rows the rail badge counts (#1571)",
            ),
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
    if not isinstance(payload, dict):
        payload = {}
    scope = row.get("scope") or ""
    sender = row.get("sender") or ""
    project = payload.get("project") or scope or "inbox"
    # Priority inferred from tier — immediate lands open and is actionable.
    tier = row.get("tier") or "immediate"
    priority = "high" if tier == "immediate" and row.get("type") == "alert" else "normal"
    # #1013 — surface dedup state when present so repeats render as
    # "9x - last seen 2d ago" instead of one row per occurrence.
    from pollypm.inbox_dedup import format_dedup_suffix
    dedup_suffix = format_dedup_suffix(payload)
    count_value = payload.get("count") if isinstance(payload, dict) else None
    # #1565 — surface the structured kind so JSON consumers (rail,
    # dashboard, ``pm inbox --awaits-user``) can read it without
    # having to import ``coerce_kind`` themselves. Falls back to
    # ``'legacy'`` for rows that pre-date the column.
    kind_value = _coerce_inbox_kind(row.get("kind")).value
    return {
        "id": f"msg:{row.get('id')}",
        "title": row.get("subject") or "(no subject)",
        "type": row.get("type") or "notify",
        "tier": tier,
        "priority": priority,
        "kind": kind_value,
        "state": row.get("state") or "open",
        "sender": sender,
        "project": project,
        "created_at": str(row.get("created_at") or ""),
        "dedup_count": int(count_value) if isinstance(count_value, int) else None,
        "dedup_suffix": dedup_suffix,
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
    project: str | None = _PROJECT_OPTION,
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
            "(#1013, mirrors the ``pm task list`` opt-in shipped in #1003.) "
            "Prefer ``--awaits-user`` (#1571) when you actually want the "
            "canonical 'what needs my attention' set — ``--include-inbox`` "
            "is the wider chat-flow-row lens, not the curated one."
        ),
    ),
    show_all: bool = typer.Option(
        False,
        "--all",
        help=(
            "Show every inbox row including pure-FYI ``notify``-type "
            "messages (completion announcements, heartbeat alerts, etc.) "
            "that are hidden by default. The default listing surfaces "
            "actionable rows only (reviews, alerts, inbox tasks); "
            "everything else is collapsed behind a footer count so the "
            "single thing that needs your attention doesn't get buried. "
            "(#1027.)"
        ),
    ),
    awaits_user_only: bool = typer.Option(
        False,
        "--awaits-user",
        help=(
            "Filter the listing to rows where the canonical "
            "``pollypm.inbox.awaits_user`` predicate (#1566) returns "
            "True — the same predicate that drives the cockpit rail "
            "badge (#1571) and the upcoming dashboard 'Waiting on you' "
            "section. Use this to sanity-check the badge from the CLI: "
            "the count printed by ``pm inbox --awaits-user`` matches "
            "the rail badge on the same DB."
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

    # #1571 — narrow to the canonical "awaits user" set. The predicate
    # reads ``item.kind`` (Task surfaces it as an attribute; for raw
    # messages we coerce the stored value into a ``kind`` attribute on
    # a tiny shim so the predicate stays the single source of truth).
    if awaits_user_only:
        tasks = [task for task in tasks if awaits_user(task)]
        display_messages = [
            m for m in display_messages
            if awaits_user(SimpleNamespace(kind=m.get("kind")))
        ]

    # #1027 — default-hide pure ``notify``-type messages (completion
    # announcements, heartbeat alerts, "Done:" / "Repeated stale review
    # ping" rows) so the single actionable row the user needs to act on
    # isn't buried under 30 historical FYIs. ``--all`` opts back in.
    # Counted before split so the footer can announce how many were
    # collapsed.
    notification_messages: list[dict[str, Any]] = []
    actionable_messages: list[dict[str, Any]] = display_messages
    if not show_all:
        actionable_messages = []
        for m in display_messages:
            if (m.get("type") or "").lower() == "notify":
                notification_messages.append(m)
            else:
                actionable_messages.append(m)

    if output_json:
        # JSON consumers need the canonical "everything we know about"
        # surface, so the full list ships regardless of ``--all``. The
        # default-hide behaviour is a CLI-rendering concern only.
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

    total_visible = len(tasks) + len(actionable_messages)
    total_all = len(tasks) + len(display_messages)
    item_word = "item" if total_visible == 1 else "items"
    typer.echo(f"Inbox: {total_visible} {item_word}")
    if total_all == 0:
        typer.echo("No messages waiting for you.")
        return
    if total_visible == 0:
        # Every row in scope is a hidden notification; announce the
        # footer alone so the user knows how to surface them.
        hidden_n = len(notification_messages)
        word = "notification" if hidden_n == 1 else "notifications"
        typer.echo(
            f"… {hidden_n} {word} hidden. Use --all to show."
        )
        return

    typer.echo(f"{'ID':<20} {'Type':<10} {'Priority':<10} {'Title'}")
    typer.echo("-" * 70)
    for m in actionable_messages:
        title = _format_inbox_title(m["title"])
        # #1013 — append "9x - last seen 2d ago" when the row has
        # dedup state (count > 1). Empty suffix is the no-op default
        # so the column layout stays stable for non-dedup rows.
        suffix = m.get("dedup_suffix") or ""
        if suffix:
            title = f"{title} ({suffix})"
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

    if notification_messages:
        hidden_n = len(notification_messages)
        word = "notification" if hidden_n == 1 else "notifications"
        typer.echo(
            f"… {hidden_n} {word} hidden. Use --all to show."
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
    from pollypm.notify_task import strip_routing_tag_prefix

    subject = strip_routing_tag_prefix(subject)
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
    task_id: str | None = typer.Argument(
        None,
        help=(
            "Task ID (``project/number``) or message ID (``msg:N``). "
            "Omit when using ``--match``."
        ),
    ),
    match: str | None = typer.Option(
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
    fake_recovery_injections: bool = typer.Option(
        False,
        "--fake-recovery-injections",
        help=(
            "One-shot cleanup for #1076 — archive every open user-recipient "
            "message whose subject matches Polly's "
            "``Nth (suspected) fake RECOVERY MODE injection ...`` shape. "
            "Producer-side gating is in place going forward; this flag "
            "drains the historical stragglers already in the inbox. "
            "Use ``--dry-run`` to preview."
        ),
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help=(
            "With --match / --deleted-projects / --read / "
            "--fake-recovery-injections: print what would be archived "
            "without changing state."
        ),
    ),
    actor: str = typer.Option("user", "--actor", help="Actor to attribute the archive to."),
    db: str = _DB_OPTION,
) -> None:
    """Archive an inbox task or message (mirrors the cockpit archive action).

    Five modes:

    - ``pm inbox archive demo/1`` — archive a single work-service task
      (the original behavior).
    - ``pm inbox archive msg:628`` — archive a single notify/alert
      message in the unified messages store.
    - ``pm inbox archive --match 'loop-test-*'`` — bulk archive every
      open user-recipient message whose title matches the glob. Add
      ``--dry-run`` to preview.
    - ``pm inbox archive --deleted-projects`` — archive stale messages
      whose structured project refs no longer exist in the active config.
    - ``pm inbox archive --fake-recovery-injections`` — one-shot cleanup
      for #1076 stragglers (Polly's "Nth fake RECOVERY MODE injection"
      meta-reports). Producer-side gating prevents new ones.
    """
    bulk_modes = sum(
        1 for enabled in (
            match is not None,
            deleted_projects,
            read,
            fake_recovery_injections,
        )
        if enabled
    )
    if bulk_modes > 1:
        typer.echo(
            "Error: use only one bulk archive mode: --match, "
            "--deleted-projects, --read, or --fake-recovery-injections.",
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

    if fake_recovery_injections:
        _bulk_archive_fake_recovery_injections(db=db, dry_run=dry_run)
        return

    if task_id is None:
        typer.echo(
            "Error: pass a task_id/message-id, OR use --match '<pattern>', "
            "--deleted-projects, --read, or --fake-recovery-injections.",
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


# #1076 — Polly's "Nth (suspected) fake RECOVERY MODE injection ..."
# meta-reports leaked into the user-facing inbox before the producer
# was gated. This regex matches the subject shape so the one-shot
# cleanup can drain stragglers already in the store.
_FAKE_RECOVERY_INJECTION_SUBJECT_RE = re.compile(
    r"\bfake\s+RECOVERY\s+MODE\s+injection\b",
    re.IGNORECASE,
)


def _bulk_archive_fake_recovery_injections(*, db: str, dry_run: bool) -> None:
    """Archive open user-recipient messages matching the #1076 subject shape.

    Producer-side gating (``_is_fake_recovery_injection_subject`` in
    :mod:`pollypm.cli_features.session_runtime`) routes new occurrences
    to ``channel:dev``. This helper drains the historical rows that
    were already in the inbox before the gate landed.
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
        if _FAKE_RECOVERY_INJECTION_SUBJECT_RE.search(subject):
            matches.append(row)

    if not matches:
        store.close()
        typer.echo("No open fake-recovery-injection messages to archive.")
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
    typer.echo(f"Archived {closed} fake-recovery-injection {word}.")
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


# ---------------------------------------------------------------------------
# pm inbox backfill-kinds — one-time legacy-row reclassification (#1570).
#
# The ``kind`` column landed in #1565 with ``'legacy'`` as the default for
# every pre-existing row. The :func:`pollypm.inbox.awaits_user` predicate
# treats ``legacy`` as "awaits user" so the migration window doesn't hide
# work; this command is the one-time pass that runs the heuristics from
# :mod:`pollypm.inbox.backfill_heuristics` against every legacy message and
# moves the matched rows onto a real kind. Unmatched rows stay legacy and
# remain visible to the predicate.
# ---------------------------------------------------------------------------


_BACKFILL_HINT = (
    "Re-run with --commit to apply; unmatched rows stay legacy and "
    "remain visible to the awaits_user predicate."
)


def _legacy_message_rows(
    db_path: str,
    *,
    project: str | None,
) -> list[dict[str, Any]]:
    """Return every open user-facing message still tagged ``kind='legacy'``.

    Scope mirrors ``pm inbox`` (recipient=user, the same three message
    types) so the backfill operates over exactly the population the
    dashboard's "Waiting on you" section would otherwise lump together.
    Closed rows are ignored — they are archive, not inbox.

    ``query_messages`` does not accept ``kind`` as a server-side filter,
    so we trim in Python after the call. The volume is bounded (low
    hundreds for the original 2026-05-17 sample); a wider filter would
    require widening :meth:`SQLAlchemyStore.query_messages` and is out
    of scope for the one-shot migration helper.
    """
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
        rows = store.query_messages(**filters)
    finally:
        store.close()

    legacy_rows: list[dict[str, Any]] = []
    for row in rows:
        kind_value = _coerce_inbox_kind(row.get("kind"))
        if kind_value is InboxItemKind.LEGACY:
            legacy_rows.append(row)
    return legacy_rows


def _row_classification(
    row: dict[str, Any],
) -> Classification | None:
    """Run the heuristic against one row's title / sender / scope."""
    return classify_legacy(
        title=str(row.get("subject") or ""),
        sender=str(row.get("sender") or ""),
        project=str(row.get("scope") or ""),
    )


def _title_preview(title: str, limit: int = 60) -> str:
    text = (title or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _apply_kind_update(
    db_path: str,
    *,
    msg_id: int,
    new_kind: InboxItemKind,
) -> None:
    """Persist a single ``messages.kind`` reclassification."""
    from pollypm.store import SQLAlchemyStore

    store = SQLAlchemyStore(f"sqlite:///{db_path}")
    try:
        store.update_message(msg_id, kind=new_kind.value)
    finally:
        store.close()


def _emit_backfill_audit(
    *,
    project: str,
    subject: str,
    new_kind: InboxItemKind,
    heuristic: str,
) -> None:
    """Audit-log one reclassification. Best-effort — never raises.

    ``subject`` is the audit-event subject (``msg:<id>`` for message
    backfills, ``<project>/<task_number>`` for task backfills) so a
    forensic read can tell which surface a row came from.
    """
    from pollypm.audit.log import EVENT_INBOX_KIND_BACKFILLED, emit

    emit(
        event=EVENT_INBOX_KIND_BACKFILLED,
        project=project or "",
        subject=subject,
        actor="user",
        status="ok",
        metadata={
            "old_kind": InboxItemKind.LEGACY.value,
            "new_kind": new_kind.value,
            "heuristic": heuristic,
        },
    )


# ---------------------------------------------------------------------------
# Task-side backfill (work_tasks.kind, #1564 follow-up)
# ---------------------------------------------------------------------------


def _legacy_task_rows(
    db_path: str,
    *,
    project: str | None,
) -> list[Any]:
    """Return every non-terminal task still tagged ``kind='legacy'``.

    Mirrors the scope of :func:`_legacy_message_rows` (the population
    the dashboard's "Waiting on you" section would otherwise lump
    together). Terminal-state tasks (``done`` / ``cancelled``) are
    intentionally included: the user's 2026-05-17 dashboard surfaced
    cancelled ``audit_watchdog`` rows, and the awaits-user predicate
    treats ``legacy`` as visible regardless of work_status.
    """
    from pollypm.work import create_work_service

    from pathlib import Path

    svc = create_work_service(
        db_path=db_path,
        project_path=Path(db_path).parent.parent,
    )
    try:
        tasks = svc.list_tasks(project=project)
    finally:
        svc.close()

    return [
        task for task in tasks
        if getattr(task, "kind", InboxItemKind.LEGACY) is InboxItemKind.LEGACY
    ]


def _task_classification(task: Any) -> Classification | None:
    """Run the task-side heuristic against one row's title + creator."""
    return classify_legacy_task(
        title=str(getattr(task, "title", "") or ""),
        created_by=str(getattr(task, "created_by", "") or ""),
    )


def _apply_task_kind_update(
    db_path: str,
    *,
    task_id: str,
    new_kind: InboxItemKind,
) -> None:
    """Persist a single ``work_tasks.kind`` reclassification."""
    from pollypm.work import create_work_service

    from pathlib import Path

    svc = create_work_service(
        db_path=db_path,
        project_path=Path(db_path).parent.parent,
    )
    try:
        svc.backfill_kind(task_id, new_kind=new_kind.value)
    finally:
        svc.close()


@inbox_app.command(
    "backfill-kinds",
    help=(
        "One-time backfill of ``kind`` for inbox rows that still carry "
        "``kind='legacy'`` (#1570, #1564 follow-up). Scans both the "
        "messages table and the work_tasks table; dry-run by default — "
        "pass --commit to actually mutate. Idempotent: rows already on "
        "a non-legacy kind are skipped."
    ),
)
def backfill_kinds(
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "Classify every legacy row and print what would change "
            "without writing to the DB. This is the default behaviour "
            "when neither --dry-run nor --commit is passed — the flag "
            "exists so an automation can make the intent explicit."
        ),
    ),
    commit: bool = typer.Option(
        False,
        "--commit",
        help=(
            "Apply the reclassification. Required for any DB mutation "
            "— mutually exclusive with --dry-run."
        ),
    ),
    project: str | None = _PROJECT_OPTION,
    db: str = _DB_OPTION,
) -> None:
    """Reclassify legacy inbox rows by heuristic.

    Heuristics live in :mod:`pollypm.inbox.backfill_heuristics` and
    are applied in spec order; unmatched rows stay legacy. Scans the
    messages table and the work_tasks table; the dry-run report
    breaks the counts down per surface so the operator can see what
    each table contributes. Every committed row emits one
    ``inbox.kind_backfilled`` audit event with the old kind, new
    kind, and matched heuristic name.
    """
    if commit and dry_run:
        typer.echo(
            "Error: --commit and --dry-run are mutually exclusive.",
            err=True,
        )
        raise typer.Exit(code=2)

    # Default = dry-run. --commit is the explicit opt-in to mutation.
    effective_commit = bool(commit)

    db_path = _resolve_db_path(db, project=project)

    try:
        message_rows = _legacy_message_rows(db_path, project=project)
    except Exception as exc:  # noqa: BLE001
        typer.echo(
            f"Error: failed to read legacy inbox rows ({exc}).",
            err=True,
        )
        raise typer.Exit(code=1) from exc

    try:
        task_rows = _legacy_task_rows(db_path, project=project)
    except Exception as exc:  # noqa: BLE001
        typer.echo(
            f"Error: failed to read legacy work_task rows ({exc}).",
            err=True,
        )
        raise typer.Exit(code=1) from exc

    if not message_rows and not task_rows:
        typer.echo("No legacy inbox rows to reclassify.")
        return

    mode_label = "commit" if effective_commit else "dry-run"
    typer.echo(
        f"Scanning {len(message_rows)} legacy message row(s) and "
        f"{len(task_rows)} legacy task row(s) ({mode_label})."
    )

    msg_matched = msg_unmatched = 0
    task_matched = task_unmatched = 0
    msg_failures: list[tuple[int, str]] = []
    task_failures: list[tuple[str, str]] = []

    # Messages surface.
    if message_rows:
        typer.echo("")
        typer.echo("Messages:")
        typer.echo(
            f"{'ID':<12} {'Project':<18} {'Heuristic → New kind':<48} Title"
        )
        typer.echo("-" * 110)
        for row in message_rows:
            msg_id_raw = row.get("id")
            if msg_id_raw is None:
                continue
            msg_id = int(msg_id_raw)
            scope = str(row.get("scope") or "-")
            title_preview = _title_preview(str(row.get("subject") or ""))
            classification = _row_classification(row)
            if classification is None:
                msg_unmatched += 1
                typer.echo(
                    f"msg:{msg_id:<8} {scope:<18} {'(unmatched, stays legacy)':<48} "
                    f"{title_preview}"
                )
                continue

            msg_matched += 1
            action = (
                f"{classification.heuristic} → {classification.kind.value}"
            )
            typer.echo(
                f"msg:{msg_id:<8} {scope:<18} {action:<48} {title_preview}"
            )
            if not effective_commit:
                continue
            try:
                _apply_kind_update(
                    db_path, msg_id=msg_id, new_kind=classification.kind,
                )
            except Exception as exc:  # noqa: BLE001
                msg_failures.append((msg_id, str(exc)))
                continue
            _emit_backfill_audit(
                project=scope,
                subject=f"msg:{msg_id}",
                new_kind=classification.kind,
                heuristic=classification.heuristic,
            )

    # Tasks surface.
    if task_rows:
        typer.echo("")
        typer.echo("Tasks:")
        typer.echo(
            f"{'ID':<18} {'Project':<18} {'Heuristic → New kind':<48} Title"
        )
        typer.echo("-" * 110)
        for task in task_rows:
            task_id = str(getattr(task, "task_id", "") or "")
            if not task_id:
                continue
            task_project = str(getattr(task, "project", "") or "-")
            title_preview = _title_preview(
                str(getattr(task, "title", "") or "")
            )
            classification = _task_classification(task)
            if classification is None:
                task_unmatched += 1
                typer.echo(
                    f"{task_id:<18} {task_project:<18} "
                    f"{'(unmatched, stays legacy)':<48} {title_preview}"
                )
                continue

            task_matched += 1
            action = (
                f"{classification.heuristic} → {classification.kind.value}"
            )
            typer.echo(
                f"{task_id:<18} {task_project:<18} {action:<48} {title_preview}"
            )
            if not effective_commit:
                continue
            try:
                _apply_task_kind_update(
                    db_path,
                    task_id=task_id,
                    new_kind=classification.kind,
                )
            except Exception as exc:  # noqa: BLE001
                task_failures.append((task_id, str(exc)))
                continue
            _emit_backfill_audit(
                project=task_project,
                subject=task_id,
                new_kind=classification.kind,
                heuristic=classification.heuristic,
            )

    typer.echo("-" * 110)
    matched = msg_matched + task_matched
    unmatched = msg_unmatched + task_unmatched
    if effective_commit:
        typer.echo(
            f"Reclassified {matched} row(s) "
            f"({msg_matched} message(s), {task_matched} task(s)); "
            f"{unmatched} unmatched "
            f"({msg_unmatched} message(s), {task_unmatched} task(s))."
        )
        if msg_failures or task_failures:
            total_failures = len(msg_failures) + len(task_failures)
            typer.echo(
                f"Failed to update {total_failures} row(s):", err=True,
            )
            for mid, reason in msg_failures[:5]:
                typer.echo(f"  msg:{mid}: {reason}", err=True)
            for tid, reason in task_failures[:5]:
                typer.echo(f"  {tid}: {reason}", err=True)
    else:
        typer.echo(
            f"Would reclassify {matched} row(s) "
            f"({msg_matched} message(s), {task_matched} task(s)); "
            f"{unmatched} unmatched "
            f"({msg_unmatched} message(s), {task_unmatched} task(s))."
        )
    typer.echo(_BACKFILL_HINT)
