"""Claude onboarding-side helpers — issue #406.

Phase E of #397 closed the provider Protocol; #406 follows up by
moving the last Claude-specific helpers out of
:mod:`pollypm.onboarding` into the provider package so a third-party
provider can ship login support without patching onboarding.

Two public helpers live here:

* :func:`detected_claude_version` — read ``claude --version`` and
  return a stable semver string (with a hard-coded fallback used when
  the binary is unavailable, so onboarding can still write a valid
  ``.claude.json`` during dry-runs).
* :func:`prime_claude_home` — populate the managed
  ``CLAUDE_CONFIG_DIR`` profile so the first launch of ``claude`` does
  not stop at the welcome wizard. Idempotent.

The legacy ``pollypm.onboarding._detected_claude_version`` and
``pollypm.onboarding._prime_claude_home`` symbols now delegate here
and remain importable for back-compat with tests / plugins.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path


_CLAUDE_VERSION_FALLBACK = "2.1.92"


def detected_claude_version() -> str:
    """Return the installed Claude CLI version, or a stable fallback.

    Why the fallback: onboarding writes ``lastOnboardingVersion`` into
    the seeded ``.claude.json`` so the wizard does not re-fire on the
    first launch. When the binary is missing (CI, dry-run installs) we
    still need a value that *looks like* a real version so the Claude
    CLI accepts it. The fallback is chosen to be the lowest release
    that supports the keys we write.
    """
    try:
        result = subprocess.run(
            ["claude", "--version"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return _CLAUDE_VERSION_FALLBACK
    match = re.search(r"(\d+\.\d+\.\d+)", result.stdout)
    if match:
        return match.group(1)
    return _CLAUDE_VERSION_FALLBACK


def prime_claude_home(home: Path) -> None:
    """Seed the managed Claude profile so launches run unattended.

    Claude Code reads ``.claude.json`` from inside ``CLAUDE_CONFIG_DIR``
    (``home/.claude/``) — onboarding pre-populates it with the keys the
    welcome wizard would otherwise prompt for. Also writes
    ``settings.json`` with the flags that suppress the
    ``--dangerously-skip-permissions`` / workspace-trust prompts so
    PollyPM-launched panes don't block on confirmation dialogs.

    Idempotent: existing values are preserved; missing values are
    filled in. Safe to call repeatedly during onboarding, control-home
    sync, and supervisor bootstrap.
    """
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)

    # Claude Code reads .claude.json from INSIDE CLAUDE_CONFIG_DIR (home/.claude/)
    state_path = claude_dir / ".claude.json"
    data: dict[str, object] = {}
    if state_path.exists():
        try:
            data = json.loads(state_path.read_text())
        except json.JSONDecodeError:
            data = {}

    if "firstStartTime" not in data:
        data["firstStartTime"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    if not isinstance(data.get("numStartups"), int):
        data["numStartups"] = 0
    data["hasCompletedOnboarding"] = True
    data["lastOnboardingVersion"] = str(data.get("lastOnboardingVersion") or detected_claude_version())

    state_path.write_text(json.dumps(data, indent=2) + "\n")

    # Ensure settings.json has the flags needed for unattended operation:
    # - skipDangerousModePermissionPrompt: skip the "are you sure?" dialog
    # - bypassWorkspaceTrust: skip the "is this a project you trust?" dialog
    # - permissions.dangerouslySkipPermissions: match the --dangerously-skip-permissions flag
    settings_path = claude_dir / "settings.json"
    settings: dict[str, object] = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
        except json.JSONDecodeError:
            settings = {}
    settings["skipDangerousModePermissionPrompt"] = True
    settings["bypassWorkspaceTrust"] = True
    if not isinstance(settings.get("permissions"), dict):
        settings["permissions"] = {}
    settings["permissions"]["dangerouslySkipPermissions"] = True  # type: ignore[index]
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")


__all__ = ["detected_claude_version", "prime_claude_home"]
