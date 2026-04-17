"""Inbox notification for downtime tasks reaching ``awaiting_approval``.

The work-service-backed inbox (``pollypm.work.inbox_view``) auto-selects
any task whose current node has ``actor_type == human``; downtime tasks
automatically appear. Spec §7 goes further: the downtime plugin must
**explicitly dispatch** a ``downtime_result`` notification with a
summary + artifact pointers before we block on the user. The
``inbox_notification_sent`` gate (dt02) enforces this — it fails unless
a marker context entry has been written.

This module is that dispatch path. It:

1. Renders a short markdown summary block from the handler's
   structured result.
2. Writes it as a context log entry prefixed ``inbox_notification_sent:``
   so the gate passes.
3. Also logs the artifact paths and (if present) branch + PR hints so
   ``pm inbox show <task>`` surfaces the affordances per spec §7.

Keeping the notification path a pure context-log write means we don't
need a new table, the inbox view picks up the entry through the
existing work-service surface, and the dt07 CLI can render the log
verbatim.
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any, Protocol

from pollypm.plugins_builtin.downtime.handlers.security_scan import (
    SecurityScanResult,
)
from pollypm.plugins_builtin.downtime.handlers.spec_feature import (
    SpecFeatureResult,
)
from pollypm.plugins_builtin.downtime.handlers.try_alt_approach import (
    TryAltApproachResult,
)


NOTIFICATION_MARKER = "inbox_notification_sent"
NOTIFICATION_KIND = "downtime_result"


class _ContextWriter(Protocol):
    """Minimal protocol — the work service's ``add_context`` surface."""

    def add_context(self, task_id: str, actor: str, text: str) -> Any:  # pragma: no cover - protocol
        ...


def render_notification(
    *,
    task_id: str,
    kind: str,
    result: Any,
) -> str:
    """Render the notification body.

    The first line **must** start with ``inbox_notification_sent:`` so
    the ``inbox_notification_sent`` gate matches. Everything after is
    free-form markdown the inbox view surfaces to the user.
    """
    data = _coerce_to_dict(result)
    summary = str(data.get("summary") or "(no summary)")

    lines = [
        f"{NOTIFICATION_MARKER}: kind={NOTIFICATION_KIND} task={task_id}",
        "",
        f"**Downtime exploration ready for review** — kind: `{kind}`",
        "",
        summary,
        "",
    ]

    artifacts = _artifact_lines(kind, data)
    if artifacts:
        lines.append("**Artifacts**")
        lines.append("")
        lines.extend(f"- {line}" for line in artifacts)
        lines.append("")

    lines.append("**Affordances**")
    lines.append("")
    lines.append(f"- `pm task approve {task_id}` — commit/merge per category")
    lines.append(f"- `pm task reject {task_id} --reason ...` — archive")
    lines.append(
        f"- `pm task comment {task_id} \"...\"` — leave a note without deciding"
    )
    return "\n".join(lines).rstrip() + "\n"


def _coerce_to_dict(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        return result
    if is_dataclass(result):
        return asdict(result)
    # Fall back to a shallow vars() — covers typed namespaces etc.
    try:
        return dict(vars(result))
    except TypeError:
        return {}


def _artifact_lines(kind: str, data: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    if kind == "spec_feature":
        if data.get("artifact_path"):
            lines.append(f"Spec draft: `{data['artifact_path']}`")
        if data.get("branch_name"):
            lines.append(f"Branch: `{data['branch_name']}`")
    elif kind == "build_speculative":
        if data.get("branch_name"):
            lines.append(f"Branch: `{data['branch_name']}`")
        if data.get("commit_sha"):
            lines.append(f"Commit: `{data['commit_sha'][:12]}`")
        tests_added = data.get("tests_added")
        if isinstance(tests_added, int):
            lines.append(
                f"Tests added: {tests_added} — {'passing' if data.get('tests_pass') else 'failing'}"
            )
    elif kind == "audit_docs":
        if data.get("branch_name"):
            lines.append(f"Branch: `{data['branch_name']}`")
        if data.get("pr_url"):
            lines.append(f"PR: {data['pr_url']}")
        elif data.get("pr_title"):
            lines.append(f"Draft PR title: {data['pr_title']}")
    elif kind == "security_scan":
        if data.get("report_path"):
            lines.append(f"Report: `{data['report_path']}`")
        if data.get("severity"):
            lines.append(f"Severity: {data['severity']}")
        finding_count = data.get("finding_count")
        if isinstance(finding_count, int):
            lines.append(f"Findings: {finding_count}")
    elif kind == "try_alt_approach":
        if data.get("branch_name"):
            lines.append(f"Branch: `{data['branch_name']}`")
        if data.get("comparison_path"):
            lines.append(f"Comparison: `{data['comparison_path']}`")
        if data.get("verdict"):
            lines.append(f"Verdict: {data['verdict']}")
    return lines


def dispatch_notification(
    *,
    service: _ContextWriter,
    task_id: str,
    kind: str,
    result: Any,
    actor: str = "downtime",
) -> str:
    """Write the notification context entry. Returns the rendered body.

    The caller is expected to have already stored the artifact(s) on
    disk — this function doesn't create files. It only ensures the
    inbox has a machine-readable record that makes ``pm inbox show``
    useful and unlocks the ``inbox_notification_sent`` gate.
    """
    body = render_notification(task_id=task_id, kind=kind, result=result)
    service.add_context(task_id, actor, body)
    return body
