from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pollypm.config import load_config
from pollypm.cockpit_rail import CockpitItem, CockpitRouter  # noqa: F401
from pollypm.projects import ensure_project_scaffold
from pollypm.service_api import PollyPMService
from pollypm.task_backends import get_task_backend
from pollypm.worktrees import list_worktrees

# ---------------------------------------------------------------------------
# Dashboard section helpers (#403). The heavy rendering lives in
# ``pollypm.cockpit_sections``; we re-export the legacy names here so
# external callers + the test suite keep their existing import paths.
# ---------------------------------------------------------------------------
from pollypm.cockpit_sections import (  # noqa: F401  (re-exported for callers)
    _DASHBOARD_BULLET,
    _DASHBOARD_DIVIDER_WIDTH,
    _DASHBOARD_PROJECT_CACHE,
    _STATUS_ICONS,
    _age_from_dt,
    _aggregate_project_tokens,
    _build_dashboard,
    _dashboard_divider,
    _dashboard_project_tasks,
    _find_commit_sha,
    _format_clock,
    _format_tokens,
    _iso_to_dt,
    _render_project_dashboard,
    _section_activity,
    _section_downtime,
    _section_header,
    _section_in_flight,
    _section_insights,
    _section_quick_actions,
    _section_recent,
    _section_summary,
    _section_velocity,
    _section_you_need_to,
    _spark_bar,
    _task_cycle_minutes,
    _worker_presence,
)

# ---------------------------------------------------------------------------
# Inbox + worker roster panels (#405). Lives in ``pollypm.cockpit_inbox``
# so the right-pane dispatcher below, the inbox unit tests, and the rail
# badge providers can reach one canonical set of helpers. Re-exported
# here so the legacy ``from pollypm.cockpit import _count_inbox_tasks_for_label``
# import path stays green.
# ---------------------------------------------------------------------------
from pollypm.cockpit_inbox import (  # noqa: F401  (re-exported for callers)
    WorkerRosterRow,
    _count_inbox_tasks_for_label,
    _format_worker_turn_label,
    _gather_activity_feed,
    _gather_worker_roster,
    _inbox_db_sources,
    _last_commit_age,
    _register_worker_roster_rail_item,
    _render_inbox_panel,
    _render_work_service_issues,
    _render_worker_roster_panel,
    _try_load_supervisor_for_config,
    _worker_roster_sort_key,
    render_inbox_panel,
)


def build_cockpit_detail(config_path: Path, kind: str, target: str | None = None) -> str:
    try:
        return _build_cockpit_detail_inner(config_path, kind, target)
    except Exception as exc:  # noqa: BLE001
        return f"Error loading {kind} view: {exc}"


def _build_cockpit_detail_inner(config_path: Path, kind: str, target: str | None = None) -> str:
    supervisor = PollyPMService(config_path).load_supervisor()
    try:
        supervisor.ensure_layout()
        return _build_cockpit_detail_dispatch(supervisor, config_path, kind, target)
    finally:
        try:
            supervisor.store.close()
        except Exception:  # noqa: BLE001
            pass


def _build_cockpit_detail_dispatch(supervisor, config_path: Path, kind: str, target: str | None = None) -> str:
    config = supervisor.config
    if kind in ("polly", "dashboard"):
        return _build_dashboard(supervisor, config, config_path=config_path)

    if kind == "inbox":
        return _render_inbox_panel(config)

    if kind == "workers":
        return _render_worker_roster_panel(config_path)

    if kind == "metrics":
        return _render_metrics_panel(config_path)

    if kind == "activity":
        from pollypm.plugins_builtin.activity_feed.cockpit.feed_panel import (
            render_activity_feed_text,
        )

        return render_activity_feed_text(config)

    if kind == "settings":
        recent_usage = supervisor.store.recent_token_usage(limit=5)
        lines = [
            "Settings",
            "",
            f"Workspace root: {config.project.workspace_root}",
            f"Control account: {config.pollypm.controller_account}",
            f"Failover order: {', '.join(config.pollypm.failover_accounts) or 'none'}",
            f"Open permissions by default: {'on' if config.pollypm.open_permissions_by_default else 'off'}",
            "",
            "This pane is read-only for now.",
            "Use Polly or the legacy `pm ui` surface for deeper account/runtime changes.",
        ]
        if recent_usage:
            lines.extend(["", "Recent token usage:"])
            for row in recent_usage[:4]:
                lines.append(
                    f"- {row.project_key} · {row.account_name} · {row.model_name} · {row.tokens_used} tokens"
                )
        return "\n".join(lines)

    if kind == "project" and target:
        project = config.projects.get(target)
        if project is None:
            return f"Project '{target}' not found in config.\n\nIt may not have been saved. Try `pm add-project <path>` or check ~/.pollypm/pollypm.toml."
        ensure_project_scaffold(project.path)

        # Try work service dashboard first
        try:
            dashboard = _render_project_dashboard(project, target, config_path, supervisor)
            if dashboard:
                return dashboard
        except Exception:
            pass

        # Fallback to basic project info
        task_backend = get_task_backend(project.path)
        issues_root = task_backend.issues_root()
        state_counts = task_backend.state_counts() if task_backend.exists() else {}
        worktrees = [item for item in list_worktrees(config_path, target) if item.status == "active"]
        lines = [
            f"{project.name or project.key}",
            "",
            f"Path: {project.path}",
            f"Kind: {project.kind.value}",
            f"Tracked: {'yes' if project.tracked else 'no'}",
            f"Issue tracker: {issues_root if task_backend.exists() else 'not initialized'}",
            f"Active worktrees: {len(worktrees)}",
            "",
            "No active live lane is running for this project.",
            "Select the project in the left rail and press N to start a worker lane.",
        ]
        # Show alerts for this project's sessions (operational alerts
        # like suspected_loop / stabilize_failed / needs_followup stay
        # on the activity log, not the dashboard — see #765).
        from pollypm.cockpit_alerts import is_operational_alert

        project_alerts = [
            a for a in supervisor.store.open_alerts()
            if any(
                l.session.project == target and l.session.name == a.session_name
                for l in supervisor.plan_launches()
            ) and not is_operational_alert(a.alert_type)
        ]
        if project_alerts:
            lines.extend(["", "⚠ Alerts:"])
            for a in project_alerts:
                lines.append(f"  {a.severity} {a.alert_type}: {a.message}")
            lines.append("")
        if state_counts:
            lines.extend(["Task states:"])
            for state, count in state_counts.items():
                if count:
                    lines.append(f"- {state}: {count}")
        return "\n".join(lines)

    if kind == "issues" and target:
        project = config.projects.get(target)
        if not project:
            return f"Project '{target}' not found."
        # Try the work service first, fall back to file-based backend
        try:
            return _render_work_service_issues(project)
        except Exception:
            pass
        task_backend = get_task_backend(project.path)
        if not task_backend.exists():
            return f"{project.name or project.key} · Issues\n\nNo issue tracker initialized.\nUse `pm init-tracker {target}` to create one."
        state_counts = task_backend.state_counts()
        lines = [f"{project.name or project.key} · Issues", ""]
        for state_name in ["01-ready", "02-in-progress", "03-needs-review", "04-in-review", "05-completed"]:
            count = state_counts.get(state_name, 0)
            if count:
                tasks = task_backend.list_tasks(states=[state_name])
                lines.append(f"─── {state_name} ({count}) ───")
                for task in tasks[:8]:
                    lines.append(f"  {task.task_id}: {task.title}")
                lines.append("")
        if not any(state_counts.values()):
            lines.append("No issues found.")
        return "\n".join(lines)

    return "PollyPM\n\nSelect Polly, Inbox, a project, or Settings from the left rail."


# ---------------------------------------------------------------------------
# Worker roster — a live mission-control view that spans every project.
# ``_gather_worker_roster`` walks the config, opens each project's
# ``.pollypm/state.db`` to find active task assignments, cross-references
# tmux windows + supervisor state, and produces one ``WorkerRosterRow``
# per worker session. The row is the stable shape every renderer + test
# consumes — keep it data-only (no Textual imports).
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Observability metrics snapshot — the data source behind the fifth cockpit
# surface (``PollyMetricsApp``). Kept in ``cockpit.py`` alongside the worker
# roster gather so the metrics screen can reuse existing helpers
# (``_gather_worker_roster``, ``_dashboard_project_tasks``,
# ``_count_inbox_tasks_for_label``) without duplication. The snapshot is a
# plain dataclass so tests + renderer consume the same shape.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MetricsSection:
    """One of the five metrics sections the screen renders.

    ``rows`` is a list of ``(label, value, tone)`` triples. ``tone`` is
    one of ``"ok"``, ``"warn"``, ``"alert"``, ``"muted"`` — the renderer
    maps each to a Rich colour consistent with the cockpit palette.
    """

    key: str     # "fleet" | "resources" | "throughput" | "failures" | "schedulers"
    title: str
    rows: list[tuple[str, str, str]]


@dataclass(slots=True)
class MetricsSnapshot:
    """Immutable snapshot of system health numbers.

    Built by :func:`_gather_metrics_snapshot` and rendered by the Textual
    ``PollyMetricsApp`` screen. Every field is best-effort — a failing
    subsystem lands as ``"?"`` in the row rather than propagating a
    crash. The snapshot also carries the ``captured_at`` ISO timestamp so
    the renderer can show "last refreshed" info without re-reading a
    clock.
    """

    captured_at: str
    fleet: MetricsSection
    resources: MetricsSection
    throughput: MetricsSection
    failures: MetricsSection
    schedulers: MetricsSection

    def sections(self) -> list[MetricsSection]:
        return [self.fleet, self.resources, self.throughput, self.failures, self.schedulers]


def _humanize_bytes(num: int | float) -> str:
    """Render a byte count in KB/MB/GB form with one decimal.

    Lives alongside the metrics gather because every resource row
    wants the same short-and-readable width.
    """
    try:
        value = float(num)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024.0:
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} PB"


def _dir_size_bytes(path: Path) -> int:
    """Walk ``path`` once and return the sum of ``st_size`` for files.

    Best-effort — symlinks are not followed, read errors are silently
    skipped. Missing paths return 0 so the metrics screen can still
    display a useful row.
    """
    total = 0
    if not path.exists():
        return 0
    try:
        for child in path.rglob("*"):
            try:
                if child.is_file():
                    total += child.stat().st_size
            except OSError:
                continue
    except OSError:
        return total
    return total


def _rss_bytes_for_pid(pid: int) -> int | None:
    """Resident-set-size for a PID in bytes, or ``None`` when unavailable.

    Shells out to ``ps -p <pid> -o rss=`` — portable across macOS + Linux
    and doesn't need psutil. The ``ps`` output is in kilobytes so we
    multiply back to bytes for the consumer.
    """
    try:
        import subprocess as _sp
        result = _sp.run(
            ["ps", "-p", str(pid), "-o", "rss="],
            capture_output=True, text=True, check=False, timeout=2,
        )
    except Exception:  # noqa: BLE001
        return None
    if result.returncode != 0:
        return None
    raw = (result.stdout or "").strip()
    if not raw:
        return None
    try:
        return int(raw) * 1024
    except ValueError:
        return None


def _metrics_24h_events(store, now=None) -> list:
    """Return the subset of ``recent_events`` within the last 24 hours.

    Reads up to 2 000 rows via ``recent_events`` and filters client-side.
    That's a bounded read and keeps the snapshot self-contained without
    adding a new StateStore method. Returns an empty list if the store
    read fails.
    """
    from datetime import UTC, datetime, timedelta
    if now is None:
        now = datetime.now(UTC)
    cutoff_iso = (now - timedelta(hours=24)).isoformat()
    try:
        recent = store.recent_events(limit=2000)
    except Exception:  # noqa: BLE001
        return []
    return [e for e in recent if getattr(e, "created_at", "") >= cutoff_iso]


def _fleet_section(config, roster_rows: list, task_counts: dict[str, int],
                   inbox_breakdown: dict[str, int]) -> MetricsSection:
    """Section 1 — Workers / Tasks in flight / Inbox rollup."""
    n_working = sum(1 for r in roster_rows if r.status == "working")
    n_idle = sum(1 for r in roster_rows if r.status == "idle")
    n_stuck = sum(1 for r in roster_rows if r.status == "stuck")
    n_offline = sum(1 for r in roster_rows if r.status == "offline")

    rows: list[tuple[str, str, str]] = []
    worker_tone = "alert" if n_stuck else ("ok" if n_working else "muted")
    rows.append(
        ("Workers",
         f"{n_working} working · {n_idle} idle · {n_stuck} stuck · {n_offline} offline",
         worker_tone),
    )

    queued = int(task_counts.get("queued", 0))
    in_progress = int(task_counts.get("in_progress", 0))
    review = int(task_counts.get("review", 0))
    blocked = int(task_counts.get("blocked", 0))
    on_hold = int(task_counts.get("on_hold", 0))
    flight_tone = "alert" if blocked else ("ok" if in_progress else "muted")
    rows.append(
        ("Tasks in flight",
         f"{queued} queued · {in_progress} in_progress · {review} review · "
         f"{blocked} blocked · {on_hold} on_hold",
         flight_tone),
    )

    action = inbox_breakdown.get("action", 0)
    unread = inbox_breakdown.get("unread", 0)
    plan_review = inbox_breakdown.get("plan_review", 0)
    blocked = inbox_breakdown.get("blocked", 0)
    inbox_tone = "warn" if (action or unread or plan_review or blocked) else "ok"
    rows.append(
        ("Inbox",
         f"{action} action · {unread} unread · {plan_review} plan_review · {blocked} blocked",
         inbox_tone),
    )
    return MetricsSection(key="fleet", title="Fleet", rows=rows)


def _inbox_breakdown(config) -> dict[str, int]:
    """Return actionable inbox counts from the shared inbox-entry triage.

    Uses the same scan as :func:`_count_inbox_tasks_for_label` so the
    numbers line up with the rail badge. Best-effort: returns zero-value
    dict on any error.
    """
    out = {"action": 0, "unread": 0, "plan_review": 0, "blocked": 0}
    try:
        from pollypm.cockpit_inbox_items import load_inbox_entries
    except Exception:  # noqa: BLE001
        return out
    try:
        items, unread_ids, _replies = load_inbox_entries(config)
    except Exception:  # noqa: BLE001
        return out
    out["unread"] = len(unread_ids)
    for item in items:
        if not getattr(item, "needs_action", False):
            continue
        out["action"] += 1
        labels = {str(label) for label in list(getattr(item, "labels", []) or [])}
        triage_label = str(getattr(item, "triage_label", "") or "")
        if "plan_review" in labels or triage_label == "plan review":
            out["plan_review"] += 1
        if triage_label in {"blocked", "worker blocked"}:
            out["blocked"] += 1
    return out


def _resource_section(config) -> MetricsSection:
    """Section 2 — state.db size, worktrees, logs, session RSS."""
    rows: list[tuple[str, str, str]] = []

    # state.db size + freelist ratio for the workspace-root DB.
    state_db = getattr(getattr(config, "project", None), "state_db", None)
    if state_db is not None:
        state_db = Path(state_db)
    if state_db and state_db.exists():
        try:
            db_size = state_db.stat().st_size
        except OSError:
            db_size = 0
        freelist_ratio = 0.0
        try:
            import sqlite3
            conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
            try:
                page_size = int(
                    conn.execute("PRAGMA page_size").fetchone()[0] or 0,
                )
                freelist = int(
                    conn.execute("PRAGMA freelist_count").fetchone()[0] or 0,
                )
                if page_size and db_size:
                    freelist_ratio = (page_size * freelist) / db_size
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            pass
        tone = "alert" if db_size > 500 * 1024 * 1024 else (
            "warn" if db_size > 100 * 1024 * 1024 else "ok"
        )
        rows.append(
            ("state.db",
             f"{_humanize_bytes(db_size)} · freelist {freelist_ratio*100:.1f}%",
             tone),
        )
    else:
        rows.append(("state.db", "(missing)", "muted"))

    # Agent worktrees — count + disk usage.
    workspace_root = getattr(getattr(config, "project", None), "workspace_root", None)
    wt_root = Path(workspace_root) / ".claude" / "worktrees" if workspace_root else None
    if wt_root and wt_root.exists():
        try:
            wt_count = sum(1 for p in wt_root.iterdir() if p.is_dir())
        except OSError:
            wt_count = 0
        wt_size = _dir_size_bytes(wt_root)
        tone = "alert" if wt_size > 10 * 1024**3 else (
            "warn" if wt_size > 2 * 1024**3 else "ok"
        )
        rows.append(
            (".claude/worktrees",
             f"{wt_count} worktree{'s' if wt_count != 1 else ''} · {_humanize_bytes(wt_size)}",
             tone),
        )
    else:
        rows.append((".claude/worktrees", "(none)", "muted"))

    # Log directory size under ~/.pollypm/logs/
    logs_dir = Path.home() / ".pollypm" / "logs"
    if logs_dir.exists():
        log_bytes = 0
        try:
            for f in logs_dir.glob("*.log*"):
                try:
                    log_bytes += f.stat().st_size
                except OSError:
                    continue
        except OSError:
            pass
        tone = "warn" if log_bytes > 500 * 1024 * 1024 else "ok"
        rows.append(("logs", _humanize_bytes(log_bytes), tone))
    else:
        rows.append(("logs", "(none)", "muted"))

    # Memory footprint per live session — sourced from tmux-tracked sessions.
    try:
        from pollypm.service_api import PollyPMService
        cfg_path = getattr(
            getattr(config, "project", None), "config_file", None,
        ) or getattr(
            getattr(config, "project", None), "config_path", None,
        )
        supervisor = None
        if cfg_path is not None:
            supervisor = PollyPMService(cfg_path).load_supervisor(readonly_state=True)
    except Exception:  # noqa: BLE001
        supervisor = None

    total_rss = 0
    live_count = 0
    if supervisor is not None:
        try:
            launches, windows, _alerts, _leases, _errors = supervisor.status()
        except Exception:  # noqa: BLE001
            launches, windows = [], []
        window_map = {w.name: w for w in windows}
        for launch in launches:
            window = window_map.get(launch.window_name)
            if window is None or getattr(window, "pane_dead", False):
                continue
            try:
                pid = int(getattr(window, "pane_pid", 0) or 0)
            except (TypeError, ValueError):
                pid = 0
            if pid <= 0:
                continue
            rss = _rss_bytes_for_pid(pid)
            if rss is None:
                continue
            total_rss += rss
            live_count += 1
        try:
            supervisor.store.close()
        except Exception:  # noqa: BLE001
            pass
    mem_tone = "alert" if total_rss > 8 * 1024**3 else (
        "warn" if total_rss > 2 * 1024**3 else "ok"
    )
    if live_count:
        rows.append(
            ("Session RSS",
             f"{live_count} session{'s' if live_count != 1 else ''} · {_humanize_bytes(total_rss)}",
             mem_tone),
        )
    else:
        rows.append(("Session RSS", "(no live sessions)", "muted"))

    return MetricsSection(key="resources", title="Resources", rows=rows)


def _throughput_section(day_events: list) -> MetricsSection:
    """Section 3 — commits / approvals / completions in the last 24h."""
    rows: list[tuple[str, str, str]] = []

    def _count(pred) -> int:
        return sum(1 for e in day_events if pred(getattr(e, "event_type", "")))

    completed = _count(lambda k: k in ("task_done", "task.done"))
    rejected = _count(lambda k: "reject" in (k or "").lower())
    approvals = _count(
        lambda k: k in ("plan_approved", "plan.approved", "task.approved", "approve"),
    )
    commits = _count(lambda k: "commit" in (k or "").lower() or k == "ran")
    pr_reviews = _count(
        lambda k: "pr_reviewed" in (k or "") or k == "review_completed",
    )

    rows.append(("Tasks completed",
                 str(completed),
                 "ok" if completed else "muted"))
    rows.append(("Tasks rejected",
                 str(rejected),
                 "warn" if rejected else "muted"))
    rows.append(("PRs reviewed",
                 str(pr_reviews),
                 "ok" if pr_reviews else "muted"))
    rows.append(("Commits (worker events)",
                 str(commits),
                 "ok" if commits else "muted"))
    rows.append(("Plan approvals",
                 str(approvals),
                 "ok" if approvals else "muted"))
    return MetricsSection(key="throughput", title="Throughput (24h)", rows=rows)


def _failure_section(day_events: list) -> MetricsSection:
    """Section 4 — failure counts over the last 24h."""
    def _count(pred) -> int:
        return sum(1 for e in day_events if pred(getattr(e, "event_type", "")))

    state_drift = _count(lambda k: k == "state_drift")
    persona_swap = _count(lambda k: k == "persona_swap_detected" or "persona_swap" in (k or ""))
    reprompts = _count(lambda k: k in ("worker_reprompt", "reprompt", "worker_turn_end_reprompt"))
    no_session = _count(lambda k: "no_session" in (k or ""))
    probe_fail = _count(lambda k: "provider_probe" in (k or "") and "fail" in (k or ""))

    rows: list[tuple[str, str, str]] = []
    rows.append(("state_drift", str(state_drift), "alert" if state_drift else "ok"))
    rows.append(("persona_swap_detected", str(persona_swap), "alert" if persona_swap else "ok"))
    rows.append(("worker reprompts", str(reprompts), "warn" if reprompts else "ok"))
    rows.append(("no_session alerts", str(no_session), "alert" if no_session else "ok"))
    rows.append(("Provider probe failures", str(probe_fail), "warn" if probe_fail else "ok"))
    return MetricsSection(key="failures", title="Failures (24h)", rows=rows)


def _scheduler_section(store) -> MetricsSection:
    """Section 5 — last fire-at + staleness flag per scheduled handler.

    Pulls the last 500 events from the store and groups them by the
    scheduled-job "subject" (captured in the summary payload via the
    inline scheduler). We treat each distinct ``kind`` as a handler
    row. Staleness is a 2× cadence rule: if more than 2h elapsed on
    something expected hourly, flag it red. Cadence is inferred from
    the gap between the most recent two firings of the same kind.
    """
    from datetime import UTC, datetime
    rows: list[tuple[str, str, str]] = []
    try:
        events = store.recent_events(limit=500)
    except Exception:  # noqa: BLE001
        events = []

    # Bucket scheduler events by subject (kind). We read the subject
    # out of the activity-summary JSON emitted by InlineSchedulerBackend
    # when available; fall back to a regex on the message text so tests
    # and older rows still group.
    import json as _json
    import re as _re
    bucket: dict[str, list[str]] = {}
    for e in events:
        if getattr(e, "session_name", "") != "scheduler":
            continue
        if getattr(e, "event_type", "") != "ran":
            continue
        message = getattr(e, "message", "") or ""
        subject: str | None = None
        try:
            payload = _json.loads(message)
            if isinstance(payload, dict):
                subject = payload.get("subject") or payload.get("kind")
        except (ValueError, TypeError):
            pass
        if not subject:
            match = _re.search(r"Ran scheduled job ([A-Za-z0-9_.:-]+)", message)
            if match:
                subject = match.group(1)
        if not subject:
            continue
        bucket.setdefault(subject, []).append(getattr(e, "created_at", "") or "")

    if not bucket:
        # Empty value lets the renderer drop the ``:`` separator —
        # avoids ``(no scheduled runs recorded): —`` reading as a
        # broken row.
        rows.append(("No scheduled jobs have run yet.", "", "muted"))
        return MetricsSection(key="schedulers", title="Schedulers", rows=rows)

    now = datetime.now(UTC)
    for kind, timestamps in sorted(bucket.items()):
        timestamps = [t for t in timestamps if t]
        if not timestamps:
            continue
        timestamps.sort(reverse=True)
        latest_raw = timestamps[0]
        try:
            latest = datetime.fromisoformat(latest_raw)
            if latest.tzinfo is None:
                from datetime import UTC as _UTC
                latest = latest.replace(tzinfo=_UTC)
            age_s = max(0, int((now - latest).total_seconds()))
        except (TypeError, ValueError):
            age_s = 0
        # Cadence guess from the gap between the two most recent fires.
        cadence_s = None
        if len(timestamps) >= 2:
            try:
                prev = datetime.fromisoformat(timestamps[1])
                if prev.tzinfo is None:
                    from datetime import UTC as _UTC
                    prev = prev.replace(tzinfo=_UTC)
                cadence_s = max(0, int((latest - prev).total_seconds()))
            except (TypeError, ValueError):
                cadence_s = None
        if age_s < 60:
            age_label = "just now"
        elif age_s < 3600:
            age_label = f"{age_s // 60}m ago"
        elif age_s < 86400:
            age_label = f"{age_s // 3600}h ago"
        else:
            age_label = f"{age_s // 86400}d ago"
        # Stale if 2× cadence — or >2h idle when cadence is unknown.
        threshold = cadence_s * 2 if cadence_s else 2 * 3600
        tone = "alert" if age_s > threshold else "ok"
        rows.append((kind, age_label, tone))
    return MetricsSection(key="schedulers", title="Schedulers", rows=rows)


def _gather_metrics_snapshot(config) -> MetricsSnapshot:
    """Build a :class:`MetricsSnapshot` from the live system state.

    Best-effort everywhere — one subsystem failure leaves the other
    sections intact. Safe to call on the UI thread for small configs
    (< 50 projects); callers that render on an interval should hop to a
    background thread (the ``PollyMetricsApp`` screen does).
    """
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    captured_at = now.isoformat()

    # Fleet — workers + task counts across every project.
    try:
        roster_rows = _gather_worker_roster(config)
    except Exception:  # noqa: BLE001
        roster_rows = []
    total_counts: dict[str, int] = {}
    for project_key, project in getattr(config, "projects", {}).items():
        try:
            _partitioned, counts = _dashboard_project_tasks(
                project_key, project.path,
            )
        except Exception:  # noqa: BLE001
            counts = {}
        for status, n in counts.items():
            total_counts[status] = total_counts.get(status, 0) + int(n)
    try:
        inbox_breakdown = _inbox_breakdown(config)
    except Exception:  # noqa: BLE001
        inbox_breakdown = {"unread": 0, "plan_review": 0, "blocking_question": 0}
    fleet = _fleet_section(config, roster_rows, total_counts, inbox_breakdown)

    # Resources.
    try:
        resources = _resource_section(config)
    except Exception:  # noqa: BLE001
        resources = MetricsSection(
            key="resources", title="Resources",
            rows=[("error", "resources unavailable", "alert")],
        )

    # Throughput + failures share a single 24h-event read.
    store = None
    try:
        state_db = getattr(getattr(config, "project", None), "state_db", None)
        if state_db is not None:
            from pollypm.storage.state import StateStore
            store = StateStore(Path(state_db), readonly=True)
    except Exception:  # noqa: BLE001
        store = None

    if store is None:
        throughput = MetricsSection(
            key="throughput", title="Throughput (24h)",
            rows=[("error", "state store unavailable", "alert")],
        )
        failures = MetricsSection(
            key="failures", title="Failures (24h)", rows=[],
        )
        schedulers = MetricsSection(
            key="schedulers", title="Schedulers", rows=[],
        )
    else:
        try:
            day_events = _metrics_24h_events(store, now=now)
        except Exception:  # noqa: BLE001
            day_events = []
        try:
            throughput = _throughput_section(day_events)
        except Exception:  # noqa: BLE001
            throughput = MetricsSection(
                key="throughput", title="Throughput (24h)", rows=[],
            )
        try:
            failures = _failure_section(day_events)
        except Exception:  # noqa: BLE001
            failures = MetricsSection(
                key="failures", title="Failures (24h)", rows=[],
            )
        try:
            schedulers = _scheduler_section(store)
        except Exception:  # noqa: BLE001
            schedulers = MetricsSection(
                key="schedulers", title="Schedulers", rows=[],
            )
        try:
            store.close()
        except Exception:  # noqa: BLE001
            pass

    return MetricsSnapshot(
        captured_at=captured_at,
        fleet=fleet,
        resources=resources,
        throughput=throughput,
        failures=failures,
        schedulers=schedulers,
    )


def _register_metrics_rail_item(registry, router) -> None:
    """Add the ``top.Metrics`` rail row if not already registered.

    Kept next to the worker-roster registration so both observability
    rows sit at the top of the rail. Safe to call repeatedly — the
    registry dedupes on ``(plugin_name, section, label)``.
    """
    try:
        from pollypm.plugin_api.v1 import RailItemRegistration, PanelSpec
    except Exception:  # noqa: BLE001
        return

    def _state(_ctx) -> str:
        return "watch"

    def _handler(ctx):
        try:
            router.route_selected("metrics")
        except Exception:  # noqa: BLE001
            pass
        return PanelSpec(widget=None, focus_hint="metrics")

    reg = RailItemRegistration(
        plugin_name="cockpit_metrics",
        section="top",
        index=28,  # after Workers (25), before Projects (30+)
        label="Metrics",
        handler=_handler,
        key="metrics",
        state_provider=_state,
    )
    try:
        registry.add(reg)
    except Exception:  # noqa: BLE001
        pass


def _render_metrics_panel(config_path: Path) -> str:
    """Fallback text rendering of the metrics snapshot — used by
    ``build_cockpit_detail`` when the right pane hasn't launched the
    Textual app yet. Mirrors the worker-roster fallback.
    """
    try:
        config = load_config(config_path)
    except Exception as exc:  # noqa: BLE001
        return f"Metrics\n\nError loading config: {exc}"
    try:
        snap = _gather_metrics_snapshot(config)
    except Exception as exc:  # noqa: BLE001
        return f"Metrics\n\nError gathering metrics: {exc}"
    lines: list[str] = ["Metrics", ""]
    for section in snap.sections():
        lines.append(section.title)
        if not section.rows:
            lines.append("  (no data)")
            lines.append("")
            continue
        for label, value, _tone in section.rows:
            lines.append(f"  {label}: {value}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command palette (``:``) — see issue brief 2026-04-17.
#
# A thin data layer that the Textual ``CommandPaletteModal`` in
# :mod:`pollypm.cockpit_ui` renders. Keeping the registry out of the UI
# layer lets tests exercise the command list (filtering, fuzzy search,
# per-project commands) without having to spin up a full Textual Pilot.
# Every entry is a plain dataclass — the dispatch happens via a ``tag``
# string the host App interprets. This avoids coupling the registry to
# any particular App class.
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class PaletteCommand:
    """A single row in the ``:`` command palette.

    ``tag`` is a dotted string the host :class:`App` interprets in
    :meth:`dispatch_palette_command`. Commands are kept as inert data
    here so the registry can be built+filtered in tests without
    requiring a running Textual app.
    """

    title: str
    subtitle: str
    category: str
    keybind: str | None
    tag: str

    def haystack(self) -> str:
        """Lowercased search string covering title + subtitle + category."""
        return f"{self.title} {self.subtitle} {self.category}".lower()


def _fuzzy_subsequence_score(needle: str, haystack: str) -> int | None:
    """Return a match score if ``needle`` is a subsequence of ``haystack``.

    Lower score is a tighter match. Adjacent characters score better than
    scattered ones — this mirrors VS Code / Raycast's "close letters
    win" feel without any real dependency on a fuzzy library. Returns
    ``None`` when ``needle`` isn't a subsequence at all.
    """
    if not needle:
        return 0
    needle = needle.lower()
    haystack = haystack.lower()
    # Substring match always wins — the earliest exact substring gets
    # the best (lowest) score.
    idx = haystack.find(needle)
    if idx != -1:
        return idx  # closer to start = better
    # Fall back to subsequence: walk both strings in lockstep.
    score = 0
    last_pos = -1
    h_i = 0
    for ch in needle:
        while h_i < len(haystack) and haystack[h_i] != ch:
            h_i += 1
        if h_i >= len(haystack):
            return None
        gap = h_i - last_pos - 1
        score += 1000 + gap  # subsequence baseline > any substring score
        last_pos = h_i
        h_i += 1
    return score


def filter_palette_commands(
    commands: list[PaletteCommand], query: str,
) -> list[PaletteCommand]:
    """Return matching commands ordered by fuzzy score, then title."""
    query = (query or "").strip()
    if not query:
        return list(commands)
    scored: list[tuple[int, str, PaletteCommand]] = []
    for cmd in commands:
        score = _fuzzy_subsequence_score(query, cmd.haystack())
        if score is None:
            continue
        scored.append((score, cmd.title.lower(), cmd))
    scored.sort(key=lambda item: (item[0], item[1]))
    return [cmd for _score, _title, cmd in scored]


def build_palette_commands(
    config_path: Path,
    *,
    current_project: str | None = None,
) -> list[PaletteCommand]:
    """Return the default global command set for the cockpit palette.

    ``current_project`` is the project key the caller is currently
    viewing (if any) — used to prefill project-scoped commands like
    "Create task". Projects are read from the loaded config; if the
    config can't be loaded we still return the static commands so the
    palette never shows an empty list.
    """
    commands: list[PaletteCommand] = []

    # Navigation — the six top-level cockpit views.
    commands.append(PaletteCommand(
        title="Go to Inbox",
        subtitle="Open the cockpit inbox",
        category="Navigation",
        keybind=None,
        tag="nav.inbox",
    ))
    commands.append(PaletteCommand(
        title="Go to Workers",
        subtitle="Open the worker roster",
        category="Navigation",
        keybind=None,
        tag="nav.workers",
    ))
    commands.append(PaletteCommand(
        title="Go to Activity",
        subtitle="Open the activity feed",
        category="Navigation",
        keybind=None,
        tag="nav.activity",
    ))
    commands.append(PaletteCommand(
        title="Go to Metrics",
        subtitle="Open the observability metrics screen",
        category="Navigation",
        keybind=None,
        tag="nav.metrics",
    ))
    commands.append(PaletteCommand(
        title="Go to Settings",
        subtitle="Open cockpit settings",
        category="Navigation",
        keybind="s",
        tag="nav.settings",
    ))
    commands.append(PaletteCommand(
        title="Go to Dashboard",
        subtitle="Jump to the main cockpit rail",
        category="Navigation",
        keybind=None,
        tag="nav.dashboard",
    ))

    # Per-project navigation + task commands.
    try:
        config = load_config(config_path)
        projects = getattr(config, "projects", {}) or {}
    except Exception:  # noqa: BLE001
        projects = {}
    for project_key, project in projects.items():
        name = getattr(project, "name", None) or project_key
        commands.append(PaletteCommand(
            title=f"Go to project: {name}",
            subtitle=f"Open the {name} dashboard",
            category="Navigation",
            keybind=None,
            tag=f"nav.project:{project_key}",
        ))
        commands.append(PaletteCommand(
            title=f"Create task in {name}",
            subtitle="Draft a new task in this project",
            category="Task",
            keybind=None,
            tag=f"task.create:{project_key}",
        ))
        commands.append(PaletteCommand(
            title=f"Queue next task in {name}",
            subtitle=f"Run pm task next --project {project_key}",
            category="Task",
            keybind=None,
            tag=f"task.queue_next:{project_key}",
        ))

    # Inbox commands.
    commands.append(PaletteCommand(
        title="Run pm notify",
        subtitle="Send a notification into the inbox",
        category="Inbox",
        keybind=None,
        tag="inbox.notify",
    ))
    commands.append(PaletteCommand(
        title="Archive all read inbox items",
        subtitle="Mark every already-read message done",
        category="Inbox",
        keybind=None,
        tag="inbox.archive_read",
    ))

    # Session / app-level.
    commands.append(PaletteCommand(
        title="Refresh data",
        subtitle="Re-read state and repaint the current screen",
        category="Session",
        keybind="r",
        tag="session.refresh",
    ))
    commands.append(PaletteCommand(
        title="Restart cockpit",
        subtitle="Exit and reload the cockpit app",
        category="Session",
        keybind=None,
        tag="session.restart",
    ))
    commands.append(PaletteCommand(
        title="Show keyboard shortcuts",
        subtitle="Display the current screen's keybindings",
        category="Session",
        keybind="?",
        tag="session.shortcuts",
    ))

    # System.
    commands.append(PaletteCommand(
        title="Run pm doctor",
        subtitle="Stream doctor checks into the palette",
        category="System",
        keybind=None,
        tag="system.doctor",
    ))
    commands.append(PaletteCommand(
        title="Open pollypm.toml in editor",
        subtitle=str(config_path),
        category="System",
        keybind=None,
        tag="system.edit_config",
    ))

    # Let the caller prioritise current-project commands visually. We
    # keep the list stable (no re-ordering mid-render); a current-project
    # hint just means the "Create task in <current>" entry sits above
    # its siblings.
    if current_project is not None:
        preferred: list[PaletteCommand] = []
        rest: list[PaletteCommand] = []
        for cmd in commands:
            if cmd.tag.endswith(f":{current_project}"):
                preferred.append(cmd)
            else:
                rest.append(cmd)
        commands = preferred + rest

    return commands
