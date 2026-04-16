"""Hourly briefing-tick handler.

Registered as ``briefing.tick`` on the job-handler registry, fired every
hour by the roster. The handler itself is cheap: it reads the current
time in the user's timezone, compares to the configured briefing hour,
and checks the persisted ``last_briefing_date`` to dedupe.

If the gates pass, it delegates to ``fire_briefing`` — a function that
will be wired up to real gather/synthesize/emit logic in mb02/mb03/mb04.
For mb01, ``fire_briefing`` is a stub that just returns ``{"fired": True}``
so acceptance tests can verify the gate logic without touching any
data-gathering surface.
"""
from __future__ import annotations

import logging
from datetime import date as _date, datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from pollypm.plugins_builtin.morning_briefing.settings import (
    BriefingSettings,
    load_briefing_settings,
)
from pollypm.plugins_builtin.morning_briefing.state import (
    BriefingState,
    iso_date,
    load_state,
    save_state,
)
from pollypm.tz import get_timezone


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Hook point for mb02/mb03/mb04 — the gather/synthesize/emit pipeline.
# Kept as a module-level callable so tests can monkeypatch it.
# ---------------------------------------------------------------------------


def fire_briefing(
    *,
    project_root: Path,
    base_dir: Path,
    settings: BriefingSettings,
    now_local: datetime,
    state: BriefingState,
    config=None,
) -> dict[str, Any]:
    """Execute the full gather → synthesize → emit path.

    mb02 wired gather; mb03 wires synthesis + quiet-mode detection.
    mb04 adds the inbox write on top. This function returns a summary
    dict that the tick handler surfaces to the job-queue status; the
    full :class:`BriefingDraft` is attached under ``"draft"`` so mb04's
    emitter can pick it up.
    """
    from pollypm.plugins_builtin.morning_briefing.handlers import gather_yesterday as _gy
    from pollypm.plugins_builtin.morning_briefing.handlers import identify_priorities as _ip
    from pollypm.plugins_builtin.morning_briefing.handlers import synthesize as _synth
    from pollypm.plugins_builtin.morning_briefing.state import iso_date, save_state

    if config is None:
        from pollypm.config import DEFAULT_CONFIG_PATH, load_config, resolve_config_path

        config_path = resolve_config_path(DEFAULT_CONFIG_PATH)
        if config_path.exists():
            config = load_config(config_path)

    if config is None:
        return {"fired": False, "reason": "no-config-for-gather"}

    snapshot = _gy.gather_yesterday(config, now_local=now_local, project_root=project_root)
    priorities = _ip.identify_priorities(
        config,
        now_local=now_local,
        priorities_count=settings.priorities_count,
        project_root=project_root,
    )

    # Quiet-mode detection — runs only when yesterday itself was empty
    # (cheap fast-path; no need to scan the full 7-day window every day).
    quiet_mode = False
    if _synth._snapshot_is_quiet(snapshot):
        try:
            quiet_mode = _synth.detect_quiet_mode(
                config,
                now_local=now_local,
                quiet_threshold_days=settings.quiet_mode_after_days,
                project_root=project_root,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("briefing: quiet-mode probe failed: %s", exc)

    if quiet_mode:
        # In quiet mode we only actually fire on Sundays. Other days return
        # "skipped-quiet" so the tick handler doesn't mark last_briefing_date
        # (mb04 interprets this — for mb03 we still set the marker to avoid
        # a thundering-hourly retry).
        if not _synth.is_weekly_quiet_fire_day(now_local):
            return {
                "fired": False,
                "reason": "quiet-mode",
                "date_local": snapshot.date_local,
                "quiet_mode": True,
            }
        draft = _synth.build_quiet_mode_draft(date_local=snapshot.date_local)
        # Record the weekly-fire marker on the persisted state.
        state.last_quiet_weekly_date = iso_date(now_local.date())
        save_state(base_dir, state)
    else:
        draft = _synth.synthesize_briefing(
            config=config,
            snapshot=snapshot,
            priorities=priorities,
            base_dir=base_dir,
            date_local=snapshot.date_local,
            budget_seconds=300,
        )

    return {
        "fired": True,
        "stub": False,
        "quiet_mode": quiet_mode,
        "draft": {
            "date_local": draft.date_local,
            "mode": draft.mode,
            "yesterday": draft.yesterday,
            "priorities": [
                {"title": p.title, "project": p.project, "why": p.why}
                for p in draft.priorities
            ],
            "watch": list(draft.watch),
            "markdown": draft.markdown,
            "meta": dict(draft.meta),
        },
        "yesterday": {
            "date_local": snapshot.date_local,
            "total_commits": snapshot.total_commits(),
            "transitions": len(snapshot.task_transitions),
            "advisor_insights": len(snapshot.advisor_insights),
            "downtime_artifacts": len(snapshot.downtime_artifacts),
        },
        "priorities": {
            "top_tasks": len(priorities.top_tasks),
            "blockers": len(priorities.blockers),
            "awaiting_approval": len(priorities.awaiting_approval),
        },
    }


# ---------------------------------------------------------------------------
# Pure helpers — split out so unit tests can hit each gate independently.
# ---------------------------------------------------------------------------


def _resolve_timezone(settings: BriefingSettings, fallback_config_tz: str = "") -> ZoneInfo:
    """Priority: briefing override → global pollypm.toml → system TZ."""
    if settings.timezone:
        try:
            return ZoneInfo(settings.timezone)
        except Exception:  # noqa: BLE001
            logger.warning(
                "briefing: invalid timezone '%s' in [briefing].timezone; falling back",
                settings.timezone,
            )
    return get_timezone(fallback_config_tz)


def _local_now(settings: BriefingSettings, fallback_config_tz: str = "") -> datetime:
    return datetime.now(_resolve_timezone(settings, fallback_config_tz))


def should_fire(
    *,
    settings: BriefingSettings,
    state: BriefingState,
    now_local: datetime,
) -> tuple[bool, str]:
    """Return (fire?, reason). Reason is a short machine-parseable tag."""
    if not settings.enabled:
        return False, "disabled"
    if now_local.hour != settings.hour:
        return False, "off-hour"
    today = iso_date(now_local.date())
    if state.last_briefing_date == today:
        return False, "already-briefed"
    return True, "ok"


# ---------------------------------------------------------------------------
# Main handler entrypoint
# ---------------------------------------------------------------------------


def briefing_tick_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Hourly briefing-tick handler.

    Payload keys (all optional — sensible defaults otherwise):

    * ``config_path`` — explicit pollypm.toml to load. Defaults to the
      global discovery path.
    * ``project_root`` — overrides the config's project root. Useful for
      tests and per-project installs.
    * ``now_local`` — ISO-8601 (with tzinfo) override; tests only.
    """
    from pollypm.config import DEFAULT_CONFIG_PATH, load_config, resolve_config_path

    config_path_override = payload.get("config_path") if isinstance(payload, dict) else None
    config_path = (
        Path(config_path_override) if config_path_override else resolve_config_path(DEFAULT_CONFIG_PATH)
    )
    if not config_path.exists():
        return {"fired": False, "reason": "no-config", "config_path": str(config_path)}

    try:
        config = load_config(config_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("briefing: failed to load config %s: %s", config_path, exc)
        return {"fired": False, "reason": "config-error", "error": str(exc)}

    settings = load_briefing_settings(config_path)
    fallback_tz = config.pollypm.timezone or ""

    override_now = payload.get("now_local") if isinstance(payload, dict) else None
    if isinstance(override_now, datetime) and override_now.tzinfo is not None:
        now_local = override_now
    elif isinstance(override_now, str) and override_now:
        try:
            candidate = datetime.fromisoformat(override_now)
        except ValueError:
            candidate = None
        if candidate is not None and candidate.tzinfo is not None:
            now_local = candidate
        else:
            now_local = _local_now(settings, fallback_tz)
    else:
        now_local = _local_now(settings, fallback_tz)

    project_root_hint = payload.get("project_root") if isinstance(payload, dict) else None
    if project_root_hint:
        project_root = Path(project_root_hint)
    else:
        project_root = config.project.root_dir

    base_dir = config.project.base_dir

    state = load_state(base_dir)

    fire, reason = should_fire(settings=settings, state=state, now_local=now_local)
    if not fire:
        return {
            "fired": False,
            "reason": reason,
            "local_hour": now_local.hour,
            "today_local": iso_date(now_local.date()),
        }

    try:
        # Call via module attribute so tests can monkeypatch ``fire_briefing``.
        from pollypm.plugins_builtin.morning_briefing.handlers import briefing_tick as _self

        result = _self.fire_briefing(
            project_root=project_root,
            base_dir=base_dir,
            settings=settings,
            now_local=now_local,
            state=state,
            config=config,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("briefing: fire_briefing failed")
        return {
            "fired": False,
            "reason": "fire-error",
            "error": str(exc),
            "local_hour": now_local.hour,
            "today_local": iso_date(now_local.date()),
        }

    today_iso = iso_date(now_local.date())

    # fire_briefing may decline to fire (quiet mode on a non-Sunday, for
    # example). Respect its verdict — don't mark the date done.
    inner_fired = True
    inner_reason = "ok"
    if isinstance(result, dict) and result.get("fired") is False:
        inner_fired = False
        inner_reason = str(result.get("reason") or "declined")

    if not inner_fired:
        summary = {
            "fired": False,
            "reason": inner_reason,
            "local_hour": now_local.hour,
            "today_local": today_iso,
        }
        if isinstance(result, dict):
            summary["result"] = result
        return summary

    state.last_briefing_date = today_iso
    state.last_fire_at = now_local.astimezone().isoformat()
    save_state(base_dir, state)

    summary: dict[str, Any] = {
        "fired": True,
        "reason": "ok",
        "date_local": today_iso,
        "local_hour": now_local.hour,
    }
    if isinstance(result, dict):
        summary["result"] = result
    return summary
