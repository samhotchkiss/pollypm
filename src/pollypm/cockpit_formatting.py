from __future__ import annotations

from datetime import datetime as _dt

from collections.abc import Callable

from pollypm.tz import format_time as _default_fmt_time


def format_relative_age(value) -> str:
    """Relative-age formatting tolerant of missing or malformed inputs."""
    if not value:
        return ""
    iso_str = value.isoformat() if isinstance(value, _dt) else str(value)
    try:
        from pollypm.tz import format_relative

        return format_relative(iso_str)
    except Exception:  # noqa: BLE001
        return iso_str[:16]


def format_event_time(value, *, formatter: Callable[[str], str] = _default_fmt_time) -> str:
    """Stable local timestamp formatting for cockpit detail rows."""
    if not value:
        return ""
    iso = value.isoformat() if hasattr(value, "isoformat") else str(value)
    return formatter(iso)
