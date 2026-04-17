"""Advisor inbox auto-close sweep.

Runs every 12 hours (spec §7). Closes advisor_insight inbox entries
that have had no user action for 7 days. Real logic landed in ad05
on top of the ad01 stub.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


def advisor_autoclose_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Close advisor_insight entries older than 7 days.

    Payload keys (all optional — sensible defaults otherwise):

    * ``config_path`` — explicit pollypm.toml. Defaults to the global
      discovery path.
    * ``max_age_days`` — override the 7-day threshold (for time-travel
      tests).
    * ``now_utc`` — ISO-8601 override for deterministic tests.
    """
    from datetime import datetime, UTC

    from pollypm.config import DEFAULT_CONFIG_PATH, load_config, resolve_config_path
    from pollypm.plugins_builtin.advisor.inbox import (
        DEFAULT_AUTO_CLOSE_DAYS,
        auto_close_expired,
    )

    config_path_override = payload.get("config_path") if isinstance(payload, dict) else None
    config_path = (
        Path(config_path_override) if config_path_override
        else resolve_config_path(DEFAULT_CONFIG_PATH)
    )
    if not config_path.exists():
        return {"fired": False, "reason": "no-config"}

    try:
        config = load_config(config_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("advisor.autoclose: load_config failed: %s", exc)
        return {"fired": False, "reason": "config-error", "error": str(exc)}

    base_dir: Path = Path(config.project.base_dir)

    max_age = payload.get("max_age_days") if isinstance(payload, dict) else None
    if not isinstance(max_age, (int, float)) or max_age <= 0:
        max_age = DEFAULT_AUTO_CLOSE_DAYS

    now_override = payload.get("now_utc") if isinstance(payload, dict) else None
    if isinstance(now_override, datetime):
        now_utc = now_override
    elif isinstance(now_override, str) and now_override:
        try:
            now_utc = datetime.fromisoformat(now_override)
        except ValueError:
            now_utc = datetime.now(UTC)
    else:
        now_utc = datetime.now(UTC)

    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=UTC)

    closed = auto_close_expired(base_dir, max_age_days=float(max_age), now_utc=now_utc)
    return {
        "fired": True,
        "reason": "ok",
        "max_age_days": float(max_age),
        "closed_count": len(closed),
        "closed_ids": [c.insight_id for c in closed],
    }
