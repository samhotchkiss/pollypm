"""Dev-only network failure simulation controls.

Contract:
- Inputs: a PollyPM config path or base directory.
- Outputs: a small marker file that long-lived PollyPM processes can
  consume to simulate one connection-refused network failure.
- Side effects: writes/removes ``dev_network_simulation.json`` under
  the PollyPM base directory.
- Invariants: file-backed so already-running cockpit/rail processes can
  observe the flag without restart; one-shot unless explicitly re-armed.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path


_MARKER_FILENAME = "dev_network_simulation.json"


class SimulatedNetworkDead(ConnectionRefusedError):
    """Raised when the dev network-dead switch consumes a failure."""


def marker_path_for_base_dir(base_dir: Path) -> Path:
    """Return the marker path for a PollyPM base directory."""
    return Path(base_dir) / _MARKER_FILENAME


def marker_path_for_config(config_path: Path) -> Path:
    """Return the marker path associated with ``config_path``.

    Prefer the parsed ``project.base_dir`` so custom configs share the
    same control location as the running cockpit. If the config is
    missing or broken, fall back to the config's parent directory so the
    dev CLI remains usable while diagnosing boot failures.
    """
    try:
        from pollypm.config import load_config

        config = load_config(config_path)
        base_dir = getattr(getattr(config, "project", None), "base_dir", None)
        if isinstance(base_dir, Path):
            return marker_path_for_base_dir(base_dir)
    except Exception:  # noqa: BLE001
        pass
    return marker_path_for_base_dir(Path(config_path).parent)


def arm_network_dead(config_path: Path) -> Path:
    """Arm one simulated network-dead failure for this PollyPM install."""
    marker = marker_path_for_config(config_path)
    payload = {
        "network_dead_once": True,
        "armed_at": datetime.now(UTC).isoformat(),
        "reason": "pm dev simulate-network-dead",
    }
    _write_marker(marker, payload)
    return marker


def clear_network_dead(config_path: Path) -> Path:
    """Clear any pending simulated network-dead failure."""
    marker = marker_path_for_config(config_path)
    marker.unlink(missing_ok=True)
    return marker


def network_dead_armed(config_path: Path) -> bool:
    """Return True if a simulated network-dead failure is pending."""
    return _marker_is_armed(marker_path_for_config(config_path))


def network_dead_armed_for_base_dir(base_dir: Path) -> bool:
    """Return True if a simulated network-dead failure is pending."""
    return _marker_is_armed(marker_path_for_base_dir(base_dir))


def raise_if_network_dead(config_path: Path, *, surface: str) -> None:
    """Consume and raise when a config-scoped network simulation is armed."""
    _raise_if_marker_armed(marker_path_for_config(config_path), surface=surface)


def raise_if_network_dead_for_base_dir(base_dir: Path, *, surface: str) -> None:
    """Consume and raise when a base-dir-scoped simulation is armed."""
    _raise_if_marker_armed(marker_path_for_base_dir(base_dir), surface=surface)


def _raise_if_marker_armed(marker: Path, *, surface: str) -> None:
    if not _consume_marker(marker):
        return
    raise SimulatedNetworkDead(
        f"network unreachable (simulated): connection refused for {surface}"
    )


def _marker_is_armed(marker: Path) -> bool:
    payload = _read_marker(marker)
    return bool(payload.get("network_dead_once"))


def _consume_marker(marker: Path) -> bool:
    payload = _read_marker(marker)
    if not payload.get("network_dead_once"):
        return False
    marker.unlink(missing_ok=True)
    return True


def _read_marker(marker: Path) -> dict[str, object]:
    try:
        raw = marker.read_text(encoding="utf-8")
        parsed = json.loads(raw)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed


def _write_marker(marker: Path, payload: dict[str, object]) -> None:
    marker.parent.mkdir(parents=True, exist_ok=True)
    tmp = marker.with_name(f".{marker.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(marker)

