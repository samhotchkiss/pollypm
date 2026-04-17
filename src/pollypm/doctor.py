"""``pm doctor`` — environment validator for brand-new PollyPM users.

Every check answers three questions when it fails:

1. What is wrong (check name + short status line).
2. Why PollyPM needs it (the ``why`` field).
3. The exact fix command (the ``fix`` field, multi-line OK).

Checks are **fast** (total runtime < 5s on a healthy system) and **safe
before any PollyPM state exists** — the first-run user gets actionable
output even if ``~/.pollypm/`` has never been touched. Anything that
requires a loaded Supervisor, the work service, or plugins being
initialised must be resilient to all of those being absent.

Design notes
------------

- A single :class:`Check` dataclass describes each probe. The ``run``
  callable returns a :class:`CheckResult` — no exceptions escape into
  the runner, so a crashing check cannot poison the rest of the run.
- Registered checks live in :func:`_registered_checks`, ordered
  top-down per the public doctor spec (issue #253).
- Every check is independently unit-testable. The runner is a plain
  loop so tests can call it with a custom check list.
- No imports of ``supervisor.py``, ``work/session_manager.py``,
  ``work/sqlite_service.py``, ``plugin_api/v1.py``, or
  ``memory_backends/*`` — per the spec's hard constraints.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable


# --------------------------------------------------------------------- #
# Result model
# --------------------------------------------------------------------- #


Severity = str  # "error" | "warning" | "info"


@dataclass(slots=True)
class CheckResult:
    """Outcome of a single check.

    ``passed`` — True when the check passes or is skipped benignly.
    ``status`` — one-line human summary for the checklist row.
    ``severity`` — only meaningful when ``passed=False``. "warning"
        results do not fail the overall run; "error" results do.
    ``why`` — the "why" sentence in the three-question error message.
    ``fix`` — the "fix" block; may span multiple lines.
    ``data`` — machine-readable payload merged into the ``--json`` output.
    ``skipped`` — true when the check cannot meaningfully run in the
        current environment (e.g. a provider-specific reachability check
        when that provider is not configured). Skipped checks never fail.
    ``fixable`` — true when ``--fix`` can safely auto-resolve this.
    ``fix_fn`` — optional zero-arg callable invoked by ``--fix``. Must
        return a tuple of ``(bool, str)`` — ``(success, message)``.
    """

    passed: bool
    status: str = ""
    severity: Severity = "error"
    why: str = ""
    fix: str = ""
    data: dict[str, object] = field(default_factory=dict)
    skipped: bool = False
    fixable: bool = False
    fix_fn: Callable[[], tuple[bool, str]] | None = None


@dataclass(slots=True)
class Check:
    """A single doctor probe."""

    name: str
    run: Callable[[], CheckResult]
    category: str = "general"
    severity: Severity = "error"


@dataclass(slots=True)
class DoctorReport:
    """Aggregate output of a full doctor run."""

    results: list[tuple[Check, CheckResult]] = field(default_factory=list)
    duration_seconds: float = 0.0

    @property
    def errors(self) -> list[tuple[Check, CheckResult]]:
        return [
            (c, r) for c, r in self.results
            if not r.passed and not r.skipped and r.severity == "error"
        ]

    @property
    def warnings(self) -> list[tuple[Check, CheckResult]]:
        return [
            (c, r) for c, r in self.results
            if not r.passed and not r.skipped and r.severity == "warning"
        ]

    @property
    def passed_count(self) -> int:
        return sum(1 for _, r in self.results if r.passed)

    @property
    def skipped_count(self) -> int:
        return sum(1 for _, r in self.results if r.skipped)

    @property
    def ok(self) -> bool:
        return not self.errors


# --------------------------------------------------------------------- #
# Helpers: version parsing, command probing
# --------------------------------------------------------------------- #


_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


def _parse_version(text: str) -> tuple[int, int, int] | None:
    """Extract the first ``N.N[.N]`` version from ``text``.

    Tolerant of prefixes (``"tmux 3.5a"``, ``"git version 2.43.0"``,
    ``"Python 3.13.1"``). Missing patch level defaults to 0. Returns
    ``None`` if no numeric version is detectable.
    """
    match = _VERSION_RE.search(text or "")
    if not match:
        return None
    major = int(match.group(1))
    minor = int(match.group(2))
    patch = int(match.group(3) or "0")
    return (major, minor, patch)


def _run_cmd(cmd: list[str], *, timeout: float = 2.0) -> tuple[int, str]:
    """Run ``cmd`` and return ``(returncode, combined_output)``.

    Errors (missing binary, timeout) are caught and surfaced as a
    ``(-1, message)`` tuple — callers never have to handle exceptions.
    Keep ``timeout`` short: doctor must stay well under 5 s overall.
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return (-1, f"command not found: {cmd[0]}")
    except subprocess.TimeoutExpired:
        return (-1, f"command timed out after {timeout}s: {' '.join(cmd)}")
    except Exception as exc:  # noqa: BLE001
        return (-1, f"command errored: {exc}")
    combined = (result.stdout or "") + (result.stderr or "")
    return (result.returncode, combined.strip())


def _ok(status: str, **kwargs) -> CheckResult:
    return CheckResult(passed=True, status=status, **kwargs)


def _fail(status: str, *, why: str, fix: str, severity: str = "error", **kwargs) -> CheckResult:
    return CheckResult(
        passed=False,
        status=status,
        severity=severity,
        why=why,
        fix=fix,
        **kwargs,
    )


def _skip(status: str) -> CheckResult:
    return CheckResult(passed=True, status=status, skipped=True)


# --------------------------------------------------------------------- #
# System prerequisite checks
# --------------------------------------------------------------------- #


def _pyproject_path() -> Path:
    """Locate the repo's ``pyproject.toml``.

    Works both when PollyPM is installed editable (walk up from this
    file to find the project root) and when called from within a test
    fixture.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "pyproject.toml"
        if candidate.is_file():
            return candidate
    # Fallback: current working directory.
    return Path.cwd() / "pyproject.toml"


def _read_pyproject_required_python() -> tuple[int, int, int]:
    """Return the minimum Python version declared in ``pyproject.toml``.

    Defaults to ``(3, 13, 0)`` if the file cannot be read — matches the
    current PollyPM floor and keeps the check fail-safe on oddball
    installs.
    """
    try:
        text = _pyproject_path().read_text()
        data = tomllib.loads(text)
    except (OSError, ValueError):
        return (3, 13, 0)
    requires = data.get("project", {}).get("requires-python", "") if isinstance(data, dict) else ""
    if not isinstance(requires, str):
        return (3, 13, 0)
    parsed = _parse_version(requires)
    return parsed or (3, 13, 0)


def _read_pyproject_version() -> str | None:
    """Return the ``[project].version`` string from ``pyproject.toml``."""
    try:
        data = tomllib.loads(_pyproject_path().read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    project = data.get("project", {})
    if not isinstance(project, dict):
        return None
    version = project.get("version")
    return version if isinstance(version, str) else None


def check_python_version() -> CheckResult:
    want = _read_pyproject_required_python()
    have = sys.version_info[:3]
    want_str = ".".join(str(x) for x in want)
    have_str = ".".join(str(x) for x in have)
    if have >= want:
        return _ok(f"Python {have_str} (>= {want_str})", data={"version": have_str})
    return _fail(
        f"Python {have_str} is below required {want_str}",
        why=(
            f"PollyPM pins requires-python to >= {want_str} in pyproject.toml. "
            "Older interpreters lack typing features the codebase depends on "
            "(StrEnum, PEP 695 generics, tomllib)."
        ),
        fix=(
            f"Install Python >= {want_str} and re-run with that interpreter —\n"
            "  macOS:  brew install python@3.13\n"
            "  Ubuntu: sudo apt install python3.13\n"
            "  Or:     uv python install 3.13 && uv sync --reinstall\n"
            "Recheck: pm doctor"
        ),
        data={"have": have_str, "want": want_str},
    )


def _tool_path(name: str) -> str | None:
    return shutil.which(name)


def check_tmux() -> CheckResult:
    path = _tool_path("tmux")
    if path is None:
        return _fail(
            "tmux not found on PATH",
            why=(
                "PollyPM is a tmux-first orchestrator — every session, worker, "
                "and storage closet pane lives inside tmux. There is no fallback."
            ),
            fix=(
                "Install tmux —\n"
                "  macOS:  brew install tmux\n"
                "  Ubuntu: sudo apt install tmux\n"
                "  Build:  https://github.com/tmux/tmux/wiki/Installing\n"
                "Recheck: pm doctor"
            ),
        )
    rc, out = _run_cmd(["tmux", "-V"])
    version = _parse_version(out) if rc == 0 else None
    version_str = ".".join(str(x) for x in version) if version else "unknown"
    if version is None:
        return _fail(
            f"tmux version not detectable ({out[:60]!r})",
            why=(
                "PollyPM needs tmux >= 3.3 for pane-pipe-mode, kill-pane -a, "
                "and window option inheritance. We could not parse `tmux -V` output."
            ),
            fix=(
                "Reinstall tmux and re-check —\n"
                "  macOS:  brew reinstall tmux\n"
                "  Ubuntu: sudo apt reinstall tmux\n"
                "Recheck: pm doctor"
            ),
            data={"raw": out},
        )
    if version < (3, 3, 0):
        return _fail(
            f"tmux version too old ({version_str})",
            why=(
                "PollyPM uses tmux features introduced in 3.3 (pane-pipe-mode, "
                "kill-pane -a, window option inheritance). Older tmux silently "
                "loses pane output and fragments storage-closet layout."
            ),
            fix=(
                "Upgrade tmux —\n"
                "  macOS:  brew upgrade tmux\n"
                "  Ubuntu: sudo apt install --only-upgrade tmux\n"
                "  Build:  https://github.com/tmux/tmux/wiki/Installing\n"
                "Recheck: pm doctor"
            ),
            data={"version": version_str, "min": "3.3.0"},
        )
    return _ok(f"tmux {version_str} (>= 3.3)", data={"version": version_str, "path": path})


def check_git() -> CheckResult:
    path = _tool_path("git")
    if path is None:
        return _fail(
            "git not found on PATH",
            why=(
                "PollyPM tracks worktrees, creates per-task branches, and "
                "delegates issue syncing to git. Without git the worker flow "
                "cannot run."
            ),
            fix=(
                "Install git —\n"
                "  macOS:  brew install git\n"
                "  Ubuntu: sudo apt install git\n"
                "Recheck: pm doctor"
            ),
        )
    rc, out = _run_cmd(["git", "--version"])
    version = _parse_version(out) if rc == 0 else None
    if version is None:
        return _fail(
            f"git version not detectable ({out[:60]!r})",
            why="PollyPM requires git >= 2.40 for safe worktree management.",
            fix=(
                "Reinstall git —\n"
                "  macOS:  brew reinstall git\n"
                "  Ubuntu: sudo apt reinstall git\n"
                "Recheck: pm doctor"
            ),
        )
    if version < (2, 40, 0):
        version_str = ".".join(str(x) for x in version)
        return _fail(
            f"git version too old ({version_str})",
            why=(
                "PollyPM needs git >= 2.40 for `git worktree add --orphan` and "
                "the per-task worktree lifecycle introduced with the work service."
            ),
            fix=(
                "Upgrade git —\n"
                "  macOS:  brew upgrade git\n"
                "  Ubuntu: sudo apt install --only-upgrade git\n"
                "Recheck: pm doctor"
            ),
            data={"version": version_str, "min": "2.40.0"},
        )
    return _ok(
        f"git {'.'.join(str(x) for x in version)} (>= 2.40)",
        data={"version": ".".join(str(x) for x in version), "path": path},
    )


def check_gh_installed() -> CheckResult:
    path = _tool_path("gh")
    if path is None:
        return _fail(
            "gh CLI not found on PATH",
            why=(
                "PollyPM's issue backend and many worker flows shell out to `gh`. "
                "Without it, `pm issue list`, PR creation, and GitHub sync break."
            ),
            fix=(
                "Install the GitHub CLI —\n"
                "  macOS:  brew install gh\n"
                "  Ubuntu: https://github.com/cli/cli/blob/trunk/docs/install_linux.md\n"
                "Then:   gh auth login\n"
                "Recheck: pm doctor"
            ),
        )
    return _ok(f"gh CLI installed", data={"path": path})


def check_gh_authenticated() -> CheckResult:
    if _tool_path("gh") is None:
        return _skip("gh auth skipped (gh not installed)")
    rc, out = _run_cmd(["gh", "auth", "status"], timeout=3.0)
    # ``gh auth status`` returns 0 when logged in, non-zero otherwise.
    if rc == 0:
        return _ok("gh authenticated")
    return _fail(
        "gh is not authenticated",
        why=(
            "PollyPM's GitHub flows (issue sync, PR creation, review pulls) "
            "assume `gh auth status` reports a logged-in account."
        ),
        fix=(
            "Authenticate gh —\n"
            "  gh auth login\n"
            "Recheck: pm doctor"
        ),
        data={"stderr": out[:200]},
    )


def check_uv() -> CheckResult:
    path = _tool_path("uv")
    if path is None:
        return _fail(
            "uv not found on PATH",
            why=(
                "PollyPM ships as an `uv`-managed editable install; `pm` and "
                "`pollypm` entry points are provisioned via `uv tool install`."
            ),
            fix=(
                "Install uv —\n"
                "  macOS/Linux: curl -LsSf https://astral.sh/uv/install.sh | sh\n"
                "  Or: brew install uv\n"
                "Recheck: pm doctor"
            ),
        )
    return _ok("uv installed", data={"path": path})


def check_terminal_color_support() -> CheckResult:
    """Warn-only check: detect obviously plain terminals.

    On macOS and Linux, the standard set of modern terminals (Terminal.app,
    iTerm2, kitty, WezTerm, Ghostty, gnome-terminal, Konsole) advertises
    256-color / true-color via ``$TERM`` and ``$COLORTERM``. A bare
    ``TERM=dumb`` or missing ``$TERM`` means the cockpit UI will be
    unreadable — not fatal (some callers pipe output), just a warning.
    """
    term = os.environ.get("TERM", "")
    colorterm = os.environ.get("COLORTERM", "")
    if term in {"", "dumb"}:
        return _fail(
            f"terminal likely lacks color support (TERM={term!r})",
            why=(
                "PollyPM's cockpit and rail rely on 256-color / true-color ANSI "
                "escapes. A 'dumb' or missing TERM renders them as garbage."
            ),
            fix=(
                "Use a modern terminal:\n"
                "  macOS:  iTerm2 (https://iterm2.com) or Terminal.app\n"
                "  Linux:  kitty, WezTerm, Ghostty, gnome-terminal\n"
                "Or set:  export TERM=xterm-256color\n"
                "Recheck: pm doctor"
            ),
            severity="warning",
            data={"TERM": term, "COLORTERM": colorterm},
        )
    return _ok(
        f"terminal color ok (TERM={term}, COLORTERM={colorterm or 'unset'})",
        data={"TERM": term, "COLORTERM": colorterm},
    )


# --------------------------------------------------------------------- #
# PollyPM install-state checks
# --------------------------------------------------------------------- #


def check_pm_binary_resolves() -> CheckResult:
    path = _tool_path("pm") or _tool_path("pollypm")
    if path is None:
        return _fail(
            "pm / pollypm binary not on PATH",
            why=(
                "The canonical entry point is the `pm` command installed by "
                "`uv tool install --editable .`. Without it, every user-facing "
                "workflow requires `uv run pm ...`."
            ),
            fix=(
                "Install the global entry points —\n"
                "  uv tool install --editable .\n"
                "Or run via uv:  uv run pm doctor\n"
                "Recheck: pm doctor"
            ),
        )
    return _ok(f"pm binary at {path}", data={"path": path})


def check_installed_version_matches_pyproject() -> CheckResult:
    """Warn when the installed package version drifts from pyproject.toml.

    Common gotcha: the user ran ``git pull`` but did not rebuild the
    editable install, so ``pm --version`` or the running module reports
    an older version than source. We compare the packaged metadata
    against the ``[project].version`` in ``pyproject.toml``.
    """
    declared = _read_pyproject_version()
    if not declared:
        return _skip("pyproject.toml version not readable")
    try:
        from importlib.metadata import PackageNotFoundError, version as _mdver

        installed = _mdver("pollypm")
    except PackageNotFoundError:
        return _fail(
            "pollypm package metadata not found",
            why=(
                "PollyPM must be installed (editable or otherwise) for the "
                "CLI entry points to resolve. `uv run pm ...` still works, "
                "but first-class `pm` requires the install step."
            ),
            fix=(
                "Install PollyPM editable —\n"
                "  uv tool install --editable .\n"
                "Recheck: pm doctor"
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return _skip(f"package metadata unreadable ({exc})")
    if installed != declared:
        return _fail(
            f"installed pollypm={installed} drifts from source {declared}",
            why=(
                "An editable install can fall behind after `git pull` if the "
                "entry point was reinstalled from a prior revision. Running "
                "`pm ...` may execute stale code."
            ),
            fix=(
                "Reinstall the editable package —\n"
                "  uv tool install --editable --reinstall .\n"
                "Or:  uv sync --reinstall\n"
                "Recheck: pm doctor"
            ),
            severity="warning",
            data={"installed": installed, "source": declared},
        )
    return _ok(f"pollypm {installed} matches pyproject", data={"version": installed})


def check_config_file() -> CheckResult:
    """Check for a PollyPM config at the global or project-local path."""
    from pollypm.config import DEFAULT_CONFIG_PATH

    if DEFAULT_CONFIG_PATH.exists():
        return _ok(f"config present at {DEFAULT_CONFIG_PATH}", data={"path": str(DEFAULT_CONFIG_PATH)})
    return _fail(
        f"no PollyPM config at {DEFAULT_CONFIG_PATH}",
        why=(
            "PollyPM loads accounts, sessions, and project settings from "
            "~/.pollypm/pollypm.toml. Without it the CLI runs first-run "
            "onboarding every time."
        ),
        fix=(
            "Run onboarding or scaffold an example config —\n"
            "  pm onboard\n"
            "Or:  pm init\n"
            "Recheck: pm doctor"
        ),
    )


def check_provider_account_configured() -> CheckResult:
    """At least one provider account must be configured."""
    from pollypm.config import DEFAULT_CONFIG_PATH, load_config

    if not DEFAULT_CONFIG_PATH.exists():
        return _skip("account check skipped (no config)")
    try:
        config = load_config(DEFAULT_CONFIG_PATH)
    except Exception as exc:  # noqa: BLE001
        return _fail(
            f"config failed to parse ({exc})",
            why=(
                "A broken ~/.pollypm/pollypm.toml prevents the CLI from "
                "loading any accounts or sessions."
            ),
            fix=(
                "Inspect and fix the config —\n"
                "  pm example-config   # reference template\n"
                "  edit ~/.pollypm/pollypm.toml\n"
                "Recheck: pm doctor"
            ),
            data={"error": str(exc)},
        )
    accounts = getattr(config, "accounts", {}) or {}
    if not accounts:
        return _fail(
            "no provider accounts configured",
            why=(
                "PollyPM needs at least one Claude or Codex account to launch "
                "agent sessions. Heartbeat, workers, and cockpit all require "
                "a provider-bound account."
            ),
            fix=(
                "Add an account via onboarding —\n"
                "  pm onboard\n"
                "Or edit ~/.pollypm/pollypm.toml and add an [accounts.*] block\n"
                "(see `pm example-config`).\n"
                "Recheck: pm doctor"
            ),
        )
    return _ok(
        f"{len(accounts)} provider account(s) configured",
        data={"accounts": sorted(accounts.keys())},
    )


# --------------------------------------------------------------------- #
# Plugin health
# --------------------------------------------------------------------- #


_CRITICAL_PLUGINS_FOR_BOOT = ("tmux_session_service",)


def check_builtin_plugin_manifests() -> CheckResult:
    """Every builtin plugin's manifest must parse as TOML."""
    here = Path(__file__).resolve().parent
    builtin_root = here / "plugins_builtin"
    if not builtin_root.is_dir():
        return _fail(
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
        return _fail(
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
    return _ok(f"{seen} builtin plugin manifest(s) parse", data={"count": seen})


def check_no_critical_plugin_disabled() -> CheckResult:
    """Configured ``[plugins].disabled`` must not include boot-critical plugins."""
    from pollypm.config import DEFAULT_CONFIG_PATH, load_config

    if not DEFAULT_CONFIG_PATH.exists():
        return _skip("critical-plugin check skipped (no config)")
    try:
        config = load_config(DEFAULT_CONFIG_PATH)
    except Exception:  # noqa: BLE001
        # Surfaced by check_provider_account_configured already.
        return _skip("critical-plugin check skipped (config parse error)")
    disabled = set(getattr(getattr(config, "plugins", None), "disabled", ()) or ())
    conflicts = sorted(disabled & set(_CRITICAL_PLUGINS_FOR_BOOT))
    if conflicts:
        return _fail(
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
    return _ok("no critical plugin disabled")


def check_plugin_capabilities_no_deprecations() -> CheckResult:
    """Builtin plugin manifests must not use deprecated capability-shape forms.

    The v1 plugin API (see ``plugin_api/v1.py``) migrated from free-form
    capability strings to a structured ``[[capabilities]]`` table. A
    plugin that declares capabilities as a bare list of strings still
    loads but emits a deprecation warning at load time. We flag these
    here so a first-run user does not see mysterious warnings later.
    """
    here = Path(__file__).resolve().parent
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
        return _fail(
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
    return _ok("no deprecated capability shapes in builtin plugins")


# --------------------------------------------------------------------- #
# Migrations
# --------------------------------------------------------------------- #


def _latest_state_migration_version() -> int | None:
    """Return the highest declared migration version for the state DB."""
    try:
        from pollypm.storage.state import StateStore  # noqa: F401
        # The migrations live on the class; avoid instantiating (that opens
        # a real DB). Walk the class var directly.
        from pollypm.storage import state as _state_mod

        migrations = getattr(_state_mod.StateStore, "_MIGRATIONS", None)
        if not migrations:
            return None
        return max(v for v, _, _ in migrations)
    except Exception:  # noqa: BLE001
        return None


def _latest_work_migration_version() -> int | None:
    try:
        from pollypm.work.schema import _WORK_MIGRATIONS

        if not _WORK_MIGRATIONS:
            return None
        return max(v for v, _, _ in _WORK_MIGRATIONS)
    except Exception:  # noqa: BLE001
        return None


def _applied_version_from_sqlite(db_path: Path, table: str) -> int | None:
    """Return ``MAX(version)`` from a schema-version table, or ``None``.

    Uses read-only access (``mode=ro``) so we never mutate a missing DB.
    Returns ``None`` if the DB or the table does not exist — the caller
    interprets that as "no migrations applied yet".
    """
    if not db_path.is_file():
        return None
    uri = f"file:{db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
    except sqlite3.Error:
        return None
    try:
        try:
            row = conn.execute(f"SELECT COALESCE(MAX(version), 0) FROM {table}").fetchone()
        except sqlite3.Error:
            return None
        return int(row[0]) if row and row[0] is not None else 0
    finally:
        conn.close()


def _state_db_candidates() -> list[Path]:
    """Best-effort list of state DB paths to probe.

    Fresh install: no config → nothing to probe, caller skips.
    Otherwise returns the tracked projects' state DB paths.
    """
    from pollypm.config import DEFAULT_CONFIG_PATH, load_config

    if not DEFAULT_CONFIG_PATH.exists():
        return []
    try:
        config = load_config(DEFAULT_CONFIG_PATH)
    except Exception:  # noqa: BLE001
        return []
    out: list[Path] = []
    for project in (getattr(config, "projects", {}) or {}).values():
        if not getattr(project, "tracked", False):
            continue
        candidate = project.path / ".pollypm" / "state.db"
        if candidate.is_file():
            out.append(candidate)
        legacy = project.path / ".pollypm-state" / "state.db"
        if legacy.is_file():
            out.append(legacy)
    return out


def check_state_migrations() -> CheckResult:
    latest = _latest_state_migration_version()
    if latest is None:
        return _skip("state migration check skipped (no migrations defined)")
    candidates = _state_db_candidates()
    if not candidates:
        return _skip("state migration check skipped (no tracked project DBs)")
    behind: list[tuple[Path, int]] = []
    for db_path in candidates:
        applied = _applied_version_from_sqlite(db_path, "schema_version")
        if applied is None or applied < latest:
            behind.append((db_path, applied or 0))
    if behind:
        summary = ", ".join(f"{p} (@{v})" for p, v in behind)
        return _fail(
            f"{len(behind)} state DB(s) behind latest v{latest}: {summary}",
            why=(
                "A stale state DB leads to missing tables / columns at runtime. "
                "Migrations run automatically on StateStore open, but only for "
                "DBs the current process actually touches."
            ),
            fix=(
                "Boot PollyPM to run pending migrations —\n"
                "  pm up\n"
                "Or clear the stale DB (destroys state):\n"
                "  rm <path>\n"
                "Recheck: pm doctor"
            ),
            data={"latest": latest, "behind": [str(p) for p, _ in behind]},
        )
    return _ok(f"state DBs on v{latest}", data={"latest": latest, "count": len(candidates)})


def check_work_migrations() -> CheckResult:
    latest = _latest_work_migration_version()
    if latest is None:
        return _skip("work migration check skipped (no migrations defined)")
    candidates = _state_db_candidates()
    if not candidates:
        return _skip("work migration check skipped (no tracked project DBs)")
    behind: list[tuple[Path, int]] = []
    for db_path in candidates:
        applied = _applied_version_from_sqlite(db_path, "work_schema_version")
        # work_schema_version lives in the same state.db but may not yet
        # exist on DBs that pre-date the work service. Treat "no table"
        # as "v0" — still behind.
        if applied is None or applied < latest:
            behind.append((db_path, applied or 0))
    if behind:
        summary = ", ".join(f"{p} (@{v})" for p, v in behind)
        return _fail(
            f"{len(behind)} work DB(s) behind latest v{latest}: {summary}",
            why=(
                "The work service schema evolves; a DB behind head lacks the "
                "tables the task CLI writes to. Symptoms: 'no such table' errors "
                "from `pm task list`."
            ),
            fix=(
                "Run migrations by booting PollyPM —\n"
                "  pm up\n"
                "Recheck: pm doctor"
            ),
            data={"latest": latest, "behind": [str(p) for p, _ in behind]},
        )
    return _ok(f"work DBs on v{latest}", data={"latest": latest, "count": len(candidates)})


# --------------------------------------------------------------------- #
# Filesystem
# --------------------------------------------------------------------- #


def _pollypm_home() -> Path:
    return Path.home() / ".pollypm"


def check_pollypm_home_writable() -> CheckResult:
    home = _pollypm_home()

    def _fix() -> tuple[bool, str]:
        try:
            home.mkdir(parents=True, exist_ok=True)
            # Touch a throwaway file to prove writability.
            probe = home / ".doctor_probe"
            probe.write_text("ok")
            probe.unlink(missing_ok=True)
            return (True, f"created {home}")
        except Exception as exc:  # noqa: BLE001
            return (False, f"mkdir failed: {exc}")

    if not home.exists():
        return _fail(
            f"~/.pollypm/ does not exist",
            why=(
                "PollyPM writes global config, plugins, and caches under "
                "~/.pollypm/. Its absence means every write path fails."
            ),
            fix=(
                "Create the directory —\n"
                "  mkdir -p ~/.pollypm\n"
                "Or run:  pm doctor --fix\n"
                "Recheck: pm doctor"
            ),
            fixable=True,
            fix_fn=_fix,
        )
    if not os.access(home, os.W_OK):
        return _fail(
            f"~/.pollypm/ is not writable",
            why=(
                "PollyPM must write config, caches, and state under "
                "~/.pollypm/. A read-only home directory breaks every flow."
            ),
            fix=(
                "Fix permissions —\n"
                f"  chmod u+w {home}\n"
                "Recheck: pm doctor"
            ),
        )
    return _ok(f"{home} writable", data={"path": str(home)})


def check_pollypm_plugins_dir() -> CheckResult:
    plugins_dir = _pollypm_home() / "plugins"

    def _fix() -> tuple[bool, str]:
        try:
            plugins_dir.mkdir(parents=True, exist_ok=True)
            return (True, f"created {plugins_dir}")
        except Exception as exc:  # noqa: BLE001
            return (False, f"mkdir failed: {exc}")

    if plugins_dir.is_dir():
        return _ok(f"{plugins_dir} exists", data={"path": str(plugins_dir)})
    return _fail(
        f"~/.pollypm/plugins/ does not exist",
        why=(
            "User-installed plugins live at ~/.pollypm/plugins/<name>/. "
            "The directory is not required for builtins but its absence "
            "breaks `pm plugins install ...`."
        ),
        fix=(
            "Create the directory —\n"
            f"  mkdir -p {plugins_dir}\n"
            "Or run:  pm doctor --fix\n"
            "Recheck: pm doctor"
        ),
        severity="warning",
        fixable=True,
        fix_fn=_fix,
    )


def check_tracked_project_state_parents() -> CheckResult:
    """Each tracked project's ``.pollypm-state/`` parent must exist."""
    from pollypm.config import DEFAULT_CONFIG_PATH, load_config

    if not DEFAULT_CONFIG_PATH.exists():
        return _skip("tracked-project check skipped (no config)")
    try:
        config = load_config(DEFAULT_CONFIG_PATH)
    except Exception:  # noqa: BLE001
        return _skip("tracked-project check skipped (config parse error)")
    missing: list[Path] = []
    for project in (getattr(config, "projects", {}) or {}).values():
        if not getattr(project, "tracked", False):
            continue
        parent = project.path
        if not parent.exists():
            missing.append(parent)
    if missing:
        return _fail(
            f"{len(missing)} tracked project path(s) missing",
            why=(
                "PollyPM tracks projects by filesystem path; a missing path "
                "means `pm scan-projects` will flag them and every per-task "
                "worktree operation fails."
            ),
            fix=(
                "Re-clone or remove the stale projects —\n"
                "  edit ~/.pollypm/pollypm.toml and drop the missing [projects.*] blocks\n"
                "Or re-create the path and re-run `pm scan-projects`.\n"
                "Recheck: pm doctor"
            ),
            data={"missing": [str(p) for p in missing]},
        )
    return _ok("tracked project paths exist")


def check_disk_space() -> CheckResult:
    """At least 1 GB free in $HOME."""
    try:
        usage = shutil.disk_usage(Path.home())
    except Exception as exc:  # noqa: BLE001
        return _skip(f"disk space check skipped ({exc})")
    gb_free = usage.free / (1024 ** 3)
    if gb_free < 1.0:
        return _fail(
            f"only {gb_free:.2f} GB free on $HOME",
            why=(
                "PollyPM writes worktrees, transcripts, and state DBs under "
                "$HOME. Running out of space leaves state partially written "
                "and can corrupt SQLite."
            ),
            fix=(
                "Free up disk space before continuing —\n"
                "  du -sh ~/* | sort -h | tail\n"
                "  rm -rf ~/.cache/  # if you know it's safe\n"
                "Recheck: pm doctor"
            ),
            data={"free_gb": round(gb_free, 2)},
        )
    return _ok(f"{gb_free:.1f} GB free on $HOME", data={"free_gb": round(gb_free, 2)})


# --------------------------------------------------------------------- #
# Tmux session state
# --------------------------------------------------------------------- #


def check_tmux_daemon() -> CheckResult:
    if _tool_path("tmux") is None:
        return _skip("tmux daemon check skipped (tmux not installed)")
    rc, out = _run_cmd(["tmux", "list-sessions"], timeout=1.5)
    # rc 0 = daemon up with sessions. rc 1 with 'no server running' = daemon
    # not up but tmux itself is fine. Anything else = surprise.
    if rc == 0:
        return _ok("tmux daemon running", data={"sessions": len(out.splitlines())})
    if "no server" in out.lower() or "no sessions" in out.lower() or rc == 1:
        return _ok("tmux daemon not running (ok — will start on demand)")
    return _fail(
        f"tmux list-sessions errored: {out[:80]!r}",
        why=(
            "An unhealthy tmux daemon prevents session creation and can leave "
            "PollyPM wedged on `pm up`."
        ),
        fix=(
            "Restart the tmux server —\n"
            "  tmux kill-server\n"
            "Recheck: pm doctor"
        ),
        severity="warning",
        data={"rc": rc, "raw": out[:200]},
    )


def check_storage_closet_reachable() -> CheckResult:
    """Verify the storage-closet session either exists or can be created.

    A creation attempt here is intrusive; we only *probe* existence and
    check that tmux is installed (so creation would be possible). Full
    creation is left to `pm up`.
    """
    if _tool_path("tmux") is None:
        return _skip("storage-closet check skipped (tmux not installed)")
    rc, out = _run_cmd(["tmux", "has-session", "-t", "pollypm-storage-closet"], timeout=1.5)
    if rc == 0:
        return _ok("pollypm-storage-closet session exists")
    # rc 1 = session doesn't exist. That's fine — it will be created on first `pm up`.
    return _ok("pollypm-storage-closet will be created on first `pm up`")


def check_no_stale_dead_panes() -> CheckResult:
    """Warn when the storage closet has dead panes (prior-crash indicator).

    Requires an active tmux daemon AND the storage closet session.
    """
    if _tool_path("tmux") is None:
        return _skip("dead-panes check skipped (tmux not installed)")
    rc, _ = _run_cmd(["tmux", "has-session", "-t", "pollypm-storage-closet"], timeout=1.5)
    if rc != 0:
        return _skip("dead-panes check skipped (storage closet not running)")
    rc, out = _run_cmd(
        ["tmux", "list-panes", "-s", "-t", "pollypm-storage-closet", "-F", "#{pane_dead}"],
        timeout=1.5,
    )
    if rc != 0:
        return _skip("dead-panes check skipped (list-panes failed)")
    dead = sum(1 for line in out.splitlines() if line.strip() == "1")

    def _fix() -> tuple[bool, str]:
        r, msg = _run_cmd(
            ["tmux", "kill-pane", "-a", "-t", "pollypm-storage-closet"], timeout=2.0,
        )
        # kill-pane -a keeps only the current pane; it should always return 0
        # when the session exists.
        if r == 0:
            return (True, "killed stale storage-closet panes")
        return (False, f"kill-pane failed: {msg}")

    if dead > 0:
        return _fail(
            f"{dead} stale dead pane(s) in storage closet",
            why=(
                "Dead panes in pollypm-storage-closet mean a prior worker or "
                "agent crashed and the carcass was not reaped. They clutter "
                "the cockpit and can confuse pane-based routing."
            ),
            fix=(
                "Reap dead panes —\n"
                "  tmux kill-pane -a -t pollypm-storage-closet\n"
                "Or run:  pm doctor --fix\n"
                "Recheck: pm doctor"
            ),
            severity="warning",
            fixable=True,
            fix_fn=_fix,
            data={"dead": dead},
        )
    return _ok("no stale dead panes in storage closet")


# --------------------------------------------------------------------- #
# Network reachability
# --------------------------------------------------------------------- #


def _tcp_reachable(host: str, port: int = 443, timeout: float = 1.5) -> tuple[bool, str]:
    """Open a TCP connection to ``host:port`` and close it immediately.

    We deliberately avoid HTTPS (no ``ssl`` overhead, no cert-chain
    surprises). TCP connect is enough to distinguish "blocked at the
    firewall" from "endpoint reachable"; proper API health belongs to
    the provider plugins.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return (True, "")
    except socket.gaierror as exc:
        return (False, f"dns failure: {exc}")
    except (OSError, TimeoutError) as exc:
        return (False, f"{type(exc).__name__}: {exc}")


def _configured_providers() -> set[str]:
    """Return the set of ProviderKind values in the configured accounts."""
    from pollypm.config import DEFAULT_CONFIG_PATH, load_config

    if not DEFAULT_CONFIG_PATH.exists():
        return set()
    try:
        config = load_config(DEFAULT_CONFIG_PATH)
    except Exception:  # noqa: BLE001
        return set()
    providers: set[str] = set()
    for account in (getattr(config, "accounts", {}) or {}).values():
        provider = getattr(account, "provider", None)
        if provider is not None:
            providers.add(str(getattr(provider, "value", provider)))
    return providers


def _network_check(host: str, purpose: str) -> CheckResult:
    ok, detail = _tcp_reachable(host)
    if ok:
        return _ok(f"{host} reachable", data={"host": host})
    return _fail(
        f"{host} unreachable ({detail})",
        why=(
            f"PollyPM reaches {host} for {purpose}. A blocked endpoint "
            "means the dependent flows silently degrade or hang."
        ),
        fix=(
            "Check your network and firewall —\n"
            f"  curl -sS https://{host} >/dev/null\n"
            "  ping -c 2 " + host + "\n"
            "If you are on a corporate VPN, allowlist the host.\n"
            "Recheck: pm doctor"
        ),
        severity="warning",
        data={"host": host, "detail": detail},
    )


def check_network_anthropic() -> CheckResult:
    providers = _configured_providers()
    if not providers or "claude" not in providers:
        return _skip("anthropic reachability skipped (no claude account)")
    # Probe both the web origin (login/auth) and the API host.
    ok_web, detail_web = _tcp_reachable("claude.ai")
    ok_api, detail_api = _tcp_reachable("api.anthropic.com")
    if ok_web and ok_api:
        return _ok("claude.ai + api.anthropic.com reachable")
    failures = []
    if not ok_web:
        failures.append(f"claude.ai ({detail_web})")
    if not ok_api:
        failures.append(f"api.anthropic.com ({detail_api})")
    return _fail(
        f"anthropic endpoints unreachable: {', '.join(failures)}",
        why=(
            "The Claude provider needs both claude.ai (login/auth) and "
            "api.anthropic.com (chat). Either being blocked breaks login, "
            "session spawn, or runtime."
        ),
        fix=(
            "Check connectivity —\n"
            "  curl -sS https://claude.ai >/dev/null\n"
            "  curl -sS https://api.anthropic.com >/dev/null\n"
            "If on a corporate VPN, allowlist both hosts.\n"
            "Recheck: pm doctor"
        ),
        severity="warning",
        data={"failures": failures},
    )


def check_network_openai() -> CheckResult:
    providers = _configured_providers()
    if not providers or "codex" not in providers:
        return _skip("openai reachability skipped (no codex account)")
    return _network_check("api.openai.com", "the Codex provider")


def check_network_github() -> CheckResult:
    return _network_check("github.com", "gh-backed issue sync and PR flows")


# --------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------- #


def _registered_checks() -> list[Check]:
    """Top-down ordered check list.

    Order matches the categories in the doctor spec so a first-run user
    reads top-to-bottom: system prerequisites first (because a missing
    Python breaks everything else), then install state, plugins,
    migrations, filesystem, tmux, network.
    """
    return [
        # System prerequisites
        Check("python-version", check_python_version, "system"),
        Check("tmux", check_tmux, "system"),
        Check("git", check_git, "system"),
        Check("gh-installed", check_gh_installed, "system"),
        Check("gh-authenticated", check_gh_authenticated, "system"),
        Check("uv", check_uv, "system"),
        Check("terminal-color", check_terminal_color_support, "system", severity="warning"),
        # Install state
        Check("pm-binary", check_pm_binary_resolves, "install"),
        Check("pollypm-version-matches", check_installed_version_matches_pyproject, "install", severity="warning"),
        Check("config-file", check_config_file, "install"),
        Check("provider-account", check_provider_account_configured, "install"),
        # Plugins
        Check("builtin-plugin-manifests", check_builtin_plugin_manifests, "plugins"),
        Check("critical-plugins-enabled", check_no_critical_plugin_disabled, "plugins"),
        Check("plugin-capability-shapes", check_plugin_capabilities_no_deprecations, "plugins", severity="warning"),
        # Migrations
        Check("state-migrations", check_state_migrations, "migrations"),
        Check("work-migrations", check_work_migrations, "migrations"),
        # Filesystem
        Check("pollypm-home-writable", check_pollypm_home_writable, "filesystem"),
        Check("pollypm-plugins-dir", check_pollypm_plugins_dir, "filesystem", severity="warning"),
        Check("tracked-project-paths", check_tracked_project_state_parents, "filesystem"),
        Check("disk-space", check_disk_space, "filesystem"),
        # Tmux session state
        Check("tmux-daemon", check_tmux_daemon, "tmux"),
        Check("storage-closet", check_storage_closet_reachable, "tmux"),
        Check("stale-dead-panes", check_no_stale_dead_panes, "tmux", severity="warning"),
        # Network
        Check("network-github", check_network_github, "network", severity="warning"),
        Check("network-anthropic", check_network_anthropic, "network", severity="warning"),
        Check("network-openai", check_network_openai, "network", severity="warning"),
    ]


def run_checks(checks: Iterable[Check] | None = None) -> DoctorReport:
    """Execute each check and return a :class:`DoctorReport`.

    A check that raises is converted into a failure so the runner itself
    never crashes — this is the *doctor*, after all.
    """
    selected = list(checks) if checks is not None else _registered_checks()
    report = DoctorReport()
    t0 = time.monotonic()
    for check in selected:
        try:
            result = check.run()
        except Exception as exc:  # noqa: BLE001
            result = CheckResult(
                passed=False,
                status=f"check raised {type(exc).__name__}: {exc}",
                severity="error",
                why=(
                    f"Doctor check '{check.name}' crashed — this is a bug in "
                    "the check itself, not your environment."
                ),
                fix=(
                    "Open an issue with the traceback —\n"
                    "  pm doctor --json  # capture full output\n"
                    "  gh issue create --title 'pm doctor crash in " + check.name + "'"
                ),
            )
        # Allow a check to return a lower severity than its declared
        # default (e.g. a warning-only probe). Never escalate.
        if not result.passed and result.severity == "error" and check.severity == "warning":
            result.severity = "warning"
        report.results.append((check, result))
    report.duration_seconds = time.monotonic() - t0
    return report


# --------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------- #


_TICK = "\u2713"  # ✓
_CROSS = "\u2717"  # ✗
_WARN = "!"
_SKIP = "-"


def render_human(report: DoctorReport) -> str:
    """Return the classic checklist + per-failure detail block."""
    lines: list[str] = []
    for check, result in report.results:
        if result.skipped:
            glyph = _SKIP
        elif result.passed:
            glyph = _TICK
        elif result.severity == "warning":
            glyph = _WARN
        else:
            glyph = _CROSS
        status = result.status or ("ok" if result.passed else "fail")
        lines.append(f"{glyph} {check.name}: {status}")

    passed = report.passed_count
    total = len(report.results)
    errors = len(report.errors)
    warnings = len(report.warnings)
    skipped = report.skipped_count
    lines.append("")
    lines.append(
        f"Summary: {passed}/{total} passed, {warnings} warning(s), "
        f"{errors} error(s), {skipped} skipped "
        f"({report.duration_seconds:.2f}s)"
    )

    failures = [
        (c, r) for c, r in report.results
        if not r.passed and not r.skipped
    ]
    if failures:
        lines.append("")
        lines.append("Failures:")
        for check, result in failures:
            glyph = _WARN if result.severity == "warning" else _CROSS
            lines.append("")
            lines.append(f"{glyph} {check.name}: {result.status}")
            if result.why:
                lines.append("")
                lines.append(f"  Why: {result.why}")
            if result.fix:
                lines.append("")
                # Indent each line of the fix block by 2 spaces so the
                # whole block reads as a unit in the checklist.
                for fix_line in result.fix.splitlines():
                    lines.append(f"  {fix_line}" if fix_line else "")
    return "\n".join(lines)


def render_json(report: DoctorReport) -> str:
    payload = {
        "ok": report.ok,
        "duration_seconds": round(report.duration_seconds, 4),
        "summary": {
            "total": len(report.results),
            "passed": report.passed_count,
            "warnings": len(report.warnings),
            "errors": len(report.errors),
            "skipped": report.skipped_count,
        },
        "checks": [
            {
                "name": check.name,
                "category": check.category,
                "passed": result.passed,
                "skipped": result.skipped,
                "severity": result.severity,
                "status": result.status,
                "why": result.why,
                "fix": result.fix,
                "fixable": result.fixable,
                "data": result.data,
            }
            for check, result in report.results
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True)


# --------------------------------------------------------------------- #
# Auto-fix
# --------------------------------------------------------------------- #


def apply_fixes(report: DoctorReport) -> list[tuple[str, bool, str]]:
    """Invoke each fixable failure's ``fix_fn``.

    Returns a list of ``(check_name, success, message)`` tuples. Safe to
    call on a passing report (no-op). Does not re-run checks; callers
    can re-run :func:`run_checks` afterwards to confirm.
    """
    results: list[tuple[str, bool, str]] = []
    for check, result in report.results:
        if result.passed or result.skipped:
            continue
        if not result.fixable or result.fix_fn is None:
            continue
        try:
            success, message = result.fix_fn()
        except Exception as exc:  # noqa: BLE001
            results.append((check.name, False, f"fix_fn raised {exc}"))
            continue
        results.append((check.name, success, message))
    return results
