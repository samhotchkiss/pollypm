"""Counts-summary bar for the per-project dashboard (#403)."""

from __future__ import annotations

from pollypm.cockpit_sections.base import _DASHBOARD_BULLET, _STATUS_ICONS


def _section_summary(counts: dict[str, int]) -> str:
    """Counts bar: ``\u27f3 1 in progress \u00b7 \u25c9 0 review \u00b7 \u25cb 2 queued \u00b7 \u2713 1 done``."""
    parts: list[str] = []
    for status in ("in_progress", "review", "queued", "blocked", "on_hold", "draft"):
        n = counts.get(status, 0)
        if n:
            icon = _STATUS_ICONS.get(status, "\u00b7")
            parts.append(f"{icon} {n} {status.replace('_', ' ')}")
    done = counts.get("done", 0)
    if done:
        parts.append(f"\u2713 {done} done")
    if not parts:
        return f"{_DASHBOARD_BULLET}No tasks yet."
    return _DASHBOARD_BULLET + " \u00b7 ".join(parts)
