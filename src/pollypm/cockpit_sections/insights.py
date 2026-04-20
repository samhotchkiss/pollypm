"""Advisor-insights section (#403)."""

from __future__ import annotations

import json
from pathlib import Path

from pollypm.cockpit_sections.base import (
    _DASHBOARD_BULLET,
    _age_from_dt,
    _dashboard_divider,
    _iso_to_dt,
)


def _section_insights(project_path: Path, project_key: str) -> list[str]:
    """Last 7 days of advisor insights (emit=true) for this project."""
    from datetime import UTC, datetime, timedelta

    lines = [_dashboard_divider("Insights"), ""]
    log_path = project_path / ".pollypm" / "advisor-log.jsonl"
    if not log_path.exists():
        lines.extend(
            [f"{_DASHBOARD_BULLET}(no advisor insights in last 7 days)", ""]
        )
        return lines

    cutoff = datetime.now(UTC) - timedelta(days=7)
    kept: list[tuple[object, str, str]] = []
    try:
        raw = log_path.read_text(encoding="utf-8")
    except OSError:
        raw = ""
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("project") != project_key:
            continue
        if data.get("decision") != "emit":
            continue
        dt = _iso_to_dt(data.get("timestamp"))
        if dt is None or dt < cutoff:
            continue
        summary = str(data.get("summary") or "")[:60]
        severity = str(data.get("severity") or "")
        kept.append((dt, severity, summary))
    if not kept:
        lines.extend(
            [f"{_DASHBOARD_BULLET}(no advisor insights in last 7 days)", ""]
        )
        return lines
    kept.sort(key=lambda row: row[0], reverse=True)
    for dt, severity, summary in kept[:5]:
        age = _age_from_dt(dt)
        sev = f"[{severity}] " if severity else ""
        lines.append(f"{_DASHBOARD_BULLET}\u2726 {sev}{summary} ({age})")
    lines.append("")
    return lines
