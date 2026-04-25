"""Timezone utilities for PollyPM.

Detects the system timezone, allows user override via config,
and provides formatting helpers for consistent local time display.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime
from functools import lru_cache
from zoneinfo import ZoneInfo


@lru_cache(maxsize=1)
def _detect_system_tz() -> str:
    """Detect the system's IANA timezone name."""
    # macOS: /etc/localtime is a symlink into zoneinfo
    try:
        link = os.readlink("/etc/localtime")
        if "zoneinfo/" in link:
            return link.split("zoneinfo/")[-1]
    except (OSError, ValueError):
        pass
    # TZ environment variable
    tz_env = os.environ.get("TZ", "")
    if tz_env and "/" in tz_env:
        return tz_env
    # Fallback
    return "UTC"


def get_timezone(config_tz: str = "") -> ZoneInfo:
    """Get the timezone to use. Config override > system detection > UTC."""
    name = config_tz.strip() if config_tz else _detect_system_tz()
    try:
        return ZoneInfo(name)
    except (KeyError, Exception):
        return ZoneInfo("UTC")


def format_time(iso_str: str, *, tz: ZoneInfo | None = None, fmt: str = "%b %d %I:%M %p") -> str:
    """Format an ISO timestamp string in the user's local timezone."""
    if not iso_str:
        return ""
    tz = tz or get_timezone()
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(tz).strftime(fmt)
    except (ValueError, TypeError):
        return iso_str[:16]


def format_relative(iso_str: str) -> str:
    """Format as relative time: 'just now', '5m ago', '2h ago', etc."""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        age = (datetime.now(UTC) - dt).total_seconds()
        if age < 60:
            return "just now"
        if age < 3600:
            return f"{int(age // 60)}m ago"
        if age < 86400:
            return f"{int(age // 3600)}h ago"
        return f"{int(age // 86400)}d ago"
    except (ValueError, TypeError):
        return iso_str[:16]


def now_local(tz: ZoneInfo | None = None) -> str:
    """Current time formatted in user's timezone."""
    tz = tz or get_timezone()
    return datetime.now(tz).strftime("%b %d %I:%M %p %Z")
