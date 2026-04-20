"""Downtime backlog section (#403)."""

from __future__ import annotations

from pathlib import Path

from pollypm.cockpit_sections.base import _DASHBOARD_BULLET, _dashboard_divider


def _section_downtime(project_path: Path) -> list[str]:
    """First 5 items from ``docs/downtime-backlog.md`` if present."""
    lines = [_dashboard_divider("Downtime backlog"), ""]
    backlog_path = project_path / "docs" / "downtime-backlog.md"
    if not backlog_path.exists():
        lines.extend([f"{_DASHBOARD_BULLET}(none queued)", ""])
        return lines
    try:
        raw = backlog_path.read_text(encoding="utf-8")
    except OSError:
        raw = ""
    entries: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        # Accept markdown bullets only \u2014 skip headings and prose.
        if stripped.startswith(("- ", "* ", "+ ")):
            entries.append(stripped[2:].strip())
        if len(entries) >= 5:
            break
    if not entries:
        lines.extend([f"{_DASHBOARD_BULLET}(none queued)", ""])
        return lines
    for e in entries:
        lines.append(f"{_DASHBOARD_BULLET}\u2022 {e[:60]}")
    lines.append("")
    return lines
