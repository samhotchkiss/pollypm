"""Check for new PollyPM versions and notify the user via inbox."""
from __future__ import annotations

import importlib.metadata
import json
import logging
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from pollypm.messaging import create_message, list_open_messages

logger = logging.getLogger(__name__)

# Marker file to avoid spamming the inbox on every heartbeat
_LAST_CHECK_FILENAME = "version_check.json"


def _current_version() -> str:
    try:
        return importlib.metadata.version("pollypm")
    except importlib.metadata.PackageNotFoundError:
        return "dev"


def _fetch_latest_version() -> str | None:
    """Query GitHub for the latest release tag. Returns version string or None."""
    # Try gh CLI first (fast, authenticated)
    try:
        result = subprocess.run(
            ["gh", "api", "repos/samhotchkiss/pollypm/releases/latest", "-q", ".tag_name"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().lstrip("v")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Fallback: git ls-remote
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--tags", "https://github.com/samhotchkiss/pollypm.git"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            tags = [
                line.split("refs/tags/")[-1].lstrip("v")
                for line in result.stdout.strip().splitlines()
                if "refs/tags/" in line and not line.endswith("^{}")
            ]
            if tags:
                return sorted(tags)[-1]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    return None


_CHECK_COOLDOWN_SECONDS = 6 * 3600  # only hit GitHub every 6 hours


def _too_soon_to_check(state_dir: Path) -> bool:
    """Return True if we checked recently enough to skip this cycle."""
    marker = state_dir / _LAST_CHECK_FILENAME
    if not marker.exists():
        return False
    try:
        data = json.loads(marker.read_text())
        checked_at = data.get("checked_at", "")
        if checked_at:
            last = datetime.fromisoformat(checked_at)
            elapsed = (datetime.now(UTC) - last).total_seconds()
            return elapsed < _CHECK_COOLDOWN_SECONDS
    except (json.JSONDecodeError, OSError, ValueError):
        pass
    return False


def _already_notified(state_dir: Path, latest: str) -> bool:
    """Check if we already sent an inbox notification for this version."""
    marker = state_dir / _LAST_CHECK_FILENAME
    if not marker.exists():
        return False
    try:
        data = json.loads(marker.read_text())
        return data.get("notified_version") == latest
    except (json.JSONDecodeError, OSError):
        return False


def _record_check(state_dir: Path) -> None:
    """Update the checked_at timestamp without changing notified_version."""
    marker = state_dir / _LAST_CHECK_FILENAME
    marker.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, str] = {}
    if marker.exists():
        try:
            existing = json.loads(marker.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    existing["checked_at"] = datetime.now(UTC).isoformat()
    marker.write_text(json.dumps(existing))


def _record_notification(state_dir: Path, latest: str) -> None:
    marker = state_dir / _LAST_CHECK_FILENAME
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({
        "notified_version": latest,
        "checked_at": datetime.now(UTC).isoformat(),
    }))


def _inbox_already_has_upgrade_message(project_root: Path, latest: str) -> bool:
    """Check if there's already an open inbox message about this version."""
    try:
        messages = list_open_messages(project_root)
        needle = f"PollyPM {latest}"
        return any(needle in msg.subject for msg in messages)
    except Exception:  # noqa: BLE001
        return False


def check_and_notify(project_root: Path, state_dir: Path) -> str | None:
    """Check for a new version and send an inbox message if one is available.

    Returns the new version string if a notification was sent, None otherwise.
    Safe to call frequently — it deduplicates via a marker file and checks
    the inbox for existing messages.
    """
    current = _current_version()
    if current == "dev":
        # Running from source — skip version checks
        return None

    if _too_soon_to_check(state_dir):
        return None

    latest = _fetch_latest_version()
    if latest is None:
        logger.debug("Could not fetch latest version from GitHub")
        return None

    if latest == current:
        _record_check(state_dir)
        return None

    # Already notified for this version?
    if _already_notified(state_dir, latest):
        return None

    # Double-check: is there already an open inbox message?
    if _inbox_already_has_upgrade_message(project_root, latest):
        _record_notification(state_dir, latest)
        return None

    # Send the notification
    create_message(
        project_root,
        sender="system",
        subject=f"PollyPM {latest} available (current: {current})",
        body=(
            f"A new version of PollyPM is available.\n"
            f"\n"
            f"  Current: {current}\n"
            f"  Latest:  {latest}\n"
            f"\n"
            f"To upgrade:\n"
            f"  pm upgrade\n"
            f"\n"
            f"To see what changed:\n"
            f"  pm upgrade --check\n"
            f"\n"
            f"Running sessions are not affected — restart with "
            f"`pm reset && pm up` after upgrading."
        ),
    )
    _record_notification(state_dir, latest)
    logger.info("Sent inbox notification for PollyPM %s (current: %s)", latest, current)
    return latest
