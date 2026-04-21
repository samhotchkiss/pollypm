"""Recent git commit timeline for the per-project dashboard (#513)."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from pollypm.cockpit_sections.base import (
    _DASHBOARD_BULLET,
    _dashboard_divider,
    _iso_to_dt,
)


@dataclass(slots=True)
class RecentCommit:
    sha: str
    committed_at: str
    subject: str


def _compact_age(dt, *, now=None) -> str:
    from datetime import UTC, datetime

    if dt is None:
        return "?"
    current = now or datetime.now(UTC)
    seconds = max(0, int((current - dt).total_seconds()))
    if seconds < 60:
        return "now"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _list_recent_commits(
    project_path: Path,
    *,
    limit: int = 5,
    timeout: float = 2.0,
    runner=None,
) -> list[RecentCommit]:
    """Best-effort git log reader for the current project root."""
    if limit <= 0 or not (project_path / ".git").exists():
        return []
    run = runner or subprocess.run
    fmt = "%H%x1f%cI%x1f%s%x1e"
    cmd = [
        "git",
        "log",
        f"-{limit}",
        f"--pretty=format:{fmt}",
        "--no-merges",
    ]
    try:
        result = run(
            cmd,
            cwd=project_path,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []
    if result.returncode != 0:
        return []

    commits: list[RecentCommit] = []
    for raw in result.stdout.split("\x1e"):
        item = raw.strip()
        if not item:
            continue
        parts = item.split("\x1f")
        if len(parts) < 3:
            continue
        commits.append(
            RecentCommit(
                sha=parts[0].strip(),
                committed_at=parts[1].strip(),
                subject=parts[2].strip(),
            )
        )
    return commits


def _section_recent_commits(project_path: Path) -> list[str]:
    from datetime import UTC, datetime

    lines = [_dashboard_divider("Recent commits"), ""]
    commits = _list_recent_commits(project_path)
    if not commits:
        lines.extend([f"{_DASHBOARD_BULLET}(none)", ""])
        return lines

    now = datetime.now(UTC)
    for commit in commits:
        age = _compact_age(_iso_to_dt(commit.committed_at), now=now)
        subject = commit.subject or "(no subject)"
        if len(subject) > 52:
            subject = subject[:49] + "..."
        lines.append(
            f"{_DASHBOARD_BULLET}{commit.sha[:7]:<7}  {age:<4}  {subject}"
        )
    lines.append("")
    return lines
