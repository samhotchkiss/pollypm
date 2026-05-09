"""Helpers for resolving the underlying task an alert references (#1545).

Background — alerts in PollyPM don't have a structured ``task_id``
column on the ``messages`` row. Different alert families encode the
referenced task in different places:

* ``stuck_on_task:<project>/<num>`` — task id in the ``alert_type``
  suffix.
* ``no_session_for_assignment:<project>/<num>`` — same shape.
* ``review-<project>-<num>`` — task number in the ``session_name``
  scope (the synthetic ``review_pending`` alert key from #1053).
* ``plan_missing`` (scope: ``plan_gate-<project>``) — the task is
  named in the alert message body
  (``... queued task <project>/<num> is waiting``).
* ``no_session`` — only the project key is encoded, in the
  ``<role>-<project>`` / ``<role>_<project>`` scope; no task id.

The supervisor's ``_sweep_stale_alerts`` (cycle #1545) extends its
clear policy to drop alerts whose underlying task has reached a
terminal status. To do that without coupling the sweep to every
alert family's keying convention, this module exposes a single
:func:`extract_task_ids` that returns the candidate task IDs for an
alert (zero, one, or several) — the sweep then asks the work-service
about each candidate's status and clears the alert when any candidate
is terminal.

Pure parsing — no I/O. Safe to call from the supervisor heartbeat path
without holding a DB handle.
"""

from __future__ import annotations

import re
from typing import Iterable

# Task id pattern: ``<project>/<num>`` where the project is a slug
# (alphanumeric, underscore, hyphen, dot are all allowed because the
# work-service stores both slugified keys and display names — see
# ``_project_storage_aliases``). The pattern matches the same shape
# the existing CLI / sweep code uses (``cli_features.session_runtime``,
# ``task_assignment_notify.handlers.sweep``).
_TASK_ID_RE = re.compile(r"\b([A-Za-z0-9_.-]+/\d+)\b")

# ``review-<project>-<num>`` scope used by ``_emit_review_pending_alert``
# (#1053). Pinned to a regex so a future scope-rename catches itself
# in tests.
_REVIEW_SCOPE_RE = re.compile(r"^review-(?P<project>.+)-(?P<num>\d+)$")

# Alert-type prefixes whose suffix carries a ``<project>/<num>`` task
# id. Mirrors the prefixes in
# ``cockpit_project_state.USER_ACTIONABLE_TASK_ALERT_PREFIXES`` plus
# the ephemeral ``stuck_on_task:`` family.
_TYPE_PREFIXES_WITH_TASK_ID: tuple[str, ...] = (
    "stuck_on_task:",
    "no_session_for_assignment:",
)

# Alert types whose underlying task reference must come from the
# message body rather than the structured fields. ``plan_missing`` is
# the witness in #1545 — the alert is keyed on a synthetic
# ``plan_gate-<project>`` scope and its message names the queued
# blocked task ("queued task <project>/<num> is waiting").
_TYPES_WITH_MESSAGE_BODY_TASK_ID: frozenset[str] = frozenset({
    "plan_missing",
})


def extract_task_ids(
    *,
    alert_type: str,
    session_name: str,
    message: str | None = None,
) -> list[str]:
    """Return the candidate task IDs an alert refers to.

    Walks the four reference shapes in order — ``alert_type`` suffix,
    ``session_name`` review scope, message body for plan_missing — and
    returns a deduped list preserving first-seen order. Empty when
    none of the shapes match.

    Callers are expected to look each candidate up in the work-service
    and short-circuit on the first terminal hit (see
    ``Supervisor._sweep_stale_alerts``).
    """
    seen: set[str] = set()
    out: list[str] = []

    def _push(candidate: str) -> None:
        candidate = (candidate or "").strip()
        if not candidate or "/" not in candidate:
            return
        # Defensive normalization: strip surrounding punctuation that
        # sometimes wraps the ID in alert message bodies (e.g. trailing
        # ``.`` or ``,``).
        candidate = candidate.rstrip(".,;:!?)\"' ").lstrip("(\"' ")
        if not candidate:
            return
        if candidate in seen:
            return
        seen.add(candidate)
        out.append(candidate)

    alert_type = (alert_type or "").strip()
    session_name = (session_name or "").strip()

    # 1. ``alert_type`` suffix: ``stuck_on_task:<project>/<num>`` etc.
    for prefix in _TYPE_PREFIXES_WITH_TASK_ID:
        if alert_type.startswith(prefix):
            tail = alert_type[len(prefix):]
            for match in _TASK_ID_RE.finditer(tail):
                _push(match.group(1))

    # 2. ``session_name`` review scope: ``review-<project>-<num>``.
    review_match = _REVIEW_SCOPE_RE.match(session_name)
    if review_match is not None:
        project = review_match.group("project")
        num = review_match.group("num")
        if project and num:
            _push(f"{project}/{num}")

    # 3. Message body for known body-bearing alert types. Restricted
    # to a small set so we don't accidentally clear an alert on a task
    # mentioned only as context (e.g. a banner copy that names a
    # downstream task that isn't the actual subject).
    if alert_type in _TYPES_WITH_MESSAGE_BODY_TASK_ID and message:
        for match in _TASK_ID_RE.finditer(str(message)):
            _push(match.group(1))

    return out


def project_key_from_task_id(task_id: str) -> str:
    """Return the leading ``<project>`` segment of a ``<project>/<num>`` task id."""
    if not task_id or "/" not in task_id:
        return ""
    return task_id.split("/", 1)[0]


__all__ = [
    "extract_task_ids",
    "project_key_from_task_id",
]
