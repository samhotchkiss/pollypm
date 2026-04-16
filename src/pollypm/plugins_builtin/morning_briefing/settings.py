"""``[briefing]`` config reader.

The morning-briefing plugin reads its own settings section directly from
the user's ``pollypm.toml`` — we don't want to push plugin-specific
knobs into the core ``PollyPMConfig`` dataclass. See spec §7.

Keys:

* ``enabled`` (bool, default ``True``) — master kill switch.
* ``hour`` (int, default ``6``) — 24-hour local hour when the briefing
  fires. Values outside 0–23 fall back to the default.
* ``timezone`` (str, default ``""``) — IANA timezone override. Falls
  back to ``config.pollypm.timezone`` then system detection.
* ``priorities_count`` (int, default ``5``) — top-N tasks to surface.
* ``quiet_mode_after_days`` (int, default ``7``) — days of silence before
  the briefing downshifts to weekly cadence (mb04).
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


DEFAULT_HOUR = 6
DEFAULT_PRIORITIES_COUNT = 5
DEFAULT_QUIET_AFTER_DAYS = 7


@dataclass(slots=True, frozen=True)
class BriefingSettings:
    enabled: bool = True
    hour: int = DEFAULT_HOUR
    timezone: str = ""
    priorities_count: int = DEFAULT_PRIORITIES_COUNT
    quiet_mode_after_days: int = DEFAULT_QUIET_AFTER_DAYS


def _coerce_int(value: object, default: int, *, min_value: int | None = None, max_value: int | None = None) -> int:
    try:
        out = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if min_value is not None and out < min_value:
        return default
    if max_value is not None and out > max_value:
        return default
    return out


def parse_briefing_settings(raw: object) -> BriefingSettings:
    """Parse a raw ``[briefing]`` TOML table (dict) into settings."""
    if not isinstance(raw, dict):
        return BriefingSettings()
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        enabled = True
    hour = _coerce_int(raw.get("hour", DEFAULT_HOUR), DEFAULT_HOUR, min_value=0, max_value=23)
    tz_raw = raw.get("timezone", "")
    timezone = tz_raw.strip() if isinstance(tz_raw, str) else ""
    priorities_count = _coerce_int(
        raw.get("priorities_count", DEFAULT_PRIORITIES_COUNT),
        DEFAULT_PRIORITIES_COUNT,
        min_value=1,
        max_value=50,
    )
    quiet_mode_after_days = _coerce_int(
        raw.get("quiet_mode_after_days", DEFAULT_QUIET_AFTER_DAYS),
        DEFAULT_QUIET_AFTER_DAYS,
        min_value=1,
        max_value=365,
    )
    return BriefingSettings(
        enabled=enabled,
        hour=hour,
        timezone=timezone,
        priorities_count=priorities_count,
        quiet_mode_after_days=quiet_mode_after_days,
    )


def load_briefing_settings(config_path: Path) -> BriefingSettings:
    """Load the ``[briefing]`` section from a ``pollypm.toml`` file.

    Missing file / parse error / missing section → defaults. The briefing
    plugin must never crash the rail over a malformed config.
    """
    try:
        raw = tomllib.loads(Path(config_path).read_text())
    except (OSError, tomllib.TOMLDecodeError, ValueError):
        return BriefingSettings()
    section = raw.get("briefing") if isinstance(raw, dict) else None
    return parse_briefing_settings(section)
