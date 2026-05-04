"""Global Polly dashboard renderer (#403 + #505 + #511 + #512 + #515)."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pollypm.cockpit_sections.base import (
    _STATUS_ICONS,
    _age_from_dt,
    _dashboard_divider,
    _format_clock,
    _format_tokens,
    _iso_to_dt,
)
from pollypm.cockpit_sections.health import (
    format_project_health_scorecard,
    project_health_rank,
)
from pollypm.cockpit_sections.just_shipped import _section_just_shipped
from pollypm.cockpit_sections.project_dashboard import (
    _DASHBOARD_PROJECT_CACHE,
    _dashboard_project_tasks,
)


@dataclass(slots=True)
class DashboardSuggestion:
    label: str
    detail: str = ""


@dataclass(slots=True)
class DashboardTokenGauge:
    account_name: str
    provider: str
    used_pct: int
    summary: str
    severity: str
    used_tokens: int | None = None
    limit_tokens: int | None = None
    burn_rate_per_hour: float = 0.0
    eta_seconds: float | None = None
    reset_at: str = ""


@dataclass(slots=True)
class DashboardAccountUsage:
    """One row in the Home dashboard's "Accounts" section (#1093).

    Sibling to :class:`DashboardTokenGauge` but represents the
    *per-account* surface — one row per LLM account, regardless of
    how many sessions share the account. Quota is account-level
    (Sam's scope refinement on #1093): multiple Claude Code sessions
    on the same Anthropic Max account share a single weekly bucket,
    so the dashboard shows one row per account, not per session.
    """

    account_name: str
    provider: str
    email: str
    used_pct: int | None
    summary: str
    severity: str  # "ok" / "warning" / "critical" / "unknown"
    reset_at: str = ""
    health: str = ""


@dataclass(slots=True)
class DashboardBriefingBanner:
    text: str
    date_local: str = ""
    created_at: datetime | None = None


def _enum_value(value: object) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "")


def _task_id(project_key: str, task: object) -> str:
    task_id = getattr(task, "task_id", None)
    if task_id:
        return str(task_id)
    number = getattr(task, "task_number", None)
    if number is None:
        return project_key
    return f"{project_key}/{number}"


def _task_worker(task: object) -> str:
    assignee = getattr(task, "assignee", None)
    if assignee:
        return str(assignee)
    roles = getattr(task, "roles", None)
    if isinstance(roles, dict) and roles.get("worker"):
        return str(roles["worker"])
    for transition in getattr(task, "transitions", None) or []:
        if _enum_value(getattr(transition, "to_state", "")) != "in_progress":
            continue
        actor = getattr(transition, "actor", None)
        if actor and actor not in {"system", "pm"}:
            return str(actor)
    return ""


def _task_reviewer(task: object) -> str:
    """Best-effort name of the reviewer assigned to a review-state task.

    Used by the dashboard's "waiting for X" line. Tries the reviewer
    role assignment first, then the task assignee, then falls back to
    an empty string so the caller can render a generic placeholder.
    Without this helper the line was hardcoded to "waiting for Russell"
    — the autoreviewer name in the spec — which was misleading for any
    workspace with a different reviewer persona configured.
    """
    roles = getattr(task, "roles", None)
    if isinstance(roles, dict):
        reviewer = roles.get("reviewer") or roles.get("autoreviewer")
        if reviewer and reviewer != "user":
            return str(reviewer)
    assignee = getattr(task, "assignee", None)
    if assignee and assignee not in {"system", "pm"}:
        return str(assignee)
    return ""


def _task_done_at(task: object):
    last_done = None
    for transition in getattr(task, "transitions", None) or []:
        if _enum_value(getattr(transition, "to_state", "")) != "done":
            continue
        dt = _iso_to_dt(getattr(transition, "timestamp", None))
        if dt is not None and (last_done is None or dt > last_done):
            last_done = dt
    if last_done is not None:
        return last_done
    status = _enum_value(getattr(task, "work_status", ""))
    if status == "done":
        return _iso_to_dt(getattr(task, "updated_at", None))
    return None


def _task_last_rejection_at(task: object):
    last_rejected = None
    for execution in getattr(task, "executions", None) or []:
        if _enum_value(getattr(execution, "decision", "")) != "rejected":
            continue
        dt = _iso_to_dt(getattr(execution, "completed_at", None))
        if dt is not None and (last_rejected is None or dt > last_rejected):
            last_rejected = dt
    for transition in getattr(task, "transitions", None) or []:
        if _enum_value(getattr(transition, "from_state", "")) != "review":
            continue
        if _enum_value(getattr(transition, "to_state", "")) != "in_progress":
            continue
        dt = _iso_to_dt(getattr(transition, "timestamp", None))
        if dt is not None and (last_rejected is None or dt > last_rejected):
            last_rejected = dt
    return last_rejected


def _task_has_rejection(task: object) -> bool:
    return _task_last_rejection_at(task) is not None


def _shipper_streaks(task_pairs: list[tuple[str, object]], *, now: datetime) -> dict[str, int]:
    cutoff = now - timedelta(hours=24)
    events: dict[str, list[tuple[datetime, bool]]] = defaultdict(list)
    for _project_key, task in task_pairs:
        worker = _task_worker(task)
        if not worker:
            continue
        done_at = _task_done_at(task)
        rejected_at = _task_last_rejection_at(task)
        if done_at is not None and done_at >= cutoff:
            events[worker].append((done_at, not _task_has_rejection(task)))
            continue
        if rejected_at is not None and rejected_at >= cutoff:
            events[worker].append((rejected_at, False))
    streaks: dict[str, int] = {}
    for worker, worker_events in events.items():
        streak = 0
        for _when, clean_done in sorted(worker_events, key=lambda item: item[0], reverse=True):
            if not clean_done:
                break
            streak += 1
        if streak:
            streaks[worker] = streak
    return streaks


def _streak_badge(worker: str, streaks: dict[str, int]) -> str:
    streak = streaks.get(worker, 0)
    if streak < 2:
        return ""
    return f" 🔥{streak}"


def _render_streak_header(streaks: dict[str, int]) -> str:
    hottest = sorted(
        ((worker, streak) for worker, streak in streaks.items() if streak >= 2),
        key=lambda item: (-item[1], item[0]),
    )
    if not hottest:
        return ""
    leaders = ", ".join(f"{worker} ({streak})" for worker, streak in hottest[:3])
    return f"🔥 Hottest workers today: {leaders}"


def _task_priority_rank(task: object) -> int:
    priority = _enum_value(getattr(task, "priority", "normal"))
    return {
        "critical": 0,
        "high": 1,
        "normal": 2,
        "low": 3,
    }.get(priority, 4)


def _period_window_days(period_label: str) -> int:
    label = period_label.lower()
    if "day" in label:
        return 1
    if "month" in label:
        return 30
    return 7


def _hours_between(start, end) -> float:
    if start is None or end is None:
        return 0.0
    return max(0.0, (end - start).total_seconds() / 3600.0)


def _build_token_gauge(supervisor, config, *, now: datetime) -> DashboardTokenGauge | None:
    try:
        token_rows = list(supervisor.store.recent_token_usage(limit=5000))
    except Exception:  # noqa: BLE001
        token_rows = []

    gauges: list[DashboardTokenGauge] = []
    for account_name, account in getattr(config, "accounts", {}).items():
        try:
            usage = supervisor.store.get_account_usage(account_name)
        except Exception:  # noqa: BLE001
            usage = None
        if usage is None or usage.used_pct is None:
            continue

        window_days = _period_window_days(usage.period_label or "")
        cutoff = now - timedelta(days=window_days)
        account_rows: list[tuple[datetime, object]] = []
        for row in token_rows:
            if getattr(row, "account_name", "") != account_name:
                continue
            hour_bucket = _iso_to_dt(getattr(row, "hour_bucket", None))
            if hour_bucket is None or hour_bucket < cutoff:
                continue
            account_rows.append((hour_bucket, row))

        period_tokens = sum(int(getattr(row, "tokens_used", 0) or 0) for _dt, row in account_rows)
        used_tokens = None
        limit_tokens = None
        if usage.used_pct and period_tokens > 0:
            used_tokens = period_tokens
            limit_tokens = int(round(period_tokens / (usage.used_pct / 100.0)))

        burn_cutoff = now - timedelta(hours=3)
        recent_rows = [(dt, row) for dt, row in account_rows if dt >= burn_cutoff]
        burn_tokens = sum(int(getattr(row, "tokens_used", 0) or 0) for _dt, row in recent_rows)
        burn_rate = 0.0
        if recent_rows:
            burn_start = min(dt for dt, _row in recent_rows)
            hours = max(1.0, _hours_between(burn_start, now))
            burn_rate = burn_tokens / hours

        eta_seconds = None
        if used_tokens is not None and limit_tokens is not None and burn_rate > 0:
            remaining_tokens = max(0, limit_tokens - used_tokens)
            if remaining_tokens > 0:
                eta_seconds = remaining_tokens / burn_rate * 3600.0

        severity = "normal"
        if usage.used_pct >= 95:
            severity = "critical"
        elif usage.used_pct >= 80:
            severity = "warning"

        gauges.append(
            DashboardTokenGauge(
                account_name=account_name,
                provider=getattr(account, "provider", getattr(usage, "provider", "")),
                used_pct=int(usage.used_pct),
                summary=str(getattr(usage, "usage_summary", "") or ""),
                severity=severity,
                used_tokens=used_tokens,
                limit_tokens=limit_tokens,
                burn_rate_per_hour=burn_rate,
                eta_seconds=eta_seconds,
                reset_at=str(getattr(usage, "reset_at", "") or ""),
            )
        )

    if not gauges:
        return None
    gauges.sort(
        key=lambda gauge: (
            -gauge.used_pct,
            -(1 if gauge.burn_rate_per_hour > 0 else 0),
            -gauge.burn_rate_per_hour,
            gauge.account_name,
        )
    )
    return gauges[0]


def _format_eta(seconds: float) -> str:
    if seconds < 3600:
        return f"{max(1, int(seconds // 60))}m"
    if seconds < 86400:
        return f"{max(1, int(seconds // 3600))}h"
    return f"{max(1, int(seconds // 86400))}d"


def _render_token_gauge(gauge: DashboardTokenGauge) -> str:
    marker = "!!" if gauge.severity == "critical" else "!" if gauge.severity == "warning" else "·"
    if gauge.used_tokens is not None and gauge.limit_tokens is not None:
        usage_text = (
            f"~{_format_tokens(gauge.used_tokens)} / ~{_format_tokens(gauge.limit_tokens)} "
            f"({gauge.used_pct}%)"
        )
    else:
        usage_text = f"{gauge.used_pct}% used"
    line = f"{marker} Token burn: {gauge.account_name} · {usage_text}"
    if gauge.eta_seconds is not None and gauge.burn_rate_per_hour > 0:
        line += (
            f" · ~{_format_eta(gauge.eta_seconds)} left "
            f"@ {_format_tokens(int(gauge.burn_rate_per_hour))}/h"
        )
    elif gauge.reset_at:
        line += f" · resets {gauge.reset_at}"
    elif gauge.summary:
        line += f" · {gauge.summary}"
    return line


def _account_severity(used_pct: int | None) -> str:
    """Bucket used_pct into ok / warning / critical / unknown (#1093)."""
    if used_pct is None:
        return "unknown"
    if used_pct >= 95:
        return "critical"
    if used_pct >= 80:
        return "warning"
    return "ok"


def _account_marker(severity: str) -> str:
    """Threshold marker per #1093 spec: ▲ at 95%+, ◆ at 80%+, · otherwise."""
    if severity == "critical":
        return "▲"  # ▲
    if severity == "warning":
        return "◆"  # ◆
    if severity == "unknown":
        return "?"
    return "·"  # ·


def _provider_label(provider: object) -> str:
    raw = getattr(provider, "value", provider)
    text = str(raw or "").strip().lower()
    if text in {"claude", "anthropic"}:
        return "Anthropic"
    if text == "codex":
        return "OpenAI"
    if not text:
        return ""
    return text.capitalize()


def _build_account_usage_rows(
    supervisor, config, *, now: datetime,
) -> list[DashboardAccountUsage]:
    """Return one :class:`DashboardAccountUsage` per configured account.

    Reads cached rows from ``account_usage`` (populated by the
    ``account.usage_refresh`` recurring sweep in
    :mod:`pollypm.account_usage_sampler`). Accounts with no cached row
    yet still surface — with ``used_pct=None`` and a "no signal"
    summary — so the user knows the account exists and the probe
    hasn't reported yet.

    De-dupes by email so multiple PollyPM accounts pointing at the
    same Anthropic identity collapse to a single row (Sam's scope
    refinement on #1093: quota is account-level, not session-level
    or per-PollyPM-account-config).
    """
    del now  # reserved for future "stale signal" badge
    accounts = getattr(config, "accounts", None) or {}
    if not accounts:
        return []

    rows: list[DashboardAccountUsage] = []
    seen_emails: set[str] = set()
    for account_name, account in accounts.items():
        email = (getattr(account, "email", None) or "").strip()
        # Dedup by email when present — same Anthropic identity used
        # by Polly + reviewer-* PollyPM accounts shares one weekly
        # quota, so we only show it once.
        if email:
            if email in seen_emails:
                continue
            seen_emails.add(email)

        try:
            usage = supervisor.store.get_account_usage(account_name)
        except Exception:  # noqa: BLE001
            usage = None

        provider_value = _enum_value(getattr(account, "provider", "")) or (
            getattr(usage, "provider", "") if usage is not None else ""
        )
        provider_label = _provider_label(provider_value)

        if usage is None:
            rows.append(
                DashboardAccountUsage(
                    account_name=account_name,
                    provider=provider_label,
                    email=email,
                    used_pct=None,
                    summary="no signal yet",
                    severity="unknown",
                    health="unknown",
                )
            )
            continue

        used_pct = usage.used_pct
        severity = _account_severity(used_pct)
        if used_pct is not None:
            summary = f"{used_pct}% of weekly limit"
        else:
            summary = str(getattr(usage, "usage_summary", "") or "no signal yet")
        rows.append(
            DashboardAccountUsage(
                account_name=account_name,
                provider=provider_label,
                email=email,
                used_pct=used_pct,
                summary=summary,
                severity=severity,
                reset_at=str(getattr(usage, "reset_at", "") or ""),
                health=str(getattr(usage, "health", "") or ""),
            )
        )

    rows.sort(
        key=lambda row: (
            -(row.used_pct if row.used_pct is not None else -1),
            row.provider.lower(),
            row.account_name,
        )
    )
    return rows


def _render_account_usage_lines(rows: list[DashboardAccountUsage]) -> list[str]:
    """Render the "Accounts" section block.

    Layout (#1093, per Sam's scope refinement):

        ─ Accounts ──────────────────────────────────────────────
          ◆ Anthropic (claude@swh.me)  79% of weekly limit · approaching ceiling
          · OpenAI                       no signal yet
    """
    if not rows:
        return []
    lines = [_dashboard_divider("Accounts"), ""]
    for row in rows:
        marker = _account_marker(row.severity)
        if row.email:
            label = f"{row.provider or '?'} ({row.email})"
        else:
            label = row.provider or row.account_name
        line = f"  {marker} {label}  {row.summary}"
        if row.severity == "critical":
            line += " ▲ over weekly limit"
        elif row.severity == "warning":
            line += " ◆ approaching ceiling"
        if row.reset_at and row.severity in {"warning", "critical"}:
            line += f" · resets {row.reset_at}"
        lines.append(line)
    lines.append("")
    return lines


def _recent_briefing_entry(base_dir: Path, *, now: datetime, status: str):
    from pollypm.plugins_builtin.morning_briefing.inbox import list_briefings

    for entry in list_briefings(base_dir, status=status, limit=8):
        created_at = _iso_to_dt(getattr(entry, "created_at", ""))
        if created_at is None:
            continue
        if created_at >= now - timedelta(hours=24):
            return entry, created_at
    return None, None


def _briefing_banner(config, *, config_path: Path | None, now: datetime) -> DashboardBriefingBanner | None:
    # #801: dashboard render is now read-only with respect to the
    # morning_briefing plugin. The previous implementation imported
    # plugin handlers/state/settings and called ``fire_briefing()``
    # from inside the banner builder, swallowing every exception —
    # that turned the dashboard into a hidden scheduling path and
    # masked plugin failures. Briefings now fire only through the
    # recurring-job/roster path; the dashboard surfaces whatever the
    # plugin has already published.
    del config_path  # unused since the render-time fire path is gone
    project_cfg = getattr(config, "project", None)
    base_dir_value = getattr(project_cfg, "base_dir", None)
    if not base_dir_value:
        return None
    base_dir = Path(base_dir_value)
    entry, created_at = _recent_briefing_entry(base_dir, now=now, status="open")
    if entry is None:
        return None
    text = "Morning briefing available — press B to read"
    age = _age_from_dt(created_at, now=now)
    if age:
        text += f" ({age})"
    return DashboardBriefingBanner(
        text=text,
        date_local=str(getattr(entry, "date_local", "") or ""),
        created_at=created_at,
    )


def _rank_dashboard_suggestions(
    *,
    review_tasks: list[tuple[str, object]],
    blocked_tasks: list[tuple[str, object]],
    queued_tasks: list[tuple[str, object]],
    briefing_banner: DashboardBriefingBanner | None,
    user_inbox: int,
    config,
    now: datetime,
) -> list[DashboardSuggestion]:
    suggestions: list[DashboardSuggestion] = []

    if review_tasks:
        review_project, review_task = sorted(
            review_tasks,
            key=lambda item: _iso_to_dt(getattr(item[1], "updated_at", None)) or now,
        )[0]
        suggestions.append(
            DashboardSuggestion(
                label=f"Approve {_task_id(review_project, review_task)}",
                detail=f"review needed {_age_from_dt(_iso_to_dt(getattr(review_task, 'updated_at', None)), now=now)}",
            )
        )

    if blocked_tasks:
        blocked_project, blocked_task = sorted(
            blocked_tasks,
            key=lambda item: _iso_to_dt(getattr(item[1], "updated_at", None)) or now,
        )[0]
        suggestions.append(
            DashboardSuggestion(
                label=f"Check why {_task_id(blocked_project, blocked_task)} is blocked",
                detail=_age_from_dt(_iso_to_dt(getattr(blocked_task, "updated_at", None)), now=now),
            )
        )

    if briefing_banner is not None:
        detail = f"{briefing_banner.date_local} · press B" if briefing_banner.date_local else "press B"
        suggestions.append(DashboardSuggestion(label="Read morning briefing", detail=detail))
    elif user_inbox:
        suggestions.append(
            DashboardSuggestion(
                label="Review inbox",
                detail=f"{user_inbox} item{'s' if user_inbox != 1 else ''} waiting",
            )
        )

    if queued_tasks:
        queued_project, queued_task = sorted(
            queued_tasks,
            key=lambda item: (
                _task_priority_rank(item[1]),
                _iso_to_dt(getattr(item[1], "updated_at", None)) or now,
            ),
        )[0]
        project = config.projects.get(queued_project)
        project_label = project.display_label() if project else queued_project
        suggestions.append(
            DashboardSuggestion(
                label=f"Claim {_task_id(queued_project, queued_task)}",
                detail=f"{getattr(queued_task, 'title', '')} ({project_label})",
            )
        )

    return suggestions[:4]


def _render_suggestions(suggestions: list[DashboardSuggestion]) -> list[str]:
    lines = [_dashboard_divider("What's next?"), ""]
    if not suggestions:
        lines.append("  Nothing urgent — the workspace is clear.")
        lines.append("")
        return lines
    for idx, suggestion in enumerate(suggestions, start=1):
        detail = f" · {suggestion.detail}" if suggestion.detail else ""
        lines.append(f"  {idx}. {suggestion.label}{detail}")
    lines.append("")
    return lines


def _build_dashboard(supervisor, config, config_path: Path | None = None) -> str:
    # Imported lazily to avoid a hard cycle with pollypm.cockpit while the
    # inbox helpers still live there.
    from pollypm.cockpit import _count_inbox_tasks_for_label

    lines: list[str] = []
    now = datetime.now(UTC)

    all_active: list[tuple[str, object]] = []
    all_review: list[tuple[str, object]] = []
    all_queued: list[tuple[str, object]] = []
    all_blocked: list[tuple[str, object]] = []
    all_done: list[tuple[str, object]] = []
    all_tasks: list[tuple[str, object]] = []
    project_scorecards: list[tuple[int, str, str]] = []
    total_counts: dict[str, int] = {}
    live_keys: set[str] = set()
    for project_key, project in config.projects.items():
        live_keys.add(project_key)
        partitioned, counts = _dashboard_project_tasks(project_key, project.path)
        project_tasks = [task for bucket in partitioned.values() for task in bucket]
        for status, count in counts.items():
            total_counts[status] = total_counts.get(status, 0) + count
        for status_name, bucket in partitioned.items():
            for task in bucket:
                pair = (project_key, task)
                all_tasks.append(pair)
                if status_name == "in_progress":
                    all_active.append(pair)
                elif status_name == "review":
                    all_review.append(pair)
                elif status_name == "queued":
                    all_queued.append(pair)
                elif status_name == "blocked":
                    all_blocked.append(pair)
                elif status_name == "done":
                    all_done.append(pair)
        label = (
            project.display_label()
            if hasattr(project, "display_label")
            else getattr(project, "name", None) or project_key
        )
        project_scorecards.append(
            (
                project_health_rank(project_tasks, now=now),
                str(label).lower(),
                format_project_health_scorecard(label, counts, project_tasks, now=now),
            )
        )
    for stale_key in list(_DASHBOARD_PROJECT_CACHE.keys()):
        if stale_key not in live_keys:
            _DASHBOARD_PROJECT_CACHE.pop(stale_key, None)
    all_done.sort(
        key=lambda item: _task_done_at(item[1]) or _iso_to_dt(getattr(item[1], "updated_at", None)) or now,
        reverse=True,
    )

    try:
        open_alerts = supervisor.store.open_alerts()
    except Exception:  # noqa: BLE001
        open_alerts = []
    from pollypm.cockpit_alerts import is_operational_alert

    user_inbox = _count_inbox_tasks_for_label(config)
    actionable_alerts = [
        alert
        for alert in open_alerts
        if not is_operational_alert(alert.alert_type)
    ]
    try:
        recent = supervisor.store.recent_events(limit=300)
    except Exception:  # noqa: BLE001
        recent = []
    cutoff_24h = (now - timedelta(hours=24)).isoformat()
    day_events = [event for event in recent if event.created_at >= cutoff_24h]

    streaks = _shipper_streaks(all_tasks, now=now)
    streak_header = _render_streak_header(streaks)
    token_gauge = _build_token_gauge(supervisor, config, now=now)
    account_rows = _build_account_usage_rows(supervisor, config, now=now)
    briefing_banner = _briefing_banner(config, config_path=config_path, now=now)
    suggestions = _rank_dashboard_suggestions(
        review_tasks=all_review,
        blocked_tasks=all_blocked,
        queued_tasks=all_queued,
        briefing_banner=briefing_banner,
        user_inbox=user_inbox,
        config=config,
        now=now,
    )

    lines.append("  PollyPM")
    if token_gauge is not None:
        lines.append("  " + _render_token_gauge(token_gauge))
    if streak_header:
        lines.append(f"  {streak_header}")
    if briefing_banner is not None:
        lines.append(f"  ☀ {briefing_banner.text}")
    lines.append("")

    # #1093: per-account quota surface. Quota is account-level
    # (multiple Claude Code sessions on the same Anthropic Max
    # account share one weekly bucket), so this is one row per
    # account. The single ``Token burn:`` line above shows the
    # hottest account; this section shows them all so a 79% → 100%
    # slide on a non-hottest account is still visible.
    lines.extend(_render_account_usage_lines(account_rows))

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

    count_parts = []
    # ``review`` was previously shown here AND on the attention bar
    # ("◉ N awaiting review") two lines up — same number, two glyphs,
    # confusing. Drop it from the flow-state breakdown so attention
    # owns the call-to-action and count_parts shows pure pipeline
    # state. Cycle 132 / dashboard audit fix.
    for status in ("in_progress", "queued", "blocked"):
        count = total_counts.get(status, 0)
        if count:
            icon = _STATUS_ICONS.get(status, "·")
            count_parts.append(f"{icon} {count} {status.replace('_', ' ')}")
    done_count = total_counts.get("done", 0)
    if done_count:
        count_parts.append(f"✓ {done_count} done")
    if count_parts:
        lines.append("  " + " · ".join(count_parts))
    lines.append("")

    if project_scorecards:
        lines.append(_dashboard_divider("Projects"))
        lines.append("")
        for _rank, _label, line in sorted(project_scorecards):
            lines.append(f"  {line}")
        lines.append("")

    lines.extend(_render_suggestions(suggestions))

    if all_active or all_review:
        lines.append(_dashboard_divider("Now"))
        lines.append("")
        for project_key, task in all_active:
            project = config.projects.get(project_key)
            project_label = project.display_label() if project else project_key
            worker = _task_worker(task)
            assignee = f" [{worker}{_streak_badge(worker, streaks)}]" if worker else ""
            node = getattr(task, "current_node_id", None) or ""
            age = _age_from_dt(_iso_to_dt(getattr(task, "updated_at", None)), now=now)
            lines.append(f"  ⟳ {getattr(task, 'title', '')}")
            lines.append(f"    {project_label}{assignee} · {node} · {age}")
            lines.append("")
        for project_key, task in all_review:
            project = config.projects.get(project_key)
            project_label = project.display_label() if project else project_key
            worker = _task_worker(task)
            worker_text = f" · by {worker}{_streak_badge(worker, streaks)}" if worker else ""
            age = _age_from_dt(_iso_to_dt(getattr(task, "updated_at", None)), now=now)
            reviewer = _task_reviewer(task)
            reviewer_text = (
                f"waiting for {reviewer}" if reviewer else "waiting for review"
            )
            lines.append(f"  ◉ {getattr(task, 'title', '')}")
            lines.append(
                f"    {project_label}{worker_text} · {reviewer_text} · {age}"
            )
            lines.append("")

    if all_queued:
        lines.append(_dashboard_divider("Ready"))
        lines.append("")
        for project_key, task in all_queued[:5]:
            project = config.projects.get(project_key)
            project_label = project.display_label() if project else project_key
            lines.append(f"  ○ {getattr(task, 'title', '')}  ({project_label})")
        if len(all_queued) > 5:
            lines.append(f"    + {len(all_queued) - 5} more queued")
        lines.append("")

    if all_done:
        lines.append(_dashboard_divider("Done"))
        lines.append("")
        for project_key, task in all_done[:8]:
            project = config.projects.get(project_key)
            project_label = project.display_label() if project else project_key
            worker = _task_worker(task)
            worker_text = f" · {worker}{_streak_badge(worker, streaks)}" if worker else ""
            age = _age_from_dt(
                _task_done_at(task) or _iso_to_dt(getattr(task, "updated_at", None)),
                now=now,
            )
            lines.append(f"  ✓ {getattr(task, 'title', '')}  ({project_label}{worker_text})  {age}")
        if len(all_done) > 8:
            lines.append(f"    + {len(all_done) - 8} more completed")
        lines.append("")

    lines.extend(_section_just_shipped(all_done, now=now))

    lines.append(_dashboard_divider("Activity"))
    lines.append("")
    commits = [event for event in day_events if "commit" in event.message.lower()]
    recoveries = [
        event for event in day_events
        if event.event_type in ("recover", "recovery", "stabilize_failed")
    ]
    sends = [event for event in day_events if event.event_type == "send_input"]
    activity_parts = []
    if commits:
        word = "commit" if len(commits) == 1 else "commits"
        activity_parts.append(f"{len(commits)} {word}")
    if sends:
        word = "message" if len(sends) == 1 else "messages"
        activity_parts.append(f"{len(sends)} {word}")
    if recoveries:
        word = "recovery" if len(recoveries) == 1 else "recoveries"
        activity_parts.append(f"{len(recoveries)} {word}")
    if activity_parts:
        lines.append("  Today: " + " · ".join(activity_parts))
    else:
        lines.append("  No notable activity today.")
    lines.append("")

    notable = [
        event
        for event in day_events
        if event.event_type not in ("heartbeat", "token_ledger", "polly_followup")
    ][:6]
    for event in notable:
        # Match per-project Activity (cockpit_sections/activity.py): use
        # absolute HH:MM clock time so users can correlate events across
        # surfaces. The previous "5m ago" / "2h ago" was inconsistent
        # with the per-project section and harder to sequence at a
        # glance. Audit UX #3.
        clock = _format_clock(_iso_to_dt(getattr(event, "created_at", None)))
        session = getattr(event, "session_name", "")
        message = getattr(event, "message", "")[:55]
        lines.append(f"  {clock}  {session}: {message}")
    if notable:
        lines.append("")

    if actionable_alerts:
        lines.append(_dashboard_divider("Alerts"))
        lines.append("")
        for alert in actionable_alerts[:5]:
            lines.append(f"  ▲ {alert.session_name}: {alert.message[:55]}")
        lines.append("")

    project_count = len(config.projects)
    project_word = "project" if project_count == 1 else "projects"
    lines.append(
        f"  {project_count} {project_word}  ·  j/k navigate  ·  S settings"
    )

    return "\n".join(lines)
