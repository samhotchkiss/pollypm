"""Inbox row-formatting + plan-review helpers extracted from ``cockpit_ui``.

Contract:
- Inputs: ``Task`` / ``InboxEntry`` / ``InboxThreadRow`` instances along
  with cockpit config-path context. All helpers are pure or
  side-effect-minimal (``_plan_content_for_review`` reads a single plan
  markdown file off disk when called).
- Outputs: Rich ``Text`` objects, plain markup strings, sort keys,
  structured metadata dicts, and PM-input primer strings.
- Side effects: none beyond the optional plan-markdown read inside
  ``_plan_content_for_review`` and the config load that resolves the
  project path for that lookup.
- Invariants: this module owns inbox row rendering + plan-review label
  / primer plumbing. It must not import ``PollyInboxApp`` or any other
  Textual ``App`` subclass. All symbols defined here are re-exported
  from ``pollypm.cockpit_ui`` for back-compat with importers and
  ``monkeypatch.setattr('pollypm.cockpit_ui._format_inbox_thread_row',
  ...)`` hooks. See #1354 — this is the second slice of the rolling
  cockpit_ui split (first slice: ``cockpit_settings_gather``, PR #1911).
- Allowed dependencies: stdlib, ``rich.text.Text``,
  ``pollypm.cockpit_inbox`` (row type), ``pollypm.cockpit_inbox_items``
  (entry-shape predicate), ``pollypm.cockpit_markup`` (escape helper),
  ``pollypm.config`` (config loader), ``pollypm.notify_task``,
  ``pollypm.rejection_feedback``, ``pollypm.plan_presence``,
  ``pollypm.tz`` (relative-age formatter; lazy-imported by callers).
- Private: ``_dashboard_summary_from_body`` /
  ``_dashboard_steps_from_body`` / ``_dashboard_plan_path`` still live
  in ``pollypm.cockpit_ui`` (later split). The two call sites that need
  them (:func:`_render_heuristic_action_block`,
  :func:`_plan_content_for_review`) import them lazily to avoid a
  module-level back-import while #1354 is in flight.

No behaviour changes — pure module-boundary move.
"""

from __future__ import annotations

import re as _re
from pathlib import Path

from rich.text import Text

from pollypm.cockpit_inbox import InboxThreadRow
from pollypm.cockpit_inbox_items import is_task_inbox_entry
from pollypm.cockpit_markup import _escape
from pollypm.cockpit_theme import State
from pollypm.config import load_config
from pollypm.notify_task import strip_routing_tag_prefix
from pollypm.rejection_feedback import (
    feedback_target_task_id,
    is_rejection_feedback_task,
)


# Mirrors ``cockpit_ui._ACTION_STEP_RE`` (the same module-level regex
# the unsplit cockpit shell uses for plan-step extraction). Kept as a
# private copy here so the inbox-format module has no back-import to
# the god-module being split.
_ACTION_STEP_RE = _re.compile(
    r"^\s*(?:[-*]\s+|\d+\.\s+|\([a-zA-Z]\)\s+)(?P<step>.+\S)\s*$"
)

# Mirrors ``cockpit_ui._PLAN_REVIEW_UNAVAILABLE_HINT_RE``. Plan-review
# message bodies emitted before the explainer-presence fix carried the
# "Press v to open the explainer (unavailable)" hint inline; we strip
# that line at render time when there is no explainer path on the
# inbox row's labels.
_PLAN_REVIEW_UNAVAILABLE_HINT_RE = _re.compile(
    r"Press\s+v\s+to\s+open\s+the\s+explainer\s+\(unavailable\),\s*"
    r"d\s+to\s+discuss\s+with\s+the\s+PM,\s*A\s+to\s+approve\.?",
    _re.IGNORECASE,
)


# Sort: most recent first (matches email-inbox affordance), then priority
# as a secondary key so a newly arrived critical item outranks a slightly
# older normal one. Falls back to title for a stable ordering when two
# tasks share the same minute-resolution timestamp.
_INBOX_PRIORITY_RANK = {
    "critical": 0,
    "high": 1,
    "normal": 2,
    "low": 3,
}


def _inbox_sort_key(task) -> tuple:
    updated = task.updated_at
    iso = updated.isoformat() if hasattr(updated, "isoformat") else str(updated or "")
    prio = getattr(task.priority, "value", str(task.priority))
    triage_rank = getattr(task, "triage_rank", None)
    triage_rank = 2 if triage_rank is None else int(triage_rank)
    # Actionable items sort ahead of informational ones; orphaned
    # deleted-project rows sort last. Within a bucket, newer still wins.
    return (
        triage_rank,
        -_iso_sort_weight(iso),
        _INBOX_PRIORITY_RANK.get(prio, 9),
        task.title,
    )


def _iso_sort_weight(iso: str) -> int:
    """Coerce an ISO timestamp to a comparable integer key.

    Lexicographic compare on ISO-8601 works for "same-offset" strings but
    we want a real ordering regardless. Falling back to string length keeps
    the sort stable for missing/invalid stamps without raising.
    """
    try:
        from datetime import datetime as _dt
        return int(_dt.fromisoformat(iso).timestamp())
    except (ValueError, TypeError):
        return 0


def _format_sender(task) -> str:
    """Best-effort human-friendly sender label for an inbox task.

    Chat-flow tasks have ``roles.operator`` set to whichever agent posted
    (``polly``, ``russell``, …). ``requester=user`` tasks that originate
    from a worker's notify use ``operator`` too. When nothing resolves,
    fall back to ``created_by``.
    """
    sender = getattr(task, "sender", None)
    if sender and sender != "user":
        return sender
    roles = getattr(task, "roles", {}) or {}
    op = roles.get("operator")
    if op and op != "user":
        return op
    if task.created_by and task.created_by != "user":
        return task.created_by
    # Last resort — unknown sender. Don't show blank.
    return "polly"


def _triage_bucket(task) -> str:
    return str(getattr(task, "triage_bucket", "info") or "info")


def _triage_label(task) -> str:
    label = getattr(task, "triage_label", None)
    if label:
        return str(label)
    if is_rejection_feedback_task(task):
        target = feedback_target_task_id(task)
        if target:
            return f"review feedback for {target}"
        return "review feedback"
    return "update"


def _archive_success_message(item, task_id: str) -> str:
    if is_task_inbox_entry(item):
        return f"Archived {task_id}"
    title = str(getattr(item, "title", "") or "").strip()
    if title:
        return f"Archived {strip_routing_tag_prefix(title)}"
    return "Archived notification"


def _render_user_prompt_block(payload: object) -> str | None:
    """Build the plain-English action block for a message detail pane.

    Architects, reviewers and PMs that send a structured ``user_prompt``
    in the message payload have already done the work of summarising
    *what the user should do*. The detail pane should lead with that
    block — the raw body still renders underneath for technical
    context, but the operator should not have to parse worker jargon
    to figure out the decision being asked of them.

    Returns ``None`` when the payload has no ``user_prompt`` dict, so
    callers can short-circuit and render the legacy body-only layout.
    """
    if not isinstance(payload, dict):
        return None
    prompt = payload.get("user_prompt")
    if not isinstance(prompt, dict):
        return None

    def _plain(value: object | None) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        return " ".join(part.strip() for part in text.splitlines() if part.strip())

    summary = _plain(prompt.get("summary"))
    question = _plain(prompt.get("question"))
    raw_steps = prompt.get("steps") or prompt.get("required_actions") or []
    if not isinstance(raw_steps, list):
        raw_steps = []
    steps = [_plain(step) for step in raw_steps if _plain(step)][:5]
    if not (summary or steps or question):
        return None

    lines: list[str] = []
    if summary:
        lines.append(f"[{State.WAITING}]◆[/{State.WAITING}] {_escape(summary)}")
    heading = _plain(prompt.get("steps_heading")) or "What to do"
    if steps:
        lines.append(f"  [b]{_escape(heading)}[/b]")
        for idx, step in enumerate(steps, start=1):
            lines.append(f"  [dim]{idx}.[/dim] {_escape(step)}")
    if question:
        lines.append(f"  [b]Decision:[/b] {_escape(question)}")
    return "\n".join(lines)


def _render_heuristic_action_block(body: object) -> str | None:
    """Heuristic fallback for messages that lack a ``user_prompt``.

    Mirrors the dashboard's Action Needed card: pull a one-paragraph
    summary out of the body and any numbered "steps" lines, render
    them as the same yellow-diamond block we use for ``user_prompt``
    payloads. The full body still renders below for context, but
    leading with this lifts the operator-visible call to action out
    of jargon-heavy worker output. Returns ``None`` when nothing
    usable can be extracted (e.g. an empty body or a body that's all
    code blocks).
    """
    # Lazy import — the dashboard-body helpers still live in
    # ``cockpit_ui`` (a later #1354 slice). Importing at module load
    # time would create a cycle (cockpit_ui re-exports from this
    # module).
    from pollypm.cockpit_ui import (
        _dashboard_steps_from_body,
        _dashboard_summary_from_body,
    )

    text = str(body or "")
    if not text.strip():
        return None
    summary = _dashboard_summary_from_body(text)
    steps = _dashboard_steps_from_body(text)[:5]
    if not (summary or steps):
        return None
    lines: list[str] = []
    if summary:
        lines.append(f"[{State.WAITING}]◆[/{State.WAITING}] {_escape(summary)}")
    if steps:
        lines.append("  [b]What to do[/b]")
        for idx, step in enumerate(steps, start=1):
            lines.append(f"  [dim]{idx}.[/dim] {_escape(step)}")
    return "\n".join(lines)


def _plan_review_message_body_for_display(body: object, meta: dict) -> str:
    """Remove stale unavailable-explainer instructions from plan review text."""
    text = str(body or "")
    if meta.get("explainer_path"):
        return text
    return _PLAN_REVIEW_UNAVAILABLE_HINT_RE.sub(
        "No visual explainer is available for this plan. "
        "Press d to discuss with the PM or A to approve.",
        text,
    )


def _render_inbox_triage_banner(item) -> str | None:
    bucket = _triage_bucket(item)
    label = _triage_label(item)
    project = (getattr(item, "project", "") or "").strip()
    if bucket == "action":
        return (
            f"[b {State.WAITING}]Action Required[/b {State.WAITING}]"
            f"  [dim]· {_escape(label)}[/dim]"
        )
    if bucket == "orphaned":
        detail = f"{project} is no longer a tracked project." if project else "This project is no longer tracked."
        return (
            f"[b {State.NEUTRAL}]Deleted Project[/b {State.NEUTRAL}]"
            f"  [dim]· {_escape(detail)}[/dim]"
        )
    if label and label != "update":
        return f"[dim]{_escape(label)}[/dim]"
    return None


def _format_inbox_row(
    task,
    *,
    is_unread: bool,
    width: int = 38,
    tree_marker: str = "",
    reply_count: int = 0,
) -> Text:
    """Render one inbox-list row as two lines of Rich text.

    Matches the cockpit aesthetic from RailItem: yellow diamond for
    unread, dim open circle for read.

    Line 1 is the bold message title (truncated with an ellipsis if it
    won't fit ``width`` chars after the unread-marker glyph — no wrap).
    Line 2 is dim ``project · age`` metadata indented under the title.
    """
    from pollypm.tz import format_relative

    text = Text(no_wrap=True, overflow="ellipsis")
    if tree_marker:
        text.append(tree_marker, style=State.MUTED)
    if is_unread:
        text.append("◆ ", style=State.WAITING)  # yellow diamond
    else:
        text.append("○ ", style=State.IDLE)  # dim circle
    subject_prefix = ""
    if is_rejection_feedback_task(task):
        subject_prefix = "🔄 "
        text.append(subject_prefix, style=State.ATTENTION_BRIGHT)
    subject = task.title or "(no subject)"
    # Drop the "[Action]" prefix from action-bucket rows. The inbox
    # already groups action-needed items under their own header, so
    # stamping every title with "[Action]" is redundant noise that
    # eats list-pane width and buries the actual subject.
    if getattr(task, "triage_bucket", "") == "action":
        subject = strip_routing_tag_prefix(subject)
    reply_suffix = ""
    if reply_count:
        noun = "reply" if reply_count == 1 else "replies"
        reply_suffix = f" ({reply_count} {noun})"
    # Account for the 2-char marker glyph prefix so the total row still
    # fits the target list-pane width without wrapping.
    max_subject = max(
        8, width - 2 - len(tree_marker) - len(reply_suffix) - len(subject_prefix)
    )
    if len(subject) > max_subject:
        subject = subject[: max_subject - 1] + "…"
    subject_style = f"bold {State.HEADING}" if is_unread else f"bold {State.LABEL}"
    text.append(subject, style=subject_style)
    if reply_suffix:
        text.append(reply_suffix, style=State.MUTED)

    # Line 2: project · age, dim. Indent by 2 so it lines up under the
    # subject text (past the marker glyph).
    updated = task.updated_at
    iso = updated.isoformat() if hasattr(updated, "isoformat") else str(updated or "")
    age = format_relative(iso) if iso else ""
    raw_project = (task.project or "").strip()
    # The detail pane surfaces workspace-root sentinel items as
    # ``[workspace]`` (cycle 14) — mirror that here so the list-rail
    # label matches the detail surface instead of leaking the raw
    # ``inbox`` sentinel string.
    if raw_project == "inbox":
        project = "[workspace]"
    else:
        project = raw_project or "—"
    meta_bits = [_triage_label(task), project]
    if age:
        meta_bits.append(age)
    meta_indent = " " * max(2, len(tree_marker) + 2)
    meta_line = meta_indent + "  ·  ".join(meta_bits)
    text.append("\n")
    text.append(meta_line, style=State.MUTED)
    return text


def _is_plan_review_task(task) -> bool:
    """True when an inbox entry / task carries the ``plan_review`` label.

    Both message-backed ``InboxEntry`` rows and work-service task rows
    expose ``labels`` as a list of strings, so this helper works
    uniformly across the two streams that feed the inbox list.
    """
    labels = getattr(task, "labels", None) or []
    try:
        return "plan_review" in labels
    except TypeError:
        return False


def _truncate_plan_summary(summary: str, *, max_chars: int = 100) -> str:
    """Trim the architect summary block to a single inbox preview line.

    Collapses internal whitespace (the summary often arrives as a
    multi-line markdown paragraph) and truncates with an ellipsis when
    the result is longer than ``max_chars``. Returns an empty string
    when the input is empty or whitespace-only so callers can fall back
    to a default placeholder.
    """
    if not summary:
        return ""
    flat = " ".join(summary.split()).strip()
    if not flat:
        return ""
    if len(flat) <= max_chars:
        return flat
    # ``-1`` to leave room for the ellipsis glyph.
    return flat[: max(1, max_chars - 1)].rstrip() + "…"


def _format_inbox_plan_review_row(
    task,
    *,
    is_unread: bool,
    width: int = 38,
    tree_marker: str = "",
    config_path: object | None = None,
    judgment_calls: list[str] | None = None,
    show_judgment_calls: bool = False,
) -> Text:
    """Render a ``plan_review`` inbox row as a heavier 2-line decision card.

    Spec from #1400:
    - Line 1: ``▶ Approve plan: <project>/<task_number>`` with a red
      ``▶`` glyph (the rail "needs decision" affordance, kept consistent
      with #1396's color/glyph convention so the operator's eye trains
      to the same shape across surfaces).
    - Line 2: TL;DR summary trimmed to ~100 chars from the architect's
      ``## Summary`` block (loaded via ``_plan_content_for_review`` →
      ``_extract_plan_summary_block``). When no plan markdown is on
      disk yet, fall back to the message body's first non-empty line so
      the row stays informative.
    - When ``show_judgment_calls`` is set (e.g. on the highlighted row),
      append the architect's flagged points as an indented bullet
      sub-section below line 2.

    The visual weight (red glyph + bold heading + dim summary) is
    intentionally heavier than the diamond/circle treatment used for
    other inbox items — these rows demand a decision, not a peek.
    """
    text = Text(no_wrap=True, overflow="ellipsis")
    if tree_marker:
        text.append(tree_marker, style=State.MUTED)

    # Red play glyph — same affordance as the rail's "needs decision"
    # signal so the operator's eye trains to the same shape. Standardised
    # on ``State.BLOCKED`` (``#ff5f6d``); historical drift to ``#ff6b5b``
    # was an accidental copy-paste flagged in the 2026-05-20 audit.
    text.append("▶ ", style=f"bold {State.BLOCKED}")

    # Heading: ``Approve plan: <project>/<task_number>`` (the inbox
    # task_id is already in ``<project>/<n>`` shape, so we don't need
    # to recompose it from labels — falling back to the project key
    # alone keeps the row useful even when task_id is missing).
    task_id = (getattr(task, "task_id", "") or "").strip()
    project = (getattr(task, "project", "") or "").strip()
    if task_id and "/" in task_id:
        ref = task_id
    elif project:
        ref = project
    else:
        ref = "?"
    heading_label = "Approve plan: "
    heading_ref = ref
    heading_style = f"bold {State.WAITING}" if is_unread else f"bold {State.WAITING_DIM}"
    text.append(heading_label, style=heading_style)
    text.append(heading_ref, style=heading_style)

    # Line 2: trimmed summary. We fetch the plan body via the same
    # helper the detail pane uses (#1410). When the plan markdown isn't
    # on disk yet, fall back to the message body's first non-empty line
    # — better than a blank summary while the architect's file is still
    # propagating.
    summary = ""
    try:
        labels = list(getattr(task, "labels", None) or [])
        meta = _extract_plan_review_meta(labels)
        plan_text = _plan_content_for_review(meta, config_path)
        if plan_text:
            summary = _extract_plan_summary_block(plan_text)
    except Exception:  # noqa: BLE001 — never crash the row render
        summary = ""
    if not summary:
        body = (getattr(task, "description", "") or "").strip()
        for line in body.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                summary = stripped
                break
    summary_line = _truncate_plan_summary(summary, max_chars=100)
    if not summary_line:
        summary_line = "(plan summary not yet on disk — open to view)"

    indent = " " * max(2, len(tree_marker) + 2)
    text.append("\n")
    text.append(indent, style=State.MUTED)
    text.append(summary_line, style=State.BODY)

    # Optional flagged-judgment-calls sub-section — only shown when the
    # row is highlighted/expanded so the list stays compact at rest.
    if show_judgment_calls:
        calls = list(judgment_calls or [])
        if not calls:
            try:
                meta = _extract_plan_review_meta(
                    list(getattr(task, "labels", None) or []),
                )
                plan_text = _plan_content_for_review(meta, config_path)
                if plan_text:
                    calls = _extract_plan_judgment_calls(plan_text)
            except Exception:  # noqa: BLE001
                calls = []
        for call in calls[:3]:
            trimmed = _truncate_plan_summary(call, max_chars=80)
            if not trimmed:
                continue
            text.append("\n")
            text.append(indent + "  • ", style=State.WAITING)
            text.append(trimmed, style=State.LABEL_DIM)

    return text


def _format_inbox_reply_row(task, reply, *, width: int = 38) -> Text:
    """Render one inline reply row underneath its parent inbox task."""
    from pollypm.tz import format_relative

    text = Text(no_wrap=True, overflow="ellipsis")
    actor = (getattr(reply, "actor", "") or "user").strip() or "user"
    speaker = "you" if actor == "user" else actor
    target = _format_sender(task) if actor == "user" else "you"
    preview = (getattr(reply, "text", "") or "").strip().splitlines()
    subject = preview[0] if preview else "(no reply text)"
    header = f"{speaker} → {target}  "
    prefix = "  └ "
    max_subject = max(8, width - len(prefix) - len(header))
    if len(subject) > max_subject:
        subject = subject[: max_subject - 1] + "…"
    text.append(prefix, style=State.MUTED)
    text.append(header, style=State.NEUTRAL)
    text.append(subject, style=State.BODY)

    stamped = getattr(reply, "timestamp", None)
    iso = stamped.isoformat() if hasattr(stamped, "isoformat") else str(stamped or "")
    age = format_relative(iso) if iso else ""
    text.append("\n")
    text.append("    " + (age or "reply"), style=State.MUTED_DIM)
    return text


def _format_inbox_thread_row(
    row: InboxThreadRow,
    *,
    is_unread: bool,
    width: int = 38,
    config_path: object | None = None,
    show_judgment_calls: bool = False,
) -> Text:
    """Render either a root task row or an inline reply row.

    ``plan_review`` rows render via :func:`_format_inbox_plan_review_row`
    so the approval decision card stands out from the regular diamond /
    circle inbox items (#1400). ``show_judgment_calls`` is forwarded to
    that renderer — callers set it to True when the row is the current
    selection so the architect's flagged points appear inline.
    """
    if row.is_reply and row.reply is not None:
        return _format_inbox_reply_row(row.task, row.reply, width=width)
    tree_marker = ""
    if row.has_children:
        tree_marker = "▾ " if row.expanded else "▸ "
    if row.is_task and _is_plan_review_task(row.task):
        return _format_inbox_plan_review_row(
            row.task,
            is_unread=is_unread,
            width=width,
            tree_marker=tree_marker,
            config_path=config_path,
            show_judgment_calls=show_judgment_calls,
        )
    return _format_inbox_row(
        row.task,
        is_unread=is_unread,
        width=width,
        tree_marker=tree_marker,
        reply_count=row.reply_count,
    )


def _task_is_rollup(task) -> bool:
    """True when a task was created by notification_staging.flush_milestone_digest.

    Primary signal is the ``rollup`` label added by flush — the title
    regex is a fallback for rollups created before the label landed.
    """
    labels = getattr(task, "labels", None) or []
    if "rollup" in labels:
        return True
    title = (getattr(task, "title", "") or "").lower()
    return "ready for review" in title and "updates" in title


def _fuzzy_subseq_match(query: str, hay: str) -> bool:
    """Inbox text match with fuzzy affordance for short abbreviations.

    Literal substring matches always win. Short abbreviations like
    ``"shp"`` can still match ``"shipped"`` as a subsequence, but
    longer queries stay literal so a word like ``"recovery"`` does not
    match rows where those letters are merely scattered across metadata.
    Empty query matches everything; case is folded before compare so
    the call site doesn't have to.
    """
    if not query:
        return True
    if not hay:
        return False
    if query in hay:
        return True
    if len(query) > 3:
        return False
    qi = 0
    q = query
    for ch in hay:
        if ch == q[qi]:
            qi += 1
            if qi == len(q):
                return True
    return False


def _task_recent_timestamp(task) -> float | None:
    """Return the most-recent updated/created stamp as a unix timestamp.

    Prefers ``updated_at`` (newer thread activity wins), falls back to
    ``created_at``. Returns ``None`` for unparseable values so the
    "recent 24h" filter simply drops them rather than including weirdly
    dated tasks by accident.
    """
    from datetime import datetime as _dt

    for attr in ("updated_at", "created_at"):
        value = getattr(task, attr, None)
        if value is None:
            continue
        try:
            if hasattr(value, "timestamp"):
                return float(value.timestamp())
            return _dt.fromisoformat(str(value)).timestamp()
        except (TypeError, ValueError):
            continue
    return None


def _project_pm_persona(config: object, project_key: str, project: object) -> str | None:
    # #1862 — the project's explicit ``persona_name`` wins over the
    # architect-role default. SamBlog configures ``persona_name = "Sage"``
    # but also registers an ``architect-samblog`` session; the previous
    # ordering returned the architect's default ("Archie") before the
    # project's own configuration was consulted, so PM Chat surfaced the
    # wrong name. Resolve project config first, then fall back to the
    # role default for projects that never picked a persona.
    persona = getattr(project, "persona_name", None)
    if isinstance(persona, str) and persona.strip():
        return persona.strip()

    sessions = getattr(config, "sessions", {}) or {}
    session_role: object | None = None
    if isinstance(sessions, dict):
        from pollypm.models import CONTROL_ROLES

        for session in sessions.values():
            if getattr(session, "project", None) != project_key:
                continue
            if getattr(session, "enabled", True) is False:
                continue
            role = getattr(session, "role", "")
            if role in CONTROL_ROLES:
                continue
            session_role = role
            break

    if isinstance(session_role, str) and session_role.strip():
        try:
            from pollypm.role_contract import canonical_role, persona_for

            if canonical_role(session_role) == "architect":
                return persona_for("architect")
        except ValueError:
            pass

    return None


def _project_pm_label(config: object, project_key: str, project: object) -> str:
    """Return the topbar PM label, e.g. ``"PM: Archie"``.

    When the project has no persona configured we return ``""`` instead
    of the placeholder ``"PM: Project PM"``; the caller is expected to
    skip rendering an empty PM meta. #1542 — ``media`` rendered
    ``PM: Project PM`` while every other project showed a real PM
    name; the placeholder leaked into the UI.
    """
    persona = _project_pm_persona(config, project_key, project)
    return f"PM: {persona}" if persona else ""


def _resolve_pm_target(config_path: Path, project_key: str | None) -> tuple[str, str]:
    """Resolve the cockpit-router key + display name for a project's PM.

    * Project has a ``persona_name`` configured → dispatch to its PM Chat
      window (``project:<key>:session``) and surface the persona name.
    * Project exists without a persona → dispatch to its PM Chat and
      surface a neutral project-PM label.
    * Empty or absent project keys still fall back to Polly's workspace
      operator session.
    """
    fallback_key = "polly"
    fallback_name = "Polly"
    if not project_key:
        return fallback_key, fallback_name
    try:
        config = load_config(config_path)
    except Exception:  # noqa: BLE001 — config errors shouldn't crash the TUI
        return fallback_key, fallback_name
    projects = getattr(config, "projects", {}) or {}
    project = projects.get(project_key)
    if project is None:
        return fallback_key, fallback_name
    persona = _project_pm_persona(config, project_key, project)
    return f"project:{project_key}:session", persona or "Project PM"


def _build_pm_context_line(
    task, *, item: dict | None = None, max_title: int = 64,
) -> str:
    """Compose the contextual first-line Sam sees in the PM input.

    Shape matches the spec: ``re: inbox/<task_number> "<title>"``. When
    ``item`` is provided (a rollup sub-item), include the sub-item
    subject so the PM knows which constituent task Sam wants to discuss.
    """
    task_id = getattr(task, "task_id", None)
    if not task_id:
        # Derive from project + number if the helper was passed a fresh Task.
        project = getattr(task, "project", "")
        number = getattr(task, "task_number", "")
        task_id = f"{project}/{number}" if project and number else "inbox/?"
    if item is not None:
        title = (item.get("subject") or task.title or "").strip()
    else:
        title = (getattr(task, "title", "") or "").strip()
    if len(title) > max_title:
        title = title[: max_title - 1] + "…"
    # Strip embedded quotes so the shell/tmux literal doesn't break.
    title = title.replace('"', "'")
    return f're: inbox/{task_id} "{title}"'


def _extract_plan_review_meta(labels: list[str] | None) -> dict:
    """Parse plan_review sidecar labels into a structured dict.

    The architect emits a plan_review item with labels that encode the
    plan task id, the explainer HTML path, and the fast-track flag:

        plan_review
        project:<key>
        plan_task:<project/number>
        explainer:<abs path to plan-review.html>
        fast_track             (optional; present only for fast-track)

    Returns ``{plan_task_id, explainer_path, fast_track, project}``;
    keys are present only when the source label was present.
    """
    meta: dict[str, object] = {"fast_track": False}
    for raw in labels or []:
        if not isinstance(raw, str):
            continue
        label = raw.strip()
        if label == "fast_track":
            meta["fast_track"] = True
            continue
        if label.startswith("plan_task:"):
            meta["plan_task_id"] = label[len("plan_task:"):].strip()
        elif label.startswith("explainer:"):
            path_str = label[len("explainer:"):].strip()
            if path_str:
                meta["explainer_path"] = path_str
        elif label.startswith("project:"):
            meta["project"] = label[len("project:"):].strip()
    return meta


def _plan_content_for_review(
    plan_review_meta: dict,
    config_path: object | None,
) -> str | None:
    """Load the plan markdown content referenced by a plan_review item.

    Resolves the project from the meta, walks ``CANONICAL_PLAN_RELATIVE_PATHS``
    for the project's on-disk path, and returns the file's text. Returns
    ``None`` when the plan can't be located or read — callers fall back
    to whatever they had before this lookup. Issue #1397: the cockpit
    surfaces the plan inline instead of pointing at a file path the
    user can't reach from a TUI.
    """
    # Lazy import — ``_dashboard_plan_path`` still lives in the unsplit
    # cockpit shell. Importing at module load time would create a cycle
    # (cockpit_ui re-exports from this module).
    from pollypm.cockpit_ui import _dashboard_plan_path

    if not isinstance(plan_review_meta, dict):
        return None
    project_key = str(plan_review_meta.get("project") or "").strip()
    if not project_key:
        plan_task_id = str(plan_review_meta.get("plan_task_id") or "")
        if "/" in plan_task_id:
            project_key = plan_task_id.split("/", 1)[0].strip()
    if not project_key or config_path is None:
        return None
    try:
        cfg = load_config(Path(config_path) if not isinstance(config_path, Path) else config_path)
    except Exception:  # noqa: BLE001
        return None
    project = getattr(cfg, "projects", {}).get(project_key)
    if project is None:
        return None
    project_path = getattr(project, "path", None)
    if not isinstance(project_path, Path):
        try:
            project_path = Path(str(project_path))
        except Exception:  # noqa: BLE001
            return None
    if not project_path.exists():
        return None
    plan_path = _dashboard_plan_path(project_path)
    if plan_path is None:
        return None
    try:
        return plan_path.read_text(encoding="utf-8")
    except OSError:
        return None


def _extract_plan_summary_block(plan_text: str) -> str:
    """Pull the leading summary paragraph from a plan markdown body.

    Heuristic: the architect's ``plan_review`` synthesis (PR #1408)
    leads with a ``## Summary`` section; pre-#1408 plans lead with the
    first paragraph after the H1. Both shapes resolve here. Returns an
    empty string when nothing summary-shaped is present.
    """
    text = (plan_text or "").strip()
    if not text:
        return ""
    lines = text.splitlines()
    # Look for a ``## Summary`` (or ``# Summary``) header and grab the
    # paragraph that follows it.
    for idx, line in enumerate(lines):
        stripped = line.strip().lower()
        if stripped in {"## summary", "# summary", "### summary"}:
            collected: list[str] = []
            for follow in lines[idx + 1:]:
                if follow.strip().startswith("#"):
                    break
                if not follow.strip():
                    if collected:
                        break
                    continue
                collected.append(follow.strip())
            if collected:
                return " ".join(collected)
    # Fall back to the first non-header paragraph.
    collected = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if collected:
                break
            continue
        if stripped.startswith("#"):
            if collected:
                break
            continue
        collected.append(stripped)
    return " ".join(collected)


def _extract_plan_judgment_calls(plan_text: str, *, limit: int = 5) -> list[str]:
    """Extract the bullet list under a ``## Judgment calls`` header.

    The PR #1408 ``plan_review`` synthesis encodes the architect's
    flagged points as a bulleted list under ``## Judgment calls``
    (case-insensitive). When that section is absent, returns an empty
    list — callers should treat that as "no flagged points" rather
    than synthesising a fake one.
    """
    text = plan_text or ""
    if not text.strip():
        return []
    lines = text.splitlines()
    target_headers = {"## judgment calls", "## judgement calls", "### judgment calls"}
    out: list[str] = []
    capturing = False
    for line in lines:
        stripped = line.strip()
        if stripped.lower() in target_headers:
            capturing = True
            continue
        if not capturing:
            continue
        if stripped.startswith("#"):
            break
        match = _ACTION_STEP_RE.match(line)
        if match is not None:
            point = _re.sub(r"\s+", " ", match.group("step")).strip()
            if point:
                out.append(point)
                if len(out) >= limit:
                    break
    return out


def _extract_blocking_question_meta(labels: list[str] | None) -> dict:
    """Parse ``blocking_question`` sidecar labels into a structured dict.

    The drift sweep emits a blocking_question item with labels that
    encode the blocked task id, the worker session doing the asking,
    and the project key:

        blocking_question
        project:<key>
        task:<project/number>
        blocking_worker:<session_name>

    Returns ``{task_id, blocking_worker, project}``; keys are
    present only when the corresponding label was present.
    """
    meta: dict[str, object] = {}
    for raw in labels or []:
        if not isinstance(raw, str):
            continue
        label = raw.strip()
        if label.startswith("task:"):
            meta["task_id"] = label[len("task:"):].strip()
        elif label.startswith("blocking_worker:"):
            meta["blocking_worker"] = label[
                len("blocking_worker:"):
            ].strip()
        elif label.startswith("project:"):
            meta["project"] = label[len("project:"):].strip()
    return meta


def _plan_review_has_round_trip(
    replies, *, requester: str = "user",
) -> bool:
    """True if the thread shows both the reviewer and the PM have spoken.

    "Round-trip" means at least one reply entry from the reviewer
    (the requester role — normally ``user``, or ``polly`` for
    fast-tracked items) AND at least one reply entry from the PM side
    (architect / polly / project persona — any non-reviewer actor).

    The gate is intentionally lenient: we don't care about ordering,
    we just need evidence of a conversation before Accept unlocks.
    """
    reviewer = (requester or "user").strip().lower() or "user"
    saw_reviewer = False
    saw_other = False
    for entry in replies or []:
        actor = (getattr(entry, "actor", "") or "").strip().lower()
        if not actor:
            continue
        if actor == reviewer:
            saw_reviewer = True
        else:
            saw_other = True
        if saw_reviewer and saw_other:
            return True
    return False


def _build_plan_review_primer(
    *,
    project_key: str,
    plan_path: str,
    explainer_path: str,
    plan_task_id: str,
    reviewer_name: str = "Sam",
) -> str:
    """Build the PM input primer injected on ``d`` for a plan_review item.

    Distinct from the generic ``re: inbox/N ...`` shape — this primer
    hands the PM a short brief plus the canonical co-refinement job
    description so the conversation starts on-topic without Sam (or
    Polly, when fast-tracked) having to type the frame themselves.
    """
    person = reviewer_name.strip() or "Sam"
    pronoun_subject = "Sam" if person == "Sam" else person
    return (
        f"{pronoun_subject} has opened plan review for project: {project_key}.\n"
        f"Plan: {plan_path}\n"
        f"Explainer: {explainer_path}\n"
        "\n"
        "Your job in this conversation:\n"
        f"- Co-refine the plan with {pronoun_subject}\n"
        "- Push hard for decomposition into the smallest reasonable tasks\n"
        "- Each task should ship a small module with clean interfaces\n"
        "- Challenge large lumps: a 500-LoC module with 3 concerns should "
        "become 3 modules with 1 concern each\n"
        "- Surface cross-cutting risks that span modules — integration "
        "bugs live there\n"
        "- Propose rewrites of any decision that's load-bearing without "
        "clear justification\n"
        f"- If {pronoun_subject} pings without a specific concern, your "
        "default opener is to walk through the plan's riskiest decisions + "
        "decomposition and ask where to dig in — don't just wait for "
        "a question\n"
        "\n"
        f"When {pronoun_subject} signs off (says 'approved' or equivalent): "
        f"record approval for plan task {plan_task_id} as "
        f"{'user' if person == 'Sam' else 'polly'} through the plan-review "
        "approval flow.\n"
        "Don't create backlog tasks yourself — emit_backlog fires "
        "after approval.\n"
        "This small-tasks / small-modules bias matters because it's much "
        "more maintainable for agentic development."
    )


def _build_plan_review_denial_primer(
    *,
    project_key: str,
    cancelled_plan_task_id: str,
    successor_plan_task_id: str,
    denial_reason: str,
    reviewer_name: str = "Sam",
    plan_path: str = "",
) -> str:
    """Build the PM input primer injected after a plan-review deny (#1403).

    Distinct from :func:`_build_plan_review_primer` — instead of opening
    a co-refinement conversation, this primer:

    * Frames the conversation as "the user just rejected the plan,
      they're giving you context for why before the architect retries".
    * Includes the denial reason verbatim so the PM persona has the
      same "address these concerns" frame the architect sees on the
      successor plan task's context.
    * Identifies the cancelled / successor task pair so the persona
      can reference them when chatting.

    Output ends WITHOUT a trailing newline so the PM CLI can append
    the user's typed follow-up directly.
    """
    person = (reviewer_name or "Sam").strip() or "Sam"
    pronoun_subject = "Sam" if person == "Sam" else person
    project_label = project_key or "(unknown)"
    plan_line = f"Denied plan body: {plan_path}\n" if plan_path else ""
    return (
        f"{pronoun_subject} just denied plan task {cancelled_plan_task_id} "
        f"for project: {project_label}.\n"
        f"{plan_line}"
        f"Successor plan task: {successor_plan_task_id} "
        "(architect will run a fresh planning pass).\n"
        "\n"
        f"Reason {pronoun_subject} gave for the denial:\n"
        f"{denial_reason}\n"
        "\n"
        "Your job in this conversation:\n"
        f"- Sit with {pronoun_subject} on the concerns above before the "
        "architect's replan kicks off; tease out anything the one-line "
        "reason left implicit\n"
        "- Push for concrete decomposition or scoping changes the "
        "architect should bake into the next plan\n"
        f"- When {pronoun_subject} signals 'ok, replan with that' "
        "(or equivalent), summarise the brief and let the architect "
        f"pick up successor task {successor_plan_task_id} — don't try "
        "to write the plan yourself\n"
        "- The denial reason is already attached to the successor task "
        "as a ``plan_review_denied`` context entry, so the architect "
        "will see it without further plumbing"
    )


def _extract_proposal_spec(task, *, labels: list[str] | None = None) -> dict:
    """Recover a proposal's ``proposed_task_spec`` from an inbox row.

    The body was rendered at emit time by ``render_proposal_body`` which
    intersperses the rationale with a ``## Proposed task`` markdown
    block. Rather than parse that back, we fall back to title +
    description: the accepted follow-on task uses the proposal title
    and rationale as its description. Tests that care about the exact
    spec shape can stub :meth:`PollyInboxApp._proposal_specs` directly.
    """
    spec: dict[str, object] = {}
    subject = (getattr(task, "title", "") or "").strip()
    if subject:
        spec["title"] = subject
    body = (getattr(task, "description", "") or "").strip()
    # Split at the preview marker so the accepted follow-on task only
    # carries the rationale, not the spec scaffold.
    marker = "## Proposed task"
    if marker in body:
        rationale, _, tail = body.partition(marker)
        spec["description"] = rationale.strip()
        # Recover acceptance criteria from the preview, when present.
        for line in tail.splitlines():
            stripped = line.strip()
            if stripped.startswith("- **acceptance criteria**"):
                # Subsequent indented lines form the AC block.
                continue
        # Heuristic AC extractor: grab the block after ``acceptance criteria:``.
        ac_lines: list[str] = []
        capturing = False
        for line in tail.splitlines():
            low = line.lstrip().lower()
            if low.startswith("- **acceptance criteria**"):
                capturing = True
                continue
            if capturing:
                if line.startswith("- **") and not line.lstrip().startswith(
                    "- **acceptance"
                ):
                    break
                if line.strip():
                    ac_lines.append(line.strip())
        if ac_lines:
            spec["acceptance_criteria"] = "\n".join(ac_lines)
    else:
        spec["description"] = body
    return spec


# Inbox lens taxonomy (#1573). The default lens is ``awaits-you`` —
# the same curated set the dashboard "Waiting on you" section shows
# (rail badge count == default-view count == dashboard section length;
# pinned by ``tests/test_inbox_default_lens.py``). The remaining
# lenses are archive views over the historical data, scoped by
# :class:`InboxItemKind`. Order here drives the cycle order on ``L``
# and the digit mapping for direct ``1``/``2``/``…`` selection.
_INBOX_LENSES: tuple[tuple[str, str, str], ...] = (
    # (slug, label, empty-state copy)
    (
        "awaits-you",
        "Awaiting you",
        "Nothing awaiting your action. Check the operator dashboard "
        "for working/idle projects.",
    ),
    (
        "all",
        "All messages",
        "Inbox is empty.",
    ),
    (
        "completion-fyi",
        "Completion FYI",
        "No completion notifications.",
    ),
    (
        "activity-events",
        "Activity events",
        "No activity events recorded.",
    ),
    (
        "self-bug-reports",
        "Self bug reports",
        "No self-reported bugs. (Use `pm bug-report` to file one.)",
    ),
    (
        "legacy",
        "Legacy (unclassified)",
        "All legacy rows have been classified. (Or backfill hasn't run "
        "yet — try `pm inbox backfill-kinds --dry-run`.)",
    ),
)

# Slug -> :class:`InboxItemKind` for the kind-scoped lenses. ``awaits-you``
# and ``all`` are handled separately (predicate vs. no-filter); the rest
# are direct kind matches.
_INBOX_LENS_KINDS: dict[str, str] = {
    "completion-fyi": "completion_fyi",
    "activity-events": "activity_event",
    "self-bug-reports": "self_bug_report",
    "legacy": "legacy",
}

_INBOX_DEFAULT_LENS_SLUG = "awaits-you"
