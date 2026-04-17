"""``[downtime]`` config reader.

The downtime plugin reads its own settings section directly from the
user's ``pollypm.toml`` — we intentionally avoid pushing plugin-specific
knobs into the core ``PollyPMConfig`` dataclass. See spec §9.

Keys:

* ``enabled`` (bool, default ``True``) — master kill switch.
* ``threshold_pct`` (int, default ``50``) — capacity % at or above which
  the downtime tick skips. ``used_pct >= threshold_pct`` → skip.
* ``cadence`` (str, default ``"@every 12h"``) — roster schedule override.
* ``disabled_categories`` (list[str], default ``[]``) — kinds to exclude
  from selection. Allowed values mirror the five exploration handler
  kinds: ``spec_feature``, ``build_speculative``, ``audit_docs``,
  ``security_scan``, ``try_alt_approach``.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_THRESHOLD_PCT = 50
DEFAULT_CADENCE = "@every 12h"

KNOWN_CATEGORIES: frozenset[str] = frozenset(
    {
        "spec_feature",
        "build_speculative",
        "audit_docs",
        "security_scan",
        "try_alt_approach",
    }
)


@dataclass(slots=True, frozen=True)
class DowntimeSettings:
    """Parsed ``[downtime]`` config block.

    All fields have sensible defaults — a missing section behaves
    identically to ``[downtime]`` with no keys set. The dataclass is
    frozen so it's cheap to pass around the tick handler / candidate
    selector without fear of mutation.
    """

    enabled: bool = True
    threshold_pct: int = DEFAULT_THRESHOLD_PCT
    cadence: str = DEFAULT_CADENCE
    disabled_categories: tuple[str, ...] = ()


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


def _coerce_categories(raw: object) -> tuple[str, ...]:
    """Return a filtered tuple of known category names. Unknown entries are dropped."""
    if not isinstance(raw, (list, tuple)):
        return ()
    out: list[str] = []
    for entry in raw:
        if isinstance(entry, str) and entry in KNOWN_CATEGORIES and entry not in out:
            out.append(entry)
    return tuple(out)


def parse_downtime_settings(raw: object) -> DowntimeSettings:
    """Parse a raw ``[downtime]`` TOML table (dict) into settings."""
    if not isinstance(raw, dict):
        return DowntimeSettings()
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        enabled = True
    threshold_pct = _coerce_int(
        raw.get("threshold_pct", DEFAULT_THRESHOLD_PCT),
        DEFAULT_THRESHOLD_PCT,
        min_value=0,
        max_value=100,
    )
    cadence_raw = raw.get("cadence", DEFAULT_CADENCE)
    cadence = cadence_raw.strip() if isinstance(cadence_raw, str) and cadence_raw.strip() else DEFAULT_CADENCE
    disabled_categories = _coerce_categories(raw.get("disabled_categories", ()))
    return DowntimeSettings(
        enabled=enabled,
        threshold_pct=threshold_pct,
        cadence=cadence,
        disabled_categories=disabled_categories,
    )


def load_downtime_settings(config_path: Path) -> DowntimeSettings:
    """Load the ``[downtime]`` section from a ``pollypm.toml`` file.

    Missing file / parse error / missing section → defaults. The downtime
    plugin must never crash the rail over a malformed config.
    """
    try:
        raw = tomllib.loads(Path(config_path).read_text())
    except (OSError, tomllib.TOMLDecodeError, ValueError):
        return DowntimeSettings()
    section = raw.get("downtime") if isinstance(raw, dict) else None
    return parse_downtime_settings(section)
