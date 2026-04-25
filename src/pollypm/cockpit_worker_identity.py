"""Worker identity helpers for cockpit worker/session surfaces.

Contract:
- Inputs: a session name plus optional ``[worker_colors]`` overrides
  loaded from ``pollypm.toml``.
- Outputs: stable ``WorkerIdentity`` records containing avatar and
  colour values for rendering.
- Side effects: optional best-effort config-file read for overrides.
- Invariants: same session name yields the same identity across renders;
  invalid override values are ignored.
"""

from __future__ import annotations

import colorsys
import hashlib
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_TRAILING_DIGITS_RE = re.compile(r"(\d+)$")


@dataclass(slots=True, frozen=True)
class WorkerIdentity:
    session_name: str
    avatar: str
    color: str


def load_worker_color_overrides(config_path: Path) -> dict[str, str]:
    """Return valid ``[worker_colors]`` overrides from ``config_path``."""
    try:
        raw = tomllib.loads(config_path.read_text())
    except Exception:  # noqa: BLE001
        return {}
    section = raw.get("worker_colors", {})
    if not isinstance(section, dict):
        return {}
    overrides: dict[str, str] = {}
    for key, value in section.items():
        normalized = _normalize_color(value)
        if normalized is None:
            continue
        overrides[str(key).strip().lower()] = normalized
    return overrides


def worker_identity(
    session_name: str,
    *,
    color_overrides: Mapping[str, str] | None = None,
) -> WorkerIdentity:
    """Return a stable avatar + colour for ``session_name``."""
    lowered = session_name.strip().lower()
    avatar = _avatar_for_session(session_name, lowered)
    color = _override_color(session_name, lowered, color_overrides) or _hashed_color(lowered)
    return WorkerIdentity(session_name=session_name, avatar=avatar, color=color)


def _avatar_for_session(session_name: str, lowered: str) -> str:
    if lowered in {"operator", "polly"} or lowered.startswith("polly"):
        return "P"
    if lowered in {"reviewer", "russell"} or lowered.startswith("russell"):
        return "R"
    # Architect sessions get their own glyph so the worker roster
    # doesn't render "W architect" — the W avatar reads as a worker
    # role, which an architect isn't.
    if lowered == "architect" or lowered.startswith("architect"):
        return "A"
    match = _TRAILING_DIGITS_RE.search(session_name)
    if match:
        return f"W{match.group(1)}"
    return "W"


def _override_color(
    session_name: str,
    lowered: str,
    color_overrides: Mapping[str, str] | None,
) -> str | None:
    if not color_overrides:
        return None
    keys = [lowered]
    if lowered in {"operator", "polly"} or lowered.startswith("polly"):
        keys.append("polly")
    elif lowered in {"reviewer", "russell"} or lowered.startswith("russell"):
        keys.append("russell")
    else:
        keys.append("worker")
    for key in keys:
        color = color_overrides.get(key)
        if color:
            return color
    return None


def _normalize_color(value: object) -> str | None:
    text = str(value).strip()
    if not _HEX_COLOR_RE.fullmatch(text):
        return None
    return text.upper()


def _hashed_color(seed: str) -> str:
    digest = hashlib.sha1(seed.encode("utf-8")).digest()
    hue = int.from_bytes(digest[:2], "big") % 360
    sat = 0.60 + (digest[2] / 255.0) * 0.15
    light = 0.50 + (digest[3] / 255.0) * 0.12
    red, green, blue = colorsys.hls_to_rgb(hue / 360.0, light, sat)
    return "#{:02X}{:02X}{:02X}".format(
        int(red * 255),
        int(green * 255),
        int(blue * 255),
    )
