"""Unified 24h activity timeline section (#403)."""

from __future__ import annotations

from pollypm.cockpit_sections.base import (
    _DASHBOARD_BULLET,
    _dashboard_divider,
    _format_clock,
    _iso_to_dt,
)


def _section_activity(
    tasks: list,
    system_events: list,
    *,
    now=None,
    limit: int = 10,
) -> list[str]:
    """Unified timeline: task transitions + system events from last 24h."""
    from datetime import UTC, datetime, timedelta

    now = now or datetime.now(UTC)
    cutoff = now - timedelta(hours=24)

    entries: list[tuple[object, str]] = []  # (datetime, rendered row)

    for t in tasks:
        for tr in getattr(t, "transitions", None) or []:
            dt = getattr(tr, "timestamp", None)
            if dt is None:
                continue
            if getattr(dt, "tzinfo", None) is None:
                dt = dt.replace(tzinfo=UTC)
            if dt < cutoff:
                continue
            clock = _format_clock(dt)
            actor = tr.actor or "?"
            entries.append(
                (
                    dt,
                    f"{_DASHBOARD_BULLET}{clock}  task/{t.task_number} "
                    f"\u2192 {tr.to_state:<15}({actor})",
                )
            )

    for ev in system_events:
        dt = _iso_to_dt(getattr(ev, "created_at", None))
        if dt is None or dt < cutoff:
            continue
        etype = getattr(ev, "event_type", "")
        if etype in ("heartbeat", "token_ledger", "polly_followup"):
            continue
        sess = getattr(ev, "session_name", "") or "system"
        msg = (getattr(ev, "message", "") or "")[:50]
        clock = _format_clock(dt)
        entries.append(
            (
                dt,
                f"{_DASHBOARD_BULLET}{clock}  {etype} {msg} ({sess})",
            )
        )

    entries.sort(key=lambda row: row[0], reverse=True)

    lines = [_dashboard_divider("Activity (last 24h)"), ""]
    if not entries:
        lines.extend([f"{_DASHBOARD_BULLET}(none)", ""])
        return lines
    for _dt, row in entries[:limit]:
        lines.append(row)
    lines.append("")
    return lines
