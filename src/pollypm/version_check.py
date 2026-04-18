"""Check for new PollyPM versions and notify the user via a durable alert."""
from __future__ import annotations

import importlib.metadata
import json
import logging
import subprocess
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Marker file to avoid spamming the alert surface on every heartbeat
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
    """Check if we already sent a notification for this version."""
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


def _raise_upgrade_alert(project_root: Path, current: str, latest: str) -> None:
    """Raise a durable alert advertising the new release."""
    try:
        from pollypm.config import DEFAULT_CONFIG_PATH, load_config
        from pollypm.store.registry import get_store

        config_path = project_root / "pollypm.toml"
        if not config_path.exists():
            config_path = DEFAULT_CONFIG_PATH
        config = load_config(config_path)
        # #349: writers land on the unified ``messages`` table via Store.
        store = get_store(config)
        try:
            store.upsert_alert(
                "pollypm",
                "upgrade_available",
                "info",
                f"PollyPM {latest} is available (current: {current}). Run `pm upgrade`.",
            )
            from pollypm.plugins_builtin.activity_feed.summaries import (
                activity_summary,
            )

            store.append_event(
                scope="pollypm",
                sender="pollypm",
                subject="upgrade_available",
                payload={
                    "message": activity_summary(
                        summary=(
                            f"New version detected: {latest} "
                            f"(current {current})"
                        ),
                        severity="recommendation",
                        verb="upgrade_available",
                        subject="pollypm",
                        latest=latest,
                        current=current,
                    ),
                    "latest": latest,
                    "current": current,
                },
            )
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()
    except Exception:  # noqa: BLE001 - version check must not fail the caller
        logger.debug("Could not persist upgrade alert for %s", latest)


def check_and_notify(project_root: Path, state_dir: Path) -> str | None:
    """Check for a new version and raise an alert if one is available.

    Returns the new version string if a notification was sent, None otherwise.
    Safe to call frequently — it deduplicates via a marker file.
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

    _raise_upgrade_alert(project_root, current, latest)
    _record_notification(state_dir, latest)
    logger.info("Raised upgrade alert for PollyPM %s (current: %s)", latest, current)
    return latest
