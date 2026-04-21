"""Plugin-health checks extracted from :mod:`pollypm.doctor`."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pollypm.doctor as doctor


_CRITICAL_PLUGINS_FOR_BOOT = ("tmux_session_service",)


def check_builtin_plugin_manifests() -> doctor.CheckResult:
    """Every builtin plugin's manifest must parse as TOML."""
    here = Path(__file__).resolve().parent.parent
    builtin_root = here / "plugins_builtin"
    if not builtin_root.is_dir():
        return doctor._fail(
            f"plugins_builtin dir missing at {builtin_root}",
            why=(
                "The builtin plugin tree ships inside the package. Its "
                "absence indicates a broken install."
            ),
            fix=(
                "Reinstall PollyPM —\n"
                "  uv tool install --editable --reinstall .\n"
                "Recheck: pm doctor"
            ),
        )
    bad: list[tuple[str, str]] = []
    seen = 0
    for manifest in builtin_root.glob("*/pollypm-plugin.toml"):
        seen += 1
        try:
            tomllib.loads(manifest.read_text())
        except Exception as exc:  # noqa: BLE001
            bad.append((manifest.parent.name, str(exc)))
    if bad:
        summary = ", ".join(f"{n} ({e[:40]})" for n, e in bad)
        return doctor._fail(
            f"{len(bad)} plugin manifest(s) failed to parse: {summary}",
            why=(
                "Plugin discovery halts on malformed manifests; affected "
                "capabilities silently vanish from the host."
            ),
            fix=(
                "Re-pull source and reinstall —\n"
                "  git pull\n"
                "  uv tool install --editable --reinstall .\n"
                "Recheck: pm doctor"
            ),
            data={"bad": [n for n, _ in bad]},
        )
    return doctor._ok(f"{seen} builtin plugin manifest(s) parse", data={"count": seen})


def check_no_critical_plugin_disabled() -> doctor.CheckResult:
    """Configured ``[plugins].disabled`` must not include boot-critical plugins."""
    from pollypm.config import DEFAULT_CONFIG_PATH, load_config

    if not DEFAULT_CONFIG_PATH.exists():
        return doctor._skip("critical-plugin check skipped (no config)")
    try:
        config = load_config(DEFAULT_CONFIG_PATH)
    except Exception:  # noqa: BLE001
        return doctor._skip("critical-plugin check skipped (config parse error)")
    disabled = set(getattr(getattr(config, "plugins", None), "disabled", ()) or ())
    conflicts = sorted(disabled & set(_CRITICAL_PLUGINS_FOR_BOOT))
    if conflicts:
        return doctor._fail(
            f"critical plugin(s) disabled: {', '.join(conflicts)}",
            why=(
                "PollyPM's session lifecycle is driven by the tmux_session_service "
                "plugin. Disabling it leaves every `pm up`, `pm attach`, and "
                "worker-start with no session backend."
            ),
            fix=(
                "Remove these names from [plugins].disabled in ~/.pollypm/pollypm.toml —\n"
                f"  {', '.join(conflicts)}\n"
                "Then: pm doctor"
            ),
            data={"conflicts": conflicts},
        )
    return doctor._ok("no critical plugin disabled")


def check_plugin_capabilities_no_deprecations() -> doctor.CheckResult:
    """Builtin plugin manifests must not use deprecated capability-shape forms."""
    here = Path(__file__).resolve().parent.parent
    builtin_root = here / "plugins_builtin"
    offenders: list[str] = []
    for manifest in builtin_root.glob("*/pollypm-plugin.toml"):
        try:
            data = tomllib.loads(manifest.read_text())
        except Exception:  # noqa: BLE001
            continue
        caps = data.get("capabilities") if isinstance(data, dict) else None
        if isinstance(caps, list):
            for entry in caps:
                if not isinstance(entry, dict):
                    offenders.append(manifest.parent.name)
                    break
    if offenders:
        return doctor._fail(
            f"deprecated capability shape in: {', '.join(sorted(set(offenders)))}",
            why=(
                "Plugin API v1 requires [[capabilities]] tables; string-list "
                "shorthand is a migration artefact that emits a warning at "
                "plugin-load time."
            ),
            fix=(
                "Update each flagged manifest to use [[capabilities]] blocks —\n"
                "  see docs/plugin-discovery-spec.md §4\n"
                "Recheck: pm doctor"
            ),
            data={"plugins": sorted(set(offenders))},
            severity="warning",
        )
    return doctor._ok("no deprecated capability shapes in builtin plugins")
