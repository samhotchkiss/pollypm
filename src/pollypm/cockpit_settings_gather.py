"""Settings-data gather helpers extracted from ``cockpit_ui``.

Contract:
- Inputs: ``_gather_settings_data(config_path, *, service, account_statuses)``
  reads the on-disk config, the cached account-usage snapshot, the
  per-project state DBs (for recent tasks), the plugin host, and the
  role registry. The other helpers are small pure formatters.
- Outputs: a populated :class:`SettingsData` instance (no Textual
  widgets are created), plus row dicts / strings consumed by
  ``PollySettingsPaneApp``.
- Side effects: ``_settings_dir_size`` shells out to ``du -sk`` with a
  short timeout. ``_collect_recent_tasks_by_account`` opens per-project
  SQLite work-services read-only and closes them via context manager.
- Invariants: this module owns *data-gather* helpers only. It must not
  import Textual widgets or ``PollySettingsPaneApp``. The names
  ``_role_label``, ``_role_assignment_summary``, ``_role_source_text``,
  ``_role_source_style``, ``_resolved_assignment_from_row``,
  ``_build_settings_role_rows``, ``_settings_status_dot``,
  ``_settings_dir_size``, ``_humanize_bytes``, ``_budget_level``,
  ``_budget_fields_from_cached_usage``, ``_format_recent_task``,
  ``_collect_recent_tasks_by_account``,
  ``_settings_session_refs_by_account``, and ``_gather_settings_data``
  are re-exported from ``pollypm.cockpit_ui`` for back-compat (see
  #1354).
- Allowed dependencies: stdlib, ``pollypm.cockpit_markup`` (escape
  helpers), ``pollypm.cockpit_formatting`` (relative-age formatter),
  ``pollypm.cockpit_settings_data`` (``SettingsData``),
  ``pollypm.cockpit_settings_history`` (history helpers),
  ``pollypm.cockpit_settings_projects`` (``collect_settings_projects``),
  ``pollypm.account_usage_sampler``, ``pollypm.config``,
  ``pollypm.model_registry``, ``pollypm.models``, and
  ``pollypm.role_routing``.

First slice of the cockpit_ui.py god-module split tracked by #1354.
No behaviour changes — this is a pure module-boundary move.
"""

from __future__ import annotations

import re as _re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from pollypm.account_usage_sampler import load_cached_account_usage
from pollypm.cockpit_formatting import format_relative_age as _format_relative_age
from pollypm.cockpit_markup import _escape
from pollypm.cockpit_settings_data import SettingsData
from pollypm.cockpit_settings_history import (
    history_rationale_for_account,
    history_rationale_for_project,
    load_settings_history,
)
from pollypm.cockpit_settings_projects import collect_settings_projects
from pollypm.config import load_config
from pollypm.model_registry import advisories_for, load_registry, resolve_alias
from pollypm.models import ModelAssignment
from pollypm.role_routing import resolve_role_assignment

if TYPE_CHECKING:
    from pollypm.service_api import PollyPMService  # noqa: F401


_GLOBAL_SETTINGS_ROLE_KEYS = (
    "operator_pm",
    "architect",
    "worker",
    "reviewer",
)

_ROLE_LABELS = {
    "operator_pm": "Operator PM",
    "architect": "Architect",
    "worker": "Worker",
    "reviewer": "Reviewer",
}


def _iso_sort_weight(iso: str) -> int:
    """Coerce an ISO timestamp to a comparable integer key.

    Local private copy of ``cockpit_ui._iso_sort_weight`` so this module
    has no back-import to the god-module being split. The public
    ``cockpit_ui._iso_sort_weight`` retains its original definition for
    inbox-row sorting; the two implementations must stay in sync.
    """
    try:
        from datetime import datetime as _dt
        return int(_dt.fromisoformat(iso).timestamp())
    except (ValueError, TypeError):
        return 0


def _role_label(role: str) -> str:
    return _ROLE_LABELS.get(role, role.replace("_", " ").title())


def _role_assignment_summary(
    assignment: ModelAssignment | None,
    *,
    registry,
    inherited: bool = False,
) -> str:
    if assignment is None:
        return "inherit" if inherited else "fallback"
    if assignment.alias is not None:
        if resolve_alias(assignment.alias, registry=registry) is None:
            return f"alias:{assignment.alias} (missing)"
        return f"alias:{assignment.alias}"
    return f"{assignment.provider}/{assignment.model}"


def _role_source_text(source: str) -> str:
    if source == "global":
        return "global"
    if source == "project":
        return "project override"
    if source == "fallback":
        return "fallback"
    return source


def _role_source_style(source: str) -> str:
    return {
        "project": "#5b8aff",
        "global": "#3ddc84",
        "fallback": "#97a6b2",
    }.get(source, "#97a6b2")


def _resolved_assignment_from_row(row: dict) -> ModelAssignment:
    alias = row.get("resolved_alias")
    if isinstance(alias, str) and alias:
        return ModelAssignment(alias=alias)
    return ModelAssignment(
        provider=str(row.get("resolved_provider") or ""),
        model=str(row.get("resolved_model") or ""),
    )


def _build_settings_role_rows(config, registry) -> list[dict]:
    rows: list[dict] = []
    assignments = getattr(getattr(config, "pollypm", None), "role_assignments", {}) or {}
    for role in _GLOBAL_SETTINGS_ROLE_KEYS:
        configured = assignments.get(role)
        resolved = resolve_role_assignment(
            role,
            config=config,
            registry=registry,
        )
        advisories = advisories_for(
            role,
            ModelAssignment(alias=resolved.alias)
            if resolved.alias is not None
            else ModelAssignment(provider=resolved.provider, model=resolved.model),
            registry=registry,
        )
        rows.append(
            {
                "role": role,
                "label": _role_label(role),
                "configured_summary": _role_assignment_summary(
                    configured,
                    registry=registry,
                ),
                "configured_alias": (
                    configured.alias if configured is not None else None
                ),
                "configured_provider": (
                    configured.provider if configured is not None else None
                ),
                "configured_model": (
                    configured.model if configured is not None else None
                ),
                "configured_kind": (
                    "alias"
                    if configured is not None and configured.alias is not None
                    else "custom"
                    if configured is not None
                    else "fallback"
                ),
                "configured_missing_alias": bool(
                    configured is not None
                    and configured.alias is not None
                    and resolve_alias(configured.alias, registry=registry) is None
                ),
                "resolved_provider": resolved.provider,
                "resolved_model": resolved.model,
                "resolved_alias": resolved.alias,
                "resolved_summary": f"{resolved.provider}/{resolved.model}",
                "source": resolved.source,
                "source_label": _role_source_text(resolved.source),
                "advisories": advisories,
                "has_override": configured is not None,
            }
        )
    return rows


def _settings_status_dot(health: str, logged_in: bool) -> tuple[str, str]:
    if not logged_in:
        return ("●", "#ff5f6d")
    h = (health or "").lower()
    if h in ("capacity-exhausted", "auth-broken", "signed-out"):
        return ("●", "#ff5f6d")
    if h in ("capacity-low", "warning", "degraded"):
        return ("●", "#f0c45a")
    if h == "healthy":
        return ("●", "#3ddc84")
    return ("●", "#6b7a88")


def _settings_dir_size(path: Path) -> int:
    try:
        result = subprocess.run(
            ["du", "-sk", str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=0.75,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return -1
    if result.returncode != 0:
        return -1
    try:
        kib = int((result.stdout or "").strip().split()[0])
    except (IndexError, ValueError):
        return -1
    return kib * 1024


def _humanize_bytes(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(n)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{n} B"


def _budget_level(summary: str) -> str:
    match = _re.search(r"(\d{1,3})\s*%", summary or "")
    if not match:
        lowered = (summary or "").lower()
        if any(token in lowered for token in ("offline", "unavailable", "error")):
            return "error"
        return "unknown"
    pct = int(match.group(1))
    if pct <= 20:
        return "error"
    if pct <= 50:
        return "warn"
    return "ok"


def _budget_fields_from_cached_usage(record: object | None) -> tuple[str, str, str]:
    if record is None:
        return ("budget unavailable", "unknown", "No cached usage yet.")
    used_pct = getattr(record, "used_pct", None)
    remaining_pct = getattr(record, "remaining_pct", None)
    usage_summary = getattr(record, "usage_summary", "") or "usage unavailable"
    if used_pct is not None and remaining_pct is not None:
        budget_summary = f"{used_pct}% used / {remaining_pct}% left"
    elif remaining_pct is not None:
        budget_summary = f"{remaining_pct}% left"
    else:
        budget_summary = usage_summary
    updated_at = getattr(record, "updated_at", "") or ""
    if updated_at:
        budget_summary = f"{budget_summary} · updated {updated_at}"
    return (budget_summary, _budget_level(budget_summary), "Cached from account_usage")


def _format_recent_task(task: object) -> str:
    task_id = str(getattr(task, "task_id", ""))
    title = str(getattr(task, "title", "") or "(untitled)")
    project = str(getattr(task, "project", "") or "")
    status_obj = getattr(task, "work_status", getattr(task, "status", ""))
    status = getattr(status_obj, "value", status_obj)
    bits = [f"[b]{_escape(task_id)}[/b]"]
    if project:
        bits.append(f"[dim]{_escape(project)}[/dim]")
    if status:
        bits.append(f"[dim]{_escape(str(status))}[/dim]")
    bits.append(f"[dim]{_escape(title)}[/dim]")
    return " · ".join(bits)


def _collect_recent_tasks_by_account(
    config,
    account_statuses: list,
    *,
    max_per_account: int = 3,
) -> dict[str, list[dict[str, str]]]:
    recent: dict[str, list[dict[str, str]]] = {
        str(getattr(status, "key", "")): [] for status in account_statuses
    }
    projects = getattr(config, "projects", {}) or {}
    for project_key, project in projects.items():
        path = getattr(project, "path", None)
        if path is None:
            continue
        project_path = Path(path)
        if not project_path.exists():
            continue
        db_path = project_path / ".pollypm" / "state.db"
        if not db_path.exists():
            continue
        try:
            from pollypm.work import create_work_service

            with create_work_service(
                db_path=db_path, project_path=project_path, config=config,
            ) as svc:
                for status in account_statuses:
                    key = str(getattr(status, "key", ""))
                    if not key:
                        continue
                    try:
                        tasks = svc.list_tasks(assignee=key, limit=max_per_account)
                    except Exception:  # noqa: BLE001
                        continue
                    for task in tasks:
                        recent[key].append(
                            {
                                "task_id": str(getattr(task, "task_id", "")),
                                "project": str(getattr(task, "project", project_key) or project_key),
                                "title": str(getattr(task, "title", "") or "(untitled)"),
                                "work_status": getattr(
                                    getattr(task, "work_status", None),
                                    "value",
                                    str(getattr(task, "work_status", "")),
                                ),
                                "updated_at": (
                                    getattr(task, "updated_at", None).isoformat()
                                    if hasattr(getattr(task, "updated_at", None), "isoformat")
                                    else str(getattr(task, "updated_at", "") or "")
                                ),
                            }
                        )
        except Exception:  # noqa: BLE001
            continue
    for key, rows in recent.items():
        rows.sort(
            key=lambda row: (_iso_sort_weight(row["updated_at"]), row["task_id"]),
            reverse=True,
        )
        recent[key] = rows[:max_per_account]
    return recent


def _settings_session_refs_by_account(config) -> dict[str, list[dict[str, object]]]:
    """Return configured session references grouped by pinned account."""
    refs: dict[str, list[dict[str, object]]] = {}
    sessions = getattr(config, "sessions", {}) or {}
    for session_name, session in sorted(sessions.items()):
        account_key = str(getattr(session, "account", "") or "")
        if not account_key:
            continue
        provider = getattr(session, "provider", "")
        provider_value = getattr(provider, "value", str(provider or ""))
        refs.setdefault(account_key, []).append(
            {
                "name": str(getattr(session, "name", session_name) or session_name),
                "role": str(getattr(session, "role", "") or ""),
                "project": str(getattr(session, "project", "") or ""),
                "provider": provider_value,
                "enabled": bool(getattr(session, "enabled", True)),
                "window_name": str(getattr(session, "window_name", "") or ""),
            }
        )
    return refs


def _gather_account_statuses(
    config_path: Path,
    *,
    service: "PollyPMService | None",
    account_statuses: list | None,
    errors: list[str],
) -> list:
    """Return raw account-status records, deferring to the service when not provided."""
    if account_statuses is not None:
        return list(account_statuses)
    if service is None:
        from pollypm.service_api import PollyPMService
        service = PollyPMService(config_path)
    try:
        list_cached = getattr(service, "list_cached_account_statuses", None)
        if callable(list_cached):
            return list(list_cached())
        return list(service.list_account_statuses())
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Accounts unavailable: {exc}")
        return []


def _build_account_rows(
    *,
    config_path: Path,
    config,
    account_statuses: list,
    history: list,
    session_refs_by_account: dict,
    cached_usages: dict,
    ctrl: str,
    fo_list: list,
) -> list[dict]:
    """Transform account-status records into cockpit-settings dict rows."""
    accounts: list[dict] = []
    for idx, status in enumerate(account_statuses):
        provider = getattr(status, "provider", None)
        provider_name = (
            getattr(provider, "value", "") if provider is not None else ""
        )
        home = getattr(status, "home", None)
        failover_pos = (
            (fo_list.index(status.key) + 1) if status.key in fo_list else None
        )
        usage_record = cached_usages.get(status.key)
        budget_summary, budget_level, budget_rationale = _budget_fields_from_cached_usage(
            usage_record,
        )
        accounts.append(
            {
                "key": status.key,
                "email": getattr(status, "email", "") or "-",
                "provider": provider_name,
                "home": str(home) if home else "",
                "is_controller": status.key == ctrl,
                "failover_pos": failover_pos,
                "logged_in": bool(getattr(status, "logged_in", False)),
                "health": getattr(status, "health", "") or "",
                "plan": getattr(status, "plan", "") or "",
                "usage_summary": getattr(status, "usage_summary", "") or "",
                "usage_raw_text": getattr(status, "usage_raw_text", "") or "",
                "usage_updated_at": getattr(status, "usage_updated_at", "") or "",
                "used_pct": getattr(status, "used_pct", None),
                "remaining_pct": getattr(status, "remaining_pct", None),
                "reset_at": getattr(status, "reset_at", "") or "",
                "period_label": getattr(status, "period_label", "") or "",
                "reason": getattr(status, "reason", "") or "",
                "available_at": getattr(status, "available_at", "") or "",
                "access_expires_at": getattr(status, "access_expires_at", "") or "",
                "isolation_status": getattr(status, "isolation_status", "") or "",
                "auth_storage": getattr(status, "auth_storage", "") or "",
                "budget_summary": budget_summary,
                "budget_level": budget_level,
                "rationale": history_rationale_for_account(
                    status.key,
                    entries=history,
                    default_account=ctrl or None,
                )
                or (
                    "Provider budgets come from the cached account_usage sampler so the UI stays offline-safe."
                ),
                "budget_rationale": budget_rationale,
                "session_refs": session_refs_by_account.get(status.key, []),
                "status_obj": status,
                "index": idx,
            }
        )
    return accounts


def _build_settings_project_rows(config, history: list) -> list[dict]:
    """Return cockpit-settings project rows with rationale overlay."""
    if config is None:
        return []
    projects = collect_settings_projects(
        config,
        format_relative_age=_format_relative_age,
    )
    for project in projects:
        project.setdefault(
            "rationale",
            "Tracked projects stay visible in the cockpit and feed task counts.",
        )
        history_rationale = history_rationale_for_project(
            project["key"],
            entries=history,
        )
        if history_rationale:
            project["rationale"] = history_rationale
    return projects


def _build_heartbeat_rows(pp) -> list[tuple[str, str]]:
    """Return the heartbeat / scheduler tuple list for the settings pane."""
    if pp is None:
        return []
    failover_accounts = getattr(pp, "failover_accounts", []) or []
    return [
        ("Controller account", getattr(pp, "controller_account", "") or "-"),
        ("Failover enabled", "yes" if getattr(pp, "failover_enabled", False) else "no"),
        (
            "Failover order",
            ", ".join(failover_accounts) if failover_accounts else "none",
        ),
        ("Lease timeout", f"{getattr(pp, 'lease_timeout_minutes', 30)} min"),
        ("Heartbeat backend", getattr(pp, "heartbeat_backend", "") or "-"),
        ("Scheduler backend", getattr(pp, "scheduler_backend", "") or "-"),
        (
            "Open permissions",
            "on" if getattr(pp, "open_permissions_by_default", False) else "off",
        ),
        ("Timezone", getattr(pp, "timezone", "") or "(auto-detect)"),
    ]


def _build_plugin_rows(
    config_path: Path, config, errors: list[str],
) -> list[dict]:
    """Return the plugin status rows (loaded, disabled, load-failed)."""
    plugins: list[dict] = []
    try:
        from pollypm.plugin_host import ExtensionHost
        host = ExtensionHost(
            config_path.parent,
            disabled=tuple(
                getattr(getattr(config, "plugins", None), "disabled", ()) or ()
            ),
        )
        loaded = host.plugins()
        degraded = host.degraded_plugins
        for name, plugin in sorted(loaded.items()):
            source = host.plugin_source(name) or "-"
            status = "degraded" if name in degraded else "loaded"
            plugins.append(
                {
                    "name": name,
                    "version": getattr(plugin, "version", ""),
                    "description": getattr(plugin, "description", "") or "",
                    "source": source,
                    "status": status,
                    "degraded_reason": degraded.get(name, ""),
                }
            )
        for name, record in sorted(host.disabled_plugins.items()):
            plugins.append(
                {
                    "name": name,
                    "version": "",
                    "description": "",
                    "source": getattr(record, "source", "-") or "-",
                    "status": "disabled",
                    "degraded_reason": getattr(record, "reason", "") or "",
                }
            )
        # Surface plugin load errors in the settings panel so a
        # silently-broken plugin (#960) is discoverable from inside
        # the cockpit too — not just at boot. Record one entry per
        # failing plugin (collapsing repeat errors for the same name).
        seen_load_failures: set[str] = set()
        for record in host.load_errors():
            plugin_name = record.plugin or "<host>"
            if plugin_name in loaded or plugin_name in host.disabled_plugins:
                # Already represented above; the load_errors entry is
                # noise relative to the existing row.
                continue
            if plugin_name in seen_load_failures:
                continue
            seen_load_failures.add(plugin_name)
            plugins.append(
                {
                    "name": plugin_name,
                    "version": "",
                    "description": "",
                    "source": "-",
                    "status": "load_failed",
                    "degraded_reason": record.message,
                }
            )
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Plugin host unavailable: {exc}")
    return plugins


def _build_planner_rows(config) -> list[tuple[str, str]]:
    pl = getattr(config, "planner", None) if config is not None else None
    if pl is None:
        return []
    return [
        (
            "Auto-fire on project created",
            "yes" if getattr(pl, "auto_on_project_created", False) else "no",
        ),
        ("Enforce plan gate", "yes" if getattr(pl, "enforce_plan", False) else "no"),
        ("Plan directory", getattr(pl, "plan_dir", "") or "docs/plan"),
    ]


def _build_inbox_about_sections(
    config, config_path: Path,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Return (inbox_section, about_section) tuples for the settings pane."""
    inbox_section: list[tuple[str, str]] = []
    project_settings = (
        getattr(config, "project", None) if config is not None else None
    )
    if project_settings is not None:
        ws = getattr(project_settings, "workspace_root", None)
        if ws is not None:
            inbox_section.append(("Workspace root", str(ws)))
        sdb = getattr(project_settings, "state_db", None)
        if sdb is not None:
            inbox_section.append(("Global state DB", str(sdb)))
        logs = getattr(project_settings, "logs_dir", None)
        if logs is not None:
            inbox_section.append(("Logs directory", str(logs)))

    about_section: list[tuple[str, str]] = []
    try:
        from pollypm import __version__ as _pp_version
    except Exception:  # noqa: BLE001
        _pp_version = "unknown"
    import sys as _sys
    about_section.append(("PollyPM version", _pp_version))
    about_section.append(("Python", _sys.version.split()[0]))
    about_section.append(("Config path", str(config_path)))
    if project_settings is not None:
        sdb = getattr(project_settings, "state_db", None)
        if sdb is not None:
            about_section.append(("State DB", str(sdb)))
    about_section.append(
        (f"Disk usage ({config_path.parent.name}/)", "loading…")
    )
    return inbox_section, about_section


def _gather_settings_data(
    config_path: Path,
    *,
    service: "PollyPMService | None" = None,
    account_statuses: list | None = None,
) -> SettingsData:
    """Build a :class:`SettingsData` snapshot in a single pass.

    All fields are loaded once so the cockpit settings pane can render
    instantly without firing per-tick subprocesses (the source of the
    legacy lag). ``service`` and ``account_statuses`` are injection
    hooks for tests.
    """
    errors: list[str] = []
    try:
        config = load_config(config_path)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Config load failed: {exc}")
        config = None

    account_statuses = _gather_account_statuses(
        config_path,
        service=service,
        account_statuses=account_statuses,
        errors=errors,
    )
    pp = getattr(config, "pollypm", None) if config is not None else None
    ctrl = getattr(pp, "controller_account", "") if pp is not None else ""
    fo_list = (
        list(getattr(pp, "failover_accounts", []) or [])
        if pp is not None else []
    )
    session_refs_by_account = (
        _settings_session_refs_by_account(config)
        if config is not None else {}
    )
    try:
        cached_usages = load_cached_account_usage(config_path) if config is not None else {}
    except Exception:  # noqa: BLE001
        cached_usages = {}
    try:
        history = load_settings_history()
    except Exception:  # noqa: BLE001
        history = []
    accounts = _build_account_rows(
        config_path=config_path,
        config=config,
        account_statuses=account_statuses,
        history=history,
        session_refs_by_account=session_refs_by_account,
        cached_usages=cached_usages,
        ctrl=ctrl,
        fo_list=fo_list,
    )

    projects = _build_settings_project_rows(config, history)

    if config is not None and accounts:
        recent_by_account = _collect_recent_tasks_by_account(
            config,
            account_statuses or [],
        )
        for account in accounts:
            account["recent_tasks"] = recent_by_account.get(account["key"], [])
    else:
        for account in accounts:
            account["recent_tasks"] = []

    roles: list[dict] = []
    if config is not None:
        try:
            registry = load_registry()
            roles = _build_settings_role_rows(config, registry)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Role registry unavailable: {exc}")

    heartbeat = _build_heartbeat_rows(pp)
    plugins = _build_plugin_rows(config_path, config, errors)
    planner = _build_planner_rows(config)
    inbox_section, about_section = _build_inbox_about_sections(
        config, config_path,
    )

    return SettingsData(
        accounts=accounts,
        projects=projects,
        roles=roles,
        heartbeat=heartbeat,
        plugins=plugins,
        planner=planner,
        inbox=inbox_section,
        about=about_section,
        errors=errors,
    )
