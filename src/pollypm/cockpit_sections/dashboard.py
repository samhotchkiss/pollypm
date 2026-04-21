"""Global Polly dashboard renderer (#403).

Aggregates per-project task partitions, system events, and inbox counts
into the cockpit's "what does the whole workspace look like?" summary
that ships when the user selects ``polly``/``dashboard`` in the rail.
"""

from __future__ import annotations

from pollypm.cockpit_sections.base import _STATUS_ICONS
from pollypm.cockpit_sections.health import (
    format_project_health_scorecard,
    project_health_rank,
)
from pollypm.cockpit_sections.just_shipped import _section_just_shipped
from pollypm.cockpit_sections.project_dashboard import (
    _DASHBOARD_PROJECT_CACHE,
    _dashboard_project_tasks,
)


def _build_dashboard(supervisor, config) -> str:
    from datetime import UTC, datetime, timedelta

    # Imported lazily to avoid a hard cycle with pollypm.cockpit while the
    # inbox helpers still live there (Issue #405 will move them).
    from pollypm.cockpit import _count_inbox_tasks_for_label

    lines: list[str] = []
    now = datetime.now(UTC)

    def _age(ts_str: str) -> str:
        try:
            dt = datetime.fromisoformat(ts_str)
            secs = (now - dt).total_seconds()
            if secs < 60:
                return "just now"
            if secs < 3600:
                return f"{int(secs // 60)}m ago"
            if secs < 86400:
                return f"{int(secs // 3600)}h ago"
            return f"{int(secs // 86400)}d ago"
        except (ValueError, TypeError):
            return ""

    # ── Gather task data across all projects ──
    # Partition + counts are cached per project by state.db mtime; unchanged
    # projects skip SQLite on each render.
    all_active: list[tuple[str, object]] = []  # in_progress
    all_review: list[tuple[str, object]] = []  # waiting for review
    all_queued: list[tuple[str, object]] = []  # ready for pickup
    all_blocked: list[tuple[str, object]] = []
    all_done: list[tuple[str, object]] = []
    project_scorecards: list[tuple[int, str, str]] = []
    total_counts: dict[str, int] = {}
    live_keys: set[str] = set()
    for pk, proj in config.projects.items():
        live_keys.add(pk)
        partitioned, counts = _dashboard_project_tasks(pk, proj.path)
        tasks = [task for bucket in partitioned.values() for task in bucket]
        for s, n in counts.items():
            total_counts[s] = total_counts.get(s, 0) + n
        for t in partitioned.get("in_progress", ()):
            all_active.append((pk, t))
        for t in partitioned.get("review", ()):
            all_review.append((pk, t))
        for t in partitioned.get("queued", ()):
            all_queued.append((pk, t))
        for t in partitioned.get("blocked", ()):
            all_blocked.append((pk, t))
        for t in partitioned.get("done", ()):
            all_done.append((pk, t))
        label = (
            proj.display_label()
            if hasattr(proj, "display_label")
            else getattr(proj, "name", None) or pk
        )
        project_scorecards.append(
            (
                project_health_rank(tasks, now=now),
                str(label).lower(),
                format_project_health_scorecard(label, counts, tasks, now=now),
            )
        )
    # Evict cache entries for projects no longer in config.
    for stale_key in list(_DASHBOARD_PROJECT_CACHE.keys()):
        if stale_key not in live_keys:
            _DASHBOARD_PROJECT_CACHE.pop(stale_key, None)
    all_done.sort(key=lambda x: x[1].updated_at or "", reverse=True)

    # ── Gather system data ──
    open_alerts = supervisor.store.open_alerts()
    user_inbox = _count_inbox_tasks_for_label(config)
    actionable_alerts = [a for a in open_alerts if a.alert_type not in (
        "suspected_loop", "stabilize_failed", "needs_followup",
    )]
    recent = supervisor.store.recent_events(limit=300)
    cutoff_24h = (now - timedelta(hours=24)).isoformat()
    day_events = [e for e in recent if e.created_at >= cutoff_24h]

    # ── Header ──
    lines.append("  PollyPM")
    lines.append("")

    # Status line: what needs YOUR attention right now (actionable items only)
    attention: list[str] = []
    if all_review:
        attention.append(f"◉ {len(all_review)} awaiting review")
    if user_inbox:
        attention.append(f"✉ {user_inbox} inbox")
    if actionable_alerts:
        attention.append(f"▲ {len(actionable_alerts)} alert{'s' if len(actionable_alerts) != 1 else ''}")
    if attention:
        lines.append("  " + "  ·  ".join(attention))
        lines.append("")

    # Task count summary
    count_parts = []
    for status in ("in_progress", "review", "queued", "blocked"):
        n = total_counts.get(status, 0)
        if n:
            icon = _STATUS_ICONS.get(status, "·")
            count_parts.append(f"{icon} {n} {status.replace('_', ' ')}")
    done_n = total_counts.get("done", 0)
    if done_n:
        count_parts.append(f"✓ {done_n} done")
    if count_parts:
        lines.append("  " + " · ".join(count_parts))
    lines.append("")

    if project_scorecards:
        lines.append("  ─── Projects ─────────────────────────────────────")
        lines.append("")
        for _rank, _label, line in sorted(project_scorecards):
            lines.append(f"  {line}")
        lines.append("")

    # ── What's happening right now ──
    if all_active or all_review:
        lines.append("  ─── Now ───────────────────────────────────────────")
        lines.append("")
        for pk, t in all_active:
            project = config.projects.get(pk)
            proj_label = project.display_label() if project else pk
            assignee = f" [{t.assignee}]" if t.assignee else ""
            node = t.current_node_id or ""
            age = _age(t.updated_at) if t.updated_at else ""
            lines.append(f"  ⟳ {t.title}")
            lines.append(f"    {proj_label}{assignee} · {node} · {age}")
            lines.append("")
        for pk, t in all_review:
            project = config.projects.get(pk)
            proj_label = project.display_label() if project else pk
            age = _age(t.updated_at) if t.updated_at else ""
            lines.append(f"  ◉ {t.title}")
            lines.append(f"    {proj_label} · waiting for Russell · {age}")
            lines.append("")

    # ── Queued (ready for pickup) ──
    if all_queued:
        lines.append("  ─── Ready ─────────────────────────────────────────")
        lines.append("")
        for pk, t in all_queued[:5]:
            project = config.projects.get(pk)
            proj_label = project.display_label() if project else pk
            lines.append(f"  ○ {t.title}  ({proj_label})")
        if len(all_queued) > 5:
            lines.append(f"    + {len(all_queued) - 5} more queued")
        lines.append("")

    # ── Recently completed ──
    if all_done:
        lines.append("  ─── Done ──────────────────────────────────────────")
        lines.append("")
        for pk, t in all_done[:8]:
            project = config.projects.get(pk)
            proj_label = project.display_label() if project else pk
            age = _age(t.updated_at) if t.updated_at else ""
            lines.append(f"  ✓ {t.title}  ({proj_label})  {age}")
        if len(all_done) > 8:
            lines.append(f"    + {len(all_done) - 8} more completed")
        lines.append("")

    lines.extend(_section_just_shipped(all_done, now=now))

    # ── System activity ──
    lines.append("  ─── Activity ──────────────────────────────────────")
    lines.append("")
    commits = [e for e in day_events if "commit" in e.message.lower()]
    recoveries = [e for e in day_events if e.event_type in ("recover", "recovery", "stabilize_failed")]
    sends = [e for e in day_events if e.event_type == "send_input"]
    activity_parts = []
    if commits:
        activity_parts.append(f"{len(commits)} commits")
    if sends:
        activity_parts.append(f"{len(sends)} messages")
    if recoveries:
        activity_parts.append(f"{len(recoveries)} recoveries")
    if activity_parts:
        lines.append("  Today: " + " · ".join(activity_parts))
    else:
        lines.append("  No notable activity today.")
    lines.append("")

    # Show last few notable events with timestamps
    notable = [e for e in day_events if e.event_type not in ("heartbeat", "token_ledger", "polly_followup")][:6]
    for event in notable:
        age = _age(event.created_at)
        session = event.session_name
        msg = event.message[:55]
        lines.append(f"  {age:>8}  {session}: {msg}")
    if notable:
        lines.append("")

    # ── Alerts (if any) ──
    if actionable_alerts:
        lines.append("  ─── Alerts ────────────────────────────────────────")
        lines.append("")
        for alert in actionable_alerts[:5]:
            lines.append(f"  ▲ {alert.session_name}: {alert.message[:55]}")
        lines.append("")

    # ── Footer ──
    project_count = len(config.projects)
    lines.append(f"  {project_count} projects  ·  j/k navigate  ·  S settings")

    return "\n".join(lines)
