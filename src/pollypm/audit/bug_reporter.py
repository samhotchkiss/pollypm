"""File PollyPM self-bug-reports as GitHub issues instead of inbox rows (#1569).

When Polly, the heartbeat, or the audit watchdog notices a system bug
in PollyPM itself (a misrouted review ping, a stale alert, a confusing
CLI output, etc.), the old flow filed the observation as an inbox
message via ``pm notify``. Those rows sat alongside the user's
actionable to-do list, accumulated forever, and forced the user to
triage system-meta-bugs out of their real work.

This helper provides a single seam for routing those observations to
GitHub issues instead. Producers call :func:`file_bug_report` with a
title + body; the helper:

* Looks for an open issue with the same title on the configured
  ``polly-self-report`` label within ``dedup_window_seconds``.
* If found, returns the existing issue number (no new issue created).
* Otherwise invokes ``gh issue create`` and returns the new number.
* Emits an audit event (:data:`EVENT_BUG_REPORT_FILED`) regardless of
  whether ``gh`` succeeded — the observation itself is forensically
  preserved even when the issue tracker is unreachable.

Best-effort: failures (no ``gh`` installed, network down, label
missing, repo unreachable) log a warning and return ``None``. The
helper never raises — producers can swap an inbox write for a
``file_bug_report`` call without adding any new try/except plumbing.

Leaf-module discipline (#1569):

* No imports from cockpit/TUI/Supervisor.
* No dependency on the inbox schema work (#1565).
* stdlib + ``pollypm.audit.log`` only.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from pollypm.audit.log import emit as _audit_emit

logger = logging.getLogger(__name__)

# Label applied to every GitHub issue the helper opens. Created via
# ``gh label create polly-self-report ...`` (see the issue body of
# #1569 for the canonical command). If the label is missing on the
# target repo, ``gh issue create`` will reject the call and the helper
# returns ``None`` — operator action is "create the label", not
# "fall back to the inbox".
SELF_REPORT_LABEL = "polly-self-report"

# Default dedup window. One hour matches the heartbeat-tick scale that
# produces most self-bug observations: a watchdog rule that fires twice
# in five minutes should collapse to one issue, but a recurrence the
# next day is worth a fresh report (rate is itself signal).
DEFAULT_DEDUP_WINDOW_SECONDS = 60 * 60

# Audit event names. Kept in this module so the audit/log.py event
# enum doesn't grow whenever the bug reporter learns a new state.
EVENT_BUG_REPORT_FILED = "bug_report.filed"
EVENT_BUG_REPORT_DEDUPED = "bug_report.deduped"
EVENT_BUG_REPORT_FAILED = "bug_report.failed"

# Override hook for tests: set ``$POLLYPM_BUG_REPORTER_DISABLED=1`` to
# short-circuit every call (returns ``None``, no subprocess). The
# default heartbeat / audit code never sets this; it exists so the
# test suite can prove a producer was called without spawning a real
# ``gh`` subprocess.
_DISABLE_ENV = "POLLYPM_BUG_REPORTER_DISABLED"

# Override hook for tests: set ``$POLLYPM_BUG_REPORTER_REPO=owner/repo``
# to pin the target repo regardless of git working directory. Without
# this, ``gh`` resolves the repo from the current directory's git
# remote, which is fine in production but flaky in tests.
_REPO_ENV = "POLLYPM_BUG_REPORTER_REPO"


@dataclass(slots=True, frozen=True)
class BugReportResult:
    """Outcome of a :func:`file_bug_report` call.

    Returned to callers that want to distinguish "I filed a new issue"
    from "I matched an existing one" — useful for log lines and tests.
    The simpler :func:`file_bug_report` returns just the issue number
    so existing call sites stay one-line.
    """

    issue_number: int
    created: bool
    title: str


def file_bug_report(
    *,
    title: str,
    body: str,
    actor: str = "polly",
    project: str = "",
    subject: str = "",
    dedup_window_seconds: int = DEFAULT_DEDUP_WINDOW_SECONDS,
    extra_labels: tuple[str, ...] = (),
) -> int | None:
    """File a PollyPM self-bug-report as a GitHub issue.

    Args:
        title: Issue title. The dedup query matches on exact title
            within the window, so callers should produce stable
            phrasing (no embedded timestamps, no rotating counters).
        body: Issue body. May include markdown — passed verbatim to
            ``gh issue create --body``.
        actor: Who is filing the report. Recorded in the audit event
            and prepended to the body as a footer so the issue carries
            the producer identity. Defaults to ``"polly"``.
        project: PollyPM project key the bug was observed against, or
            empty string for cross-project / workspace-level bugs.
            Recorded in audit metadata and added as a project footer.
        subject: Free-form forensic subject (task id, session name,
            etc.). Recorded in audit metadata only — not folded into
            the issue body, since titles already carry enough context.
        dedup_window_seconds: Suppress duplicate issues with the same
            title filed within this window. Default 1 hour. Set to 0
            to disable deduplication (every call opens a fresh issue).
        extra_labels: Additional labels to attach beyond
            :data:`SELF_REPORT_LABEL`. The label must already exist
            on the target repo — ``gh`` rejects unknown labels.

    Returns:
        Issue number on success (newly created OR matched existing).
        ``None`` on failure (``gh`` missing, network down, label
        missing, etc.). Callers MUST treat ``None`` as "issue not
        filed" but the original observation is still recorded in the
        audit log so forensic readers can correlate.
    """
    result = file_bug_report_detailed(
        title=title,
        body=body,
        actor=actor,
        project=project,
        subject=subject,
        dedup_window_seconds=dedup_window_seconds,
        extra_labels=extra_labels,
    )
    return result.issue_number if result is not None else None


def file_bug_report_detailed(
    *,
    title: str,
    body: str,
    actor: str = "polly",
    project: str = "",
    subject: str = "",
    dedup_window_seconds: int = DEFAULT_DEDUP_WINDOW_SECONDS,
    extra_labels: tuple[str, ...] = (),
) -> BugReportResult | None:
    """Variant of :func:`file_bug_report` that returns the full outcome.

    Same semantics + arguments; returns :class:`BugReportResult` so
    callers can distinguish ``created`` vs deduped without re-querying
    GitHub.
    """
    clean_title = (title or "").strip()
    if not clean_title:
        logger.warning("bug_reporter: refused empty title (actor=%s)", actor)
        return None

    if os.environ.get(_DISABLE_ENV):
        # Test hook: producer was called, but don't shell out. Still
        # emit the audit event so test cases can assert the side
        # effect happened.
        _emit_audit(
            event=EVENT_BUG_REPORT_FAILED,
            project=project,
            subject=subject,
            actor=actor,
            metadata={
                "title": clean_title,
                "error": "disabled_by_env",
            },
        )
        return None

    if not _gh_available():
        logger.warning(
            "bug_reporter: gh CLI unavailable, dropping bug report %r",
            clean_title,
        )
        _emit_audit(
            event=EVENT_BUG_REPORT_FAILED,
            project=project,
            subject=subject,
            actor=actor,
            metadata={
                "title": clean_title,
                "error": "gh_unavailable",
            },
        )
        return None

    if dedup_window_seconds > 0:
        existing = _find_recent_open_issue(
            title=clean_title,
            window_seconds=dedup_window_seconds,
        )
        if existing is not None:
            _emit_audit(
                event=EVENT_BUG_REPORT_DEDUPED,
                project=project,
                subject=subject,
                actor=actor,
                metadata={
                    "title": clean_title,
                    "issue_number": existing,
                    "window_seconds": dedup_window_seconds,
                },
            )
            return BugReportResult(
                issue_number=existing,
                created=False,
                title=clean_title,
            )

    rendered_body = _render_body(
        body=body or "",
        actor=actor,
        project=project,
        subject=subject,
    )
    issue_number = _create_issue(
        title=clean_title,
        body=rendered_body,
        labels=(SELF_REPORT_LABEL, *extra_labels),
    )
    if issue_number is None:
        _emit_audit(
            event=EVENT_BUG_REPORT_FAILED,
            project=project,
            subject=subject,
            actor=actor,
            metadata={
                "title": clean_title,
                "error": "gh_create_failed",
            },
        )
        return None

    _emit_audit(
        event=EVENT_BUG_REPORT_FILED,
        project=project,
        subject=subject,
        actor=actor,
        metadata={
            "title": clean_title,
            "issue_number": issue_number,
        },
    )
    return BugReportResult(
        issue_number=issue_number,
        created=True,
        title=clean_title,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _gh_available() -> bool:
    """Return True iff the ``gh`` CLI is on PATH."""
    return shutil.which("gh") is not None


def _repo_args() -> list[str]:
    """Return ``["--repo", "<env>"]`` when the override env is set."""
    repo = os.environ.get(_REPO_ENV, "").strip()
    if not repo:
        return []
    return ["--repo", repo]


def _find_recent_open_issue(
    *,
    title: str,
    window_seconds: int,
) -> int | None:
    """Return the newest open issue number with the same title in window.

    Queries ``gh issue list --label polly-self-report --state open
    --search "<title> in:title" --json number,title,createdAt``. The
    label scope keeps the search tight; the title check rules out
    partial matches that ``in:title`` would otherwise allow.

    Returns ``None`` on any failure (network down, gh missing, parse
    error) so the caller falls through to creating a fresh issue —
    duplicates are a smaller bug than dropped observations.
    """
    cmd = [
        "gh", "issue", "list",
        "--label", SELF_REPORT_LABEL,
        "--state", "open",
        "--search", f"{title} in:title",
        "--limit", "20",
        "--json", "number,title,createdAt",
        *_repo_args(),
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.debug("bug_reporter: gh issue list raised: %s", exc)
        return None
    if proc.returncode != 0:
        logger.debug(
            "bug_reporter: gh issue list returned %d: %s",
            proc.returncode, proc.stderr.strip(),
        )
        return None
    try:
        rows = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        logger.debug("bug_reporter: gh issue list output not JSON: %s", exc)
        return None
    if not isinstance(rows, list):
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
    best: tuple[datetime, int] | None = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_title = str(row.get("title") or "")
        if row_title.strip() != title:
            continue
        created_raw = str(row.get("createdAt") or "")
        created = _parse_iso(created_raw)
        if created is None:
            continue
        if created < cutoff:
            continue
        number = row.get("number")
        if not isinstance(number, int):
            continue
        if best is None or created > best[0]:
            best = (created, number)
    return best[1] if best is not None else None


def _parse_iso(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp from ``gh``. ``Z`` suffix is allowed."""
    if not value:
        return None
    try:
        # gh emits ``2026-05-17T01:02:03Z``; ``fromisoformat`` only
        # learned ``Z`` in 3.11 — strip explicitly for older runs.
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _create_issue(
    *,
    title: str,
    body: str,
    labels: tuple[str, ...],
) -> int | None:
    """Invoke ``gh issue create`` and parse the issue number from output."""
    cmd: list[str] = ["gh", "issue", "create", "--title", title, "--body", body]
    for label in labels:
        cmd.extend(["--label", label])
    cmd.extend(_repo_args())
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.warning("bug_reporter: gh issue create raised: %s", exc)
        return None
    if proc.returncode != 0:
        logger.warning(
            "bug_reporter: gh issue create returned %d: %s",
            proc.returncode, proc.stderr.strip(),
        )
        return None
    return _parse_issue_number(proc.stdout)


def _parse_issue_number(stdout: str) -> int | None:
    """Extract the trailing ``/issues/<N>`` integer from ``gh`` output.

    ``gh issue create`` prints the URL of the new issue, e.g.
    ``https://github.com/owner/repo/issues/1572``. We don't import
    ``re`` for one match — split on the marker token directly.
    """
    if not stdout:
        return None
    url = stdout.strip().splitlines()[-1].strip()
    marker = "/issues/"
    idx = url.rfind(marker)
    if idx < 0:
        return None
    tail = url[idx + len(marker):]
    # Trim any trailing query string / fragment.
    for sep in ("?", "#"):
        if sep in tail:
            tail = tail.split(sep, 1)[0]
    try:
        return int(tail)
    except ValueError:
        return None


def _render_body(
    *,
    body: str,
    actor: str,
    project: str,
    subject: str,
) -> str:
    """Append a small forensic footer to the user-supplied body."""
    parts = [body.rstrip(), ""]
    parts.append("---")
    parts.append("Filed automatically by PollyPM's bug_reporter.")
    parts.append(f"actor: {actor or 'unknown'}")
    if project:
        parts.append(f"project: {project}")
    if subject:
        parts.append(f"subject: {subject}")
    return "\n".join(parts).strip() + "\n"


def _emit_audit(
    *,
    event: str,
    project: str,
    subject: str,
    actor: str,
    metadata: dict,
) -> None:
    """Wrap ``audit.emit`` so a failed audit write never raises."""
    try:
        _audit_emit(
            event=event,
            project=project or "",
            subject=subject or "",
            actor=actor or "",
            metadata=metadata,
        )
    except Exception:  # noqa: BLE001 — audit must never break the reporter
        logger.debug("bug_reporter: audit emit failed", exc_info=True)


__all__ = [
    "BugReportResult",
    "DEFAULT_DEDUP_WINDOW_SECONDS",
    "EVENT_BUG_REPORT_DEDUPED",
    "EVENT_BUG_REPORT_FAILED",
    "EVENT_BUG_REPORT_FILED",
    "SELF_REPORT_LABEL",
    "file_bug_report",
    "file_bug_report_detailed",
]
