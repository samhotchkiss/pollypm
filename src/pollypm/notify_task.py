"""Identify notification-shaped tasks (#1003, #1013, #1020).

Contract:
- Inputs: a work-service task row (or anything exposing ``labels`` /
  ``roles`` / ``title``).
- Outputs: a boolean classification — does this task represent a pure
  inbox notification (architect/operator notify, plan_review handoff,
  rejection feedback, supervisor/heartbeat alert, …) rather than a
  real work item that should appear in the cockpit Tasks pane and
  ``pm task list``?
- Side effects: none.
- Invariants:
    - ``pm notify --priority immediate`` materialises a ``chat``-flow
      task carrying the ``notify`` label and a ``notify_message:<id>``
      sidecar so the cockpit inbox can render it as a structured row
      with the architect's user_prompt payload (see
      ``cli_features/session_runtime.py::notify``).
    - The reviewer's "Rejected …" feedback writer (and any other
      writer that addresses the user directly) sets
      ``roles.operator = "user"`` to mean "this row is for the human
      to read; no worker should pick it up". That structural signal
      is now the primary discriminator.
    - Those rows must NOT show up in the Tasks view — they're pure
      announcements with no node-level transition affordance, so the
      user sees a ``draft`` row that ``A``/``X``/``Q`` won't act on.
    - Real work tasks have ``roles.operator`` set to a worker-shaped
      role (``worker``, ``architect``, ``reviewer``, …) or no operator
      at all. The filter is intentionally conservative: when in doubt
      it leaves a task visible (false negatives in the Tasks view are
      far better than false positives that hide real work).
"""

from __future__ import annotations


NOTIFY_LABEL = "notify"


# Title prefixes used by historical notification writers. Kept as a
# defence-in-depth backup for tasks that pre-date the structural
# ``roles.operator == "user"`` convention or that slipped through with
# malformed roles (eg. roles serialised as a JSON string the loader
# couldn't parse). The structural check is the primary discriminator;
# this list catches the long tail.
_NOTIFY_TITLE_PREFIXES: tuple[str, ...] = (
    "Plan ready for review:",
    "Rejected ",
    "Done:",
    "Project: ",
    "Queued:",
    "Heartbeat:",
    "URGENT:",
    "Nth ",
    "Repeated stale",
)


def _coerce_roles(value: object) -> dict[str, str]:
    """Best-effort coerce ``task.roles`` into a ``{str: str}`` mapping.

    The work-service hydrator returns a dict, but defensive code here
    keeps the predicate robust against partial / corrupt rows (a row
    where ``roles`` came back as a JSON string, ``None``, etc.). The
    fallback is "no roles known" — which downgrades to title-prefix
    matching rather than crashing on a bad row.
    """
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    if isinstance(value, str) and value:
        try:
            import json as _json

            parsed = _json.loads(value)
        except ValueError:
            return {}
        if isinstance(parsed, dict):
            return {str(k): str(v) for k, v in parsed.items()}
    return {}


def is_notify_inbox_task(task) -> bool:
    """True when ``task`` is a notification-shaped row, not real work.

    Three signals, in order:

    1. **Structural** (#1020): ``roles.operator == "user"``. The task
       is addressed to the human — every writer that does this means
       "this is a notification, not a worker assignment". Rejection
       feedback, plan-review handoff, supervisor alerts all set this.
    2. **Label** (#1003, #1013): the ``notify`` label is set by
       ``pm notify --priority immediate`` and the architect's
       plan_review handoff. Backstop for callers that emit a notify
       without a roles map.
    3. **Title prefix** (#1020): historical notification headers
       (``Rejected …``, ``Done:``, ``Plan ready for review:``, …).
       Belt-and-braces for legacy rows that pre-date the structural
       convention.

    Any single signal is sufficient. The predicate is intentionally
    OR-shaped — false positives in the Tasks view are far worse than
    false negatives, but every signal here is a deliberate
    "addressed to the user" marker, so OR is safe.
    """
    roles = _coerce_roles(getattr(task, "roles", None))
    if roles.get("operator") == "user":
        return True

    labels = list(getattr(task, "labels", []) or [])
    if NOTIFY_LABEL in labels:
        return True

    title = str(getattr(task, "title", "") or "").strip()
    if title:
        for prefix in _NOTIFY_TITLE_PREFIXES:
            if title.startswith(prefix):
                return True

    return False


def is_notify_only_inbox_entry(item) -> bool:
    """True when ``item`` is a notify-only (FYI) inbox entry, not actionable.

    Used by ``pm inbox`` (CLI) and the cockpit Inbox panel (#1027) to
    default-hide the bulk of pure-FYI notifications — completion
    announcements ("Done: …"), heartbeat alerts ("Repeated stale review
    ping"), debug-style drafts — so the single actionable item the user
    needs to act on doesn't get buried.

    Treated as notify-only:

    * **Message rows** (``source == "message"``) with ``message_type ==
      "notify"``. The unified messages-store ``notify`` type is the
      canonical FYI surface; the alert/inbox_task types are kept visible.
    * **Task rows** that satisfy :func:`is_notify_inbox_task` — the
      legacy notify-stub predicate (label-based, role-based, or title
      prefix). These are announcement stubs the cockpit inbox surfaces
      via its specialised actions but the CLI listing has nothing to do
      with.

    Anything else — work-service tasks, ``alert``-type messages,
    ``inbox_task``-type messages — is treated as actionable and stays
    visible by default.
    """
    source = getattr(item, "source", "task")
    if source == "message":
        message_type = (getattr(item, "message_type", None) or "").lower()
        # Only the bare ``notify`` type is hidden by default; alerts and
        # inbox_tasks remain visible because they carry the actionable
        # supervisor / user-prompt payloads.
        return message_type == "notify"
    return is_notify_inbox_task(item)


__all__ = [
    "NOTIFY_LABEL",
    "is_notify_inbox_task",
    "is_notify_only_inbox_entry",
]
