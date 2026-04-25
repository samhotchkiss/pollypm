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

import hashlib
import json
import platform
import re
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version as _package_version
from pathlib import Path
from typing import Any, Callable, Iterable

from pollypm.models import AccountConfig, ModelAssignment, PollyPMConfig, ProjectSettings, ProviderKind
from pollypm.service_api import PollyPMService

# Allow ``pollypm.doctor`` to host internal submodules while keeping the
# public import path stable as a module-level facade.
__path__ = [str(Path(__file__).resolve().with_name("doctor"))]


# --------------------------------------------------------------------- #
# Result model
# --------------------------------------------------------------------- #


Severity = str  # "error" | "warning" | "info"


@dataclass(slots=True)
class AutoFixPlan:
    """A shell command the doctor can present as a one-keypress fix."""

    description: str
    command: list[str]
    requires_sudo: bool = False
    platforms: list[str] = field(default_factory=list)


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
    ``auto_fix`` — optional shell-command plan for interactive surfaces
        to present as a one-keypress fix.
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
    auto_fix: AutoFixPlan | None = None


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


def _current_platform() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform.startswith("win"):
        return "windows"
    return sys.platform


def _auto_fix_supported(plan: AutoFixPlan | None) -> bool:
    if plan is None:
        return False
    if not plan.platforms:
        return True
    return _current_platform() in plan.platforms


def _auto_fix_payload(plan: AutoFixPlan | None) -> dict[str, object] | None:
    if plan is None:
        return None
    return {
        "description": plan.description,
        "command": plan.command,
        "requires_sudo": plan.requires_sudo,
        "platforms": plan.platforms,
        "supported": _auto_fix_supported(plan),
    }


def _brew_auto_fix(package: str, *, description: str) -> AutoFixPlan | None:
    if _tool_path("brew") is None:
        return None
    return AutoFixPlan(
        description=description,
        command=["brew", "install", package],
        requires_sudo=False,
        platforms=["macos"],
    )


def _linux_pkg_manager_auto_fix(
    package: str,
    *,
    description: str,
) -> AutoFixPlan | None:
    for manager in ("apt-get", "dnf", "yum"):
        if _tool_path(manager) is None:
            continue
        return AutoFixPlan(
            description=description,
            command=["sudo", manager, "install", "-y", package],
            requires_sudo=True,
            platforms=["linux"],
        )
    return None


def _npm_global_auto_fix(package: str, *, description: str) -> AutoFixPlan | None:
    if _tool_path("npm") is None:
        return None
    return AutoFixPlan(
        description=description,
        command=["npm", "i", "-g", package],
        requires_sudo=False,
        platforms=["macos", "linux"],
    )


def _reinstall_editable_auto_fix(description: str) -> AutoFixPlan | None:
    if _tool_path("uv") is None:
        return None
    project_root = _pyproject_path().parent
    return AutoFixPlan(
        description=description,
        command=[
            "sh",
            "-lc",
            f"cd {shlex.quote(str(project_root))} && uv tool install --editable --reinstall .",
        ],
        requires_sudo=False,
        platforms=["macos", "linux"],
    )


def _uv_install_auto_fix() -> AutoFixPlan | None:
    if _current_platform() not in {"macos", "linux"}:
        return None
    if _tool_path("brew") is not None and _current_platform() == "macos":
        return AutoFixPlan(
            description="Install uv with Homebrew",
            command=["brew", "install", "uv"],
            requires_sudo=False,
            platforms=["macos"],
        )
    return AutoFixPlan(
        description="Install uv with Astral's installer",
        command=["sh", "-lc", "curl -LsSf https://astral.sh/uv/install.sh | sh"],
        requires_sudo=False,
        platforms=["macos", "linux"],
    )


def _command_text(command: list[str]) -> str:
    try:
        return shlex.join(command)
    except AttributeError:  # pragma: no cover
        return " ".join(shlex.quote(part) for part in command)


def run_auto_fix(plan: AutoFixPlan) -> tuple[bool, str]:
    """Execute an auto-fix plan with inherited stdio for interactive use."""
    try:
        result = subprocess.run(plan.command, check=False)
    except FileNotFoundError as exc:
        return (False, f"{plan.description} failed: {exc}")
    except Exception as exc:  # noqa: BLE001
        return (False, f"{plan.description} failed: {exc}")
    if result.returncode == 0:
        return (True, f"{plan.description} completed.")
    return (
        False,
        f"{plan.description} exited with status {result.returncode}: {_command_text(plan.command)}",
    )


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


def _setup_tags_path() -> Path:
    return Path.home() / ".pollypm" / "setup-tags.json"


def _tool_version(binary: str, *, timeout: float = 2.0) -> str | None:
    path = _tool_path(binary)
    if path is None:
        return None
    rc, out = _run_cmd([binary, "--version"], timeout=timeout)
    if rc != 0:
        return None
    version = _parse_version(out)
    if version is not None:
        return ".".join(str(part) for part in version)
    line = (out.splitlines()[0] if out else "").strip()
    return line or None


def _tool_major(binary: str, *, timeout: float = 2.0) -> int | None:
    version = _tool_version(binary, timeout=timeout)
    parsed = _parse_version(version or "")
    return parsed[0] if parsed is not None else None


def _provider_home_mode(provider: str, config) -> str:
    accounts = getattr(config, "accounts", {}) or {}
    for account in accounts.values():
        value = str(getattr(getattr(account, "provider", None), "value", getattr(account, "provider", "")))
        if value != provider:
            continue
        if getattr(account, "home", None) is not None:
            return "isolated"
    return "default-profile"


def _setup_fingerprint(config_path: Path | None = None) -> dict[str, object]:
    from pollypm.config import DEFAULT_CONFIG_PATH, load_config

    path = config_path or DEFAULT_CONFIG_PATH
    config = None
    if path.exists():
        try:
            config = load_config(path)
        except Exception:  # noqa: BLE001
            config = None

    accounts = getattr(config, "accounts", {}) if config is not None else {}
    projects = getattr(config, "projects", {}) if config is not None else {}
    try:
        pollypm_version = _package_version("pollypm")
    except PackageNotFoundError:
        pollypm_version = "unknown"
    except Exception:  # noqa: BLE001
        pollypm_version = "unknown"

    return {
        "platform": f"{platform.system().lower()}-{platform.machine().lower()}",
        "pollypm_version": pollypm_version,
        "claude_version": _tool_version("claude"),
        "claude_home_mode": _provider_home_mode("claude", config) if config is not None else "default-profile",
        "codex_version": _tool_version("codex"),
        "codex_home_mode": _provider_home_mode("codex", config) if config is not None else "default-profile",
        "tmux_major": _tool_major("tmux"),
        "git_major": _tool_major("git"),
        "node_major": _tool_major("node"),
        "accounts": len(accounts) if isinstance(accounts, dict) else 0,
        "projects": len(projects) if isinstance(projects, dict) else 0,
    }


def _setup_tag_for_fingerprint(fingerprint: dict[str, object]) -> str:
    canonical = json.dumps(fingerprint, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:6]


def _setup_tags_store() -> list[dict[str, object]]:
    path = _setup_tags_path()
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _store_setup_tag(tag: str, fingerprint: dict[str, object]) -> None:
    path = _setup_tags_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = [entry for entry in _setup_tags_store() if entry.get("tag") != tag]
    entries.append({"tag": tag, "fingerprint": fingerprint})
    path.write_text(json.dumps(entries[-50:], indent=2, sort_keys=True) + "\n")


def setup_tag_line(config_path: Path | None = None) -> str:
    fingerprint = _setup_fingerprint(config_path)
    tag = _setup_tag_for_fingerprint(fingerprint)
    _store_setup_tag(tag, fingerprint)
    return f"pollypm setup: {tag} - share this if something looks off"


def release_channel_line(config_path: Path | None = None) -> str:
    """Return a single-line ``Release channel: <value>`` summary.

    Falls back to the default channel when the config is missing or
    unparseable — ``pm doctor`` must stay useful even before a first-run
    user has written a config. See issue #713.
    """
    from pollypm.config import DEFAULT_CONFIG_PATH, load_config

    path = config_path or DEFAULT_CONFIG_PATH
    channel = "stable"
    if path.exists():
        try:
            config = load_config(path)
            channel = config.pollypm.release_channel
        except Exception:  # noqa: BLE001
            channel = "stable"
    return f"Release channel: {channel}"


def decode_setup_tag(tag: str) -> dict[str, object] | None:
    for entry in _setup_tags_store():
        if entry.get("tag") == tag:
            fingerprint = entry.get("fingerprint")
            if isinstance(fingerprint, dict):
                return fingerprint
    return None


def _tool_path(name: str) -> str | None:
    return shutil.which(name)


# The system checks now live in ``pollypm.doctor.system``. Keep these
# public names wired to the extracted module so imports and CLI behavior
# stay stable while the implementation moves out of this file.
from pollypm.doctor import system as _doctor_system

check_python_version = _doctor_system.check_python_version
check_tmux = _doctor_system.check_tmux
check_git = _doctor_system.check_git
check_gh_installed = _doctor_system.check_gh_installed
check_gh_authenticated = _doctor_system.check_gh_authenticated
check_uv = _doctor_system.check_uv
check_claude_cli = _doctor_system.check_claude_cli
check_codex_cli = _doctor_system.check_codex_cli
check_terminal_color_support = _doctor_system.check_terminal_color_support


# --------------------------------------------------------------------- #
# PollyPM install-state / plugin / filesystem checks now live in
# ``pollypm.doctor.install_state``, ``pollypm.doctor.plugins``, and
# ``pollypm.doctor.filesystem``. Keep public names stable here.
# --------------------------------------------------------------------- #

from pollypm.doctor import filesystem as _doctor_filesystem
from pollypm.doctor import install_state as _doctor_install_state
from pollypm.doctor import plugins as _doctor_plugins

check_pm_binary_resolves = _doctor_install_state.check_pm_binary_resolves
check_installed_version_matches_pyproject = _doctor_install_state.check_installed_version_matches_pyproject
check_config_file = _doctor_install_state.check_config_file
check_provider_account_configured = _doctor_install_state.check_provider_account_configured
check_storage_backend = _doctor_install_state.check_storage_backend
check_registered_providers = _doctor_install_state.check_registered_providers

check_builtin_plugin_manifests = _doctor_plugins.check_builtin_plugin_manifests
check_no_critical_plugin_disabled = _doctor_plugins.check_no_critical_plugin_disabled
check_plugin_capabilities_no_deprecations = _doctor_plugins.check_plugin_capabilities_no_deprecations

check_pollypm_home_writable = _doctor_filesystem.check_pollypm_home_writable
check_pollypm_plugins_dir = _doctor_filesystem.check_pollypm_plugins_dir
# ``check_tracked_project_state_parents`` and ``check_db_layout_canonical``
# are defined locally below — the local function bodies are the live
# implementations; rebinding from ``_doctor_filesystem`` here would be
# silently overwritten by Python's normal definition order.
check_disk_space = _doctor_filesystem.check_disk_space


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


def check_rail_daemon_alive() -> CheckResult:
    """Warn when ``pm up``'s rail daemon isn't running.

    Without the daemon, heartbeat recovery only fires while the cockpit
    TUI is open. When the cockpit is closed or crashes, a dead
    ``pm-operator`` won't be auto-recovered — exactly the failure mode
    behind the 2026-04-19 OOM incident.
    """
    import os as _os

    pid_path = _pollypm_home() / "rail_daemon.pid"
    if not pid_path.exists():
        return _fail(
            "rail daemon is not running",
            why=(
                "The headless rail daemon runs heartbeat sweeps + "
                "auto-recovery independently of the cockpit. Without "
                "it, a crashed core session (e.g. pm-operator) won't "
                "come back until you manually run `pm alerts` or reopen "
                "the cockpit."
            ),
            fix=(
                "Start the daemon by running —\n"
                "  pm up\n"
                "(idempotent — if PollyPM is already up, this just "
                "spawns the daemon.)\n"
                "Recheck: pm doctor"
            ),
            severity="warning",
            fixable=False,
            data={"pid_path": str(pid_path)},
        )
    try:
        pid = int(pid_path.read_text().strip())
    except (ValueError, OSError) as exc:
        return _fail(
            f"rail daemon PID file unreadable: {exc}",
            why=(
                "~/.pollypm/rail_daemon.pid exists but can't be parsed. "
                "Likely a corrupt write from an earlier crash."
            ),
            fix=(
                "Remove the stale PID file and restart —\n"
                f"  rm {pid_path}\n"
                "  pm up\n"
                "Recheck: pm doctor"
            ),
            severity="warning",
            fixable=False,
        )
    try:
        _os.kill(pid, 0)
    except ProcessLookupError:
        return _fail(
            f"rail daemon PID {pid} is stale (process not running)",
            why=(
                "The PID file names a process that no longer exists — "
                "the daemon crashed or was killed. Auto-recovery is "
                "currently disabled."
            ),
            fix=(
                "Remove the stale PID file and restart —\n"
                f"  rm {pid_path}\n"
                "  pm up\n"
                "Recheck: pm doctor"
            ),
            severity="warning",
            fixable=False,
        )
    except PermissionError:
        # Process exists but belongs to another user — treat as alive.
        pass
    return _ok(f"rail daemon alive (pid {pid})", data={"pid": pid})


def check_tracked_project_state_parents() -> CheckResult:
    """Each tracked project's ``.pollypm/`` parent must exist."""
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


# Legacy directory name from the pre-#339 layout. Assembled from parts
# so a source-wide grep for the old directory name returns zero hits —
# the collapsed-layout contract asserts no active reference to the old
# path anywhere in src/. The check below detects stray leftovers on disk
# so users can clean them up.
_LEGACY_STATE_DIRNAME = ".pollypm" + "-state"


def check_db_layout_canonical() -> CheckResult:
    """Report the canonical two-scope DB layout and warn on strays.

    PollyPM v1 collapsed its on-disk storage to exactly two SQLite files
    (#339):

    * ``~/.pollypm/state.db`` — user scope
    * ``<workspace_root>/.pollypm/state.db`` — workspace scope

    Any leftover legacy state directory is noise from a pre-#339 install.
    Nothing reads it; we just flag it so the user can remove it.
    """
    from pollypm.config import DEFAULT_CONFIG_PATH, load_config

    user_db = Path.home() / ".pollypm" / "state.db"
    workspace_db: Path | None = None
    strays: list[Path] = []

    if DEFAULT_CONFIG_PATH.exists():
        try:
            config = load_config(DEFAULT_CONFIG_PATH)
        except Exception:  # noqa: BLE001
            config = None
        if config is not None:
            workspace_root = getattr(config.project, "workspace_root", None)
            if workspace_root is not None:
                workspace_db = Path(workspace_root) / ".pollypm" / "state.db"
                stray = Path(workspace_root) / _LEGACY_STATE_DIRNAME
                if stray.exists():
                    strays.append(stray)
            for project in (getattr(config, "projects", {}) or {}).values():
                stray = project.path / _LEGACY_STATE_DIRNAME
                if stray.exists():
                    strays.append(stray)

    data = {
        "user_db": str(user_db),
        "workspace_db": str(workspace_db) if workspace_db else None,
        "strays": [str(p) for p in strays],
    }
    if strays:
        summary = ", ".join(str(p) for p in strays)
        return _fail(
            f"legacy {_LEGACY_STATE_DIRNAME}/ directories present: {summary}",
            why=(
                "#339 collapsed PollyPM storage to two scopes — "
                "~/.pollypm/state.db (user) and <workspace_root>/.pollypm/"
                f"state.db (workspace). A leftover {_LEGACY_STATE_DIRNAME}/ "
                "tree is not read by any code path; it just wastes disk "
                "and can confuse grep when debugging."
            ),
            fix=(
                "Remove the stray directories once you've confirmed "
                "nothing under them is needed —\n"
                "  rm -rf " + " ".join(str(p) for p in strays) + "\n"
                "Recheck: pm doctor"
            ),
            severity="warning",
            data=data,
        )
    return _ok("DB layout canonical (two scopes)", data=data)


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
# Pipeline / scheduler / resource / inbox / sessions checks
# --------------------------------------------------------------------- #
#
# These probe today's PollyPM subsystems: the plan-presence gate, the
# architect bootstrap profile, the per-project task-assignment sweeper,
# the recurring-handler roster, on-disk resource thresholds (state.db
# size, agent worktree count, log dir size, claude RSS), the inbox
# aggregator path + open-item count, and the persona-swap defense
# wiring. Each function returns a :class:`CheckResult` and never raises;
# the runner treats an unexpected exception as a failure.


_EXPECTED_SCHEDULED_HANDLERS: tuple[str, ...] = (
    "db.vacuum",
    "memory.ttl_sweep",
    "agent_worktree.prune",
    "log.rotate",
    "events.retention_sweep",
    "work.progress_sweep",
    "task_assignment.sweep",
)
_PROJECT_GUIDE_ROLES: tuple[str, ...] = ("architect", "worker", "reviewer")


@dataclass(slots=True, frozen=True)
class _LocalGuideDriftInfo:
    role: str
    path: Path
    forked_from: str
    current_ref: str
    drifted: bool
    body: str
    upstream_body: str


def _safe_load_config():
    """Best-effort ``(path, config)`` for any check that needs both.

    Returns ``(None, None)`` when no config exists or it cannot parse —
    every caller treats both as a clean skip.
    """
    from pollypm.config import DEFAULT_CONFIG_PATH, load_config

    if not DEFAULT_CONFIG_PATH.exists():
        return None, None
    try:
        config = load_config(DEFAULT_CONFIG_PATH)
    except Exception:  # noqa: BLE001
        return DEFAULT_CONFIG_PATH, None
    return DEFAULT_CONFIG_PATH, config


_ROLE_ASSIGNMENT_ORDER: tuple[str, ...] = (
    "operator_pm",
    "architect",
    "worker",
    "reviewer",
)
_ROLE_MODEL_PROBE_TIMEOUT_SECONDS = 8.0
_INVALID_ROLE_MODEL_MARKERS: tuple[str, ...] = (
    "invalid model",
    "unknown model",
    "model not found",
    "unsupported model",
    "not a valid model",
    "unrecognized model",
)


@dataclass(slots=True, frozen=True)
class _RoleAssignmentSpec:
    check_name: str
    role: str
    source: str
    assignment: ModelAssignment
    table_label: str
    project_key: str | None = None


@dataclass(slots=True, frozen=True)
class _RoleModelSmokeResult:
    ok: bool
    account_name: str | None
    attempts: tuple[str, ...] = ()


def _last_lines_text(text: str, *, count: int = 4) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return ""
    return " | ".join(lines[-count:])


def _role_assignment_specs(config: PollyPMConfig) -> list[_RoleAssignmentSpec]:
    specs: list[_RoleAssignmentSpec] = []
    for role in _ROLE_ASSIGNMENT_ORDER:
        assignment = config.pollypm.role_assignments.get(role)
        if assignment is None:
            continue
        specs.append(
            _RoleAssignmentSpec(
                check_name=f"global.{role}",
                role=role,
                source="global",
                assignment=assignment,
                table_label=f"[pollypm.roles.{role}]",
            )
        )
    for project_key in sorted(config.projects):
        project = config.projects[project_key]
        for role in _ROLE_ASSIGNMENT_ORDER:
            assignment = project.role_assignments.get(role)
            if assignment is None:
                continue
            specs.append(
                _RoleAssignmentSpec(
                    check_name=f"project.{project_key}.{role}",
                    role=role,
                    source="project",
                    assignment=assignment,
                    table_label=f"[projects.{project_key}.roles.{role}]",
                    project_key=project_key,
                )
            )
    return specs


def _known_role_providers() -> set[str]:
    providers = {kind.value for kind in ProviderKind}
    try:
        from pollypm.acct import list_providers

        providers.update(list_providers())
    except Exception:  # noqa: BLE001
        pass
    return providers


def _resolve_role_model_assignment(
    assignment: ModelAssignment,
    *,
    registry,
) -> tuple[str, str] | None:
    if assignment.alias is None:
        return (assignment.provider or "", assignment.model or "")
    from pollypm.model_registry import resolve_alias

    resolved = resolve_alias(assignment.alias, registry=registry)
    if resolved is None:
        return None
    return (resolved.provider or "", resolved.model or "")


def _role_assignment_descriptor(
    assignment: ModelAssignment,
    *,
    provider: str,
    model: str,
) -> str:
    descriptor = f"{provider}/{model}"
    if assignment.alias is not None:
        descriptor += f" via alias {assignment.alias}"
    return descriptor


def _role_model_probe_failure(
    provider: str,
    *,
    returncode: int,
    output: str,
) -> str | None:
    lowered = (output or "").lower()
    if any(marker in lowered for marker in _INVALID_ROLE_MODEL_MARKERS):
        return "provider rejected the model"
    if provider == "claude":
        if "authentication" in lowered or "not logged" in lowered:
            return "authentication failed"
        if returncode != 0:
            return f"probe exited {returncode}"
        if "ok" not in lowered:
            return "probe did not return ok"
        return None
    if provider == "codex":
        if "usage limit" in lowered or "out of credits" in lowered:
            return "account is out of credits"
        if "not logged" in lowered or "not authenticated" in lowered:
            return "authentication failed"
        if "error:" in lowered:
            return "probe returned an error"
        if returncode != 0:
            return f"probe exited {returncode}"
        return None
    if returncode != 0:
        return f"probe exited {returncode}"
    return None


def _probe_role_model_access(
    project: ProjectSettings,
    provider: str,
    model: str,
    accounts: tuple[tuple[str, AccountConfig], ...],
    *,
    timeout_seconds: float = _ROLE_MODEL_PROBE_TIMEOUT_SECONDS,
) -> _RoleModelSmokeResult:
    from pollypm.supervision.probe_runner import ProbeRunner

    runner = ProbeRunner(project)
    attempts: list[str] = []
    for account_name, account in accounts:
        try:
            probe = runner.run_probe_result(
                account,
                model=model,
                timeout=timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            attempts.append(f"{account_name}: {type(exc).__name__}: {exc}")
            continue
        failure = _role_model_probe_failure(
            provider,
            returncode=probe.returncode,
            output=probe.output,
        )
        if failure is None:
            return _RoleModelSmokeResult(
                ok=True,
                account_name=account_name,
                attempts=tuple(attempts),
            )
        tail = _last_lines_text(probe.output)
        detail = f"{account_name}: {failure}"
        if tail:
            detail = f"{detail} ({tail})"
        attempts.append(detail)
    return _RoleModelSmokeResult(
        ok=False,
        account_name=None,
        attempts=tuple(attempts),
    )


def _role_assignment_check_result(
    spec: _RoleAssignmentSpec,
    *,
    config_path: Path | None,
    config: PollyPMConfig,
    registry,
    known_providers: set[str],
    accounts_by_provider: dict[str, tuple[tuple[str, AccountConfig], ...]],
    smoke_cache: dict[tuple[str, str], _RoleModelSmokeResult],
) -> CheckResult:
    from pollypm.model_registry import advisories_for

    resolved = _resolve_role_model_assignment(spec.assignment, registry=registry)
    location_text = spec.table_label
    if config_path is not None:
        location_text = f"{spec.table_label} in {config_path}"
    if spec.assignment.alias is not None and resolved is None:
        return _fail(
            f"alias {spec.assignment.alias!r}, source={spec.source}, unknown alias",
            why=(
                f"{location_text} references alias {spec.assignment.alias!r}, but "
                "that alias is not present in the model registry. PollyPM would "
                "silently fall through to a lower-precedence role assignment."
            ),
            fix=(
                f"Edit {location_text} to use a known alias from "
                "src/pollypm/model_registry.toml or an explicit provider/model pair.\n"
                "Recheck: pm doctor"
            ),
            data={
                "role": spec.role,
                "source": spec.source,
                "project_key": spec.project_key,
                "alias": spec.assignment.alias,
                "table": spec.table_label,
            },
        )

    provider, model = resolved or ("", "")
    descriptor = _role_assignment_descriptor(
        spec.assignment,
        provider=provider,
        model=model,
    )
    data = {
        "role": spec.role,
        "source": spec.source,
        "project_key": spec.project_key,
        "table": spec.table_label,
        "alias": spec.assignment.alias,
        "provider": provider,
        "model": model,
    }

    if provider not in known_providers:
        return _fail(
            f"{descriptor}, source={spec.source}, unknown provider",
            why=(
                f"{location_text} resolves to provider {provider!r}, but no "
                "installed PollyPM provider adapter advertises that name."
            ),
            fix=(
                f"Change {location_text} to a registered provider "
                f"({', '.join(sorted(known_providers))}) or install the missing "
                "provider plugin.\n"
                "Recheck: pm doctor"
            ),
            data=data | {"known_providers": sorted(known_providers)},
        )

    provider_accounts = accounts_by_provider.get(provider, ())
    if not provider_accounts:
        return _fail(
            f"{descriptor}, source={spec.source}, no {provider} account configured",
            why=(
                f"{location_text} resolves to {provider}/{model}, but PollyPM has "
                f"no configured {provider} account to launch or probe that model."
            ),
            fix=(
                f"Add a {provider} account via `pm onboard`, or point "
                f"{location_text} at a provider with configured accounts.\n"
                "Recheck: pm doctor"
            ),
            data=data | {"accounts": []},
        )

    smoke = smoke_cache.get((provider, model))
    if smoke is None:
        smoke = _probe_role_model_access(
            config.project,
            provider,
            model,
            provider_accounts,
        )
        smoke_cache[(provider, model)] = smoke

    probe_data = {
        "ok": smoke.ok,
        "account": smoke.account_name,
        "attempts": list(smoke.attempts),
        "accounts": [name for name, _account in provider_accounts],
    }
    if not smoke.ok:
        attempts = "; ".join(smoke.attempts[:3]) or "no configured account succeeded"
        return _fail(
            f"{descriptor}, source={spec.source}, smoke probe failed",
            why=(
                f"{location_text} resolves to {provider}/{model}, but doctor could "
                f"not confirm that any configured {provider} account can reach it."
            ),
            fix=(
                f"Re-login a {provider} account or choose a reachable model for "
                f"{location_text}.\n"
                f"Probe attempts: {attempts}\n"
                "Recheck: pm doctor"
            ),
            data=data | {"probe": probe_data},
        )

    advisories = advisories_for(spec.role, spec.assignment, registry=registry)
    if advisories:
        return _fail(
            f"{descriptor}, source={spec.source}, reachable via {smoke.account_name}, advisory",
            why="; ".join(advisories),
            fix=(
                f"Prefer a stronger model for role {spec.role}, or keep "
                f"{location_text} if the weaker fit is intentional.\n"
                "Recheck: pm doctor"
            ),
            severity="warning",
            data=data | {"probe": probe_data, "advisories": advisories},
        )

    return _ok(
        f"{descriptor}, source={spec.source}, reachable via {smoke.account_name}",
        data=data | {"probe": probe_data, "advisories": []},
    )


def _role_assignment_checks() -> list[Check]:
    path, config = _safe_load_config()
    if config is None:
        status = (
            "role-assignment check skipped (config parse error)"
            if path is not None else
            "role-assignment check skipped (no config)"
        )
        return [Check("role-assignments", lambda status=status: _skip(status), "roles")]

    specs = _role_assignment_specs(config)
    if not specs:
        return [
            Check(
                "role-assignments",
                lambda: _ok("no configured global/project role assignments"),
                "roles",
            )
        ]

    from pollypm.model_registry import load_registry

    registry = load_registry()
    known_providers = _known_role_providers()
    accounts_by_provider: dict[str, list[tuple[str, AccountConfig]]] = {}
    for account_name, account in sorted(config.accounts.items()):
        provider_name = str(getattr(account.provider, "value", account.provider))
        accounts_by_provider.setdefault(provider_name, []).append((account_name, account))
    frozen_accounts = {
        provider: tuple(entries)
        for provider, entries in accounts_by_provider.items()
    }
    smoke_cache: dict[tuple[str, str], _RoleModelSmokeResult] = {}

    checks: list[Check] = []
    for spec in specs:
        checks.append(
            Check(
                spec.check_name,
                lambda spec=spec: _role_assignment_check_result(
                    spec,
                    config_path=path,
                    config=config,
                    registry=registry,
                    known_providers=known_providers,
                    accounts_by_provider=frozen_accounts,
                    smoke_cache=smoke_cache,
                ),
                "roles",
            )
        )
    return checks


def _drift_value(info: object, field: str, default: object = None) -> object:
    if isinstance(info, dict):
        return info.get(field, default)
    return getattr(info, field, default)


@lru_cache(maxsize=1)
def _project_guides_module():
    try:
        import pollypm.project_guides as project_guides
    except Exception:  # noqa: BLE001
        return None
    return project_guides


def _project_guides_dir(project_path: Path) -> Path:
    return Path(project_path) / ".pollypm" / "project-guides"


def _project_guide_path(project_path: Path, role: str) -> Path:
    return _project_guides_dir(project_path) / f"{role}.md"


def _parse_guide_front_matter(text: str) -> tuple[dict[str, str], str]:
    normalized = text.replace("\r\n", "\n")
    if not normalized.startswith("---\n"):
        return {}, normalized
    marker = "\n---\n"
    end = normalized.find(marker, 4)
    if end == -1:
        return {}, normalized
    raw_header = normalized[4:end]
    body = normalized[end + len(marker):]
    header: dict[str, str] = {}
    for line in raw_header.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        header[key.strip()] = value.strip().strip('"').strip("'")
    return header, body


def _guide_source_path(role: str) -> Path | None:
    here = Path(__file__).resolve().parent
    if role == "architect":
        return here / "plugins_builtin" / "project_planning" / "profiles" / "architect.md"
    if role in {"worker", "reviewer"}:
        return here / "plugins_builtin" / "core_agent_profiles" / "profiles.py"
    return None


def _looks_like_hash(value: str, *, min_len: int = 7, max_len: int = 64) -> bool:
    return bool(re.fullmatch(rf"[0-9a-f]{{{min_len},{max_len}}}", value))


def _git_last_change_ref(path: Path | None) -> str | None:
    if path is None or not path.exists():
        return None
    rc, out = _run_cmd(
        ["git", "-C", str(path.parent), "rev-parse", "--show-toplevel"],
        timeout=1.0,
    )
    if rc != 0:
        return None
    repo_root = Path(out.strip())
    try:
        relative = path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return None
    rc, out = _run_cmd(
        ["git", "-C", str(repo_root), "log", "-n", "1", "--format=%H", "--", str(relative)],
        timeout=1.5,
    )
    if rc != 0:
        return None
    sha = (out.splitlines() or [""])[0].strip().lower()
    return sha if _looks_like_hash(sha, min_len=40, max_len=40) else None


@lru_cache(maxsize=None)
def _built_in_guide_body(role: str) -> str:
    if role == "architect":
        source = _guide_source_path(role)
        if source is None:
            raise ValueError(f"unknown guide role: {role}")
        return source.read_text(encoding="utf-8")
    if role == "worker":
        from pollypm.plugins_builtin.core_agent_profiles.profiles import worker_prompt

        return worker_prompt()
    if role == "reviewer":
        from pollypm.plugins_builtin.core_agent_profiles.profiles import reviewer_prompt

        return reviewer_prompt()
    raise ValueError(f"unknown guide role: {role}")


@lru_cache(maxsize=None)
def _built_in_guide_ref(role: str) -> str:
    body = _built_in_guide_body(role)
    git_ref = _git_last_change_ref(_guide_source_path(role))
    if git_ref:
        return git_ref
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _reference_aliases(value: str) -> set[str]:
    cleaned = (value or "").strip().lower()
    if not cleaned:
        return set()
    aliases = {cleaned}
    if cleaned.startswith("sha256:"):
        aliases.add(cleaned.split(":", 1)[1])
    elif _looks_like_hash(cleaned, min_len=64, max_len=64):
        aliases.add(f"sha256:{cleaned}")
    return aliases


def _forked_from_matches_current(forked_from: str, current_ref: str, *, upstream_body: str) -> bool:
    left = _reference_aliases(forked_from)
    right = _reference_aliases(current_ref)
    digest = hashlib.sha256(upstream_body.encode("utf-8")).hexdigest()
    right.add(digest)
    right.add(f"sha256:{digest}")
    if left & right:
        return True
    forked = (forked_from or "").strip().lower()
    current = (current_ref or "").strip().lower()
    if _looks_like_hash(forked) and _looks_like_hash(current):
        return forked.startswith(current) or current.startswith(forked)
    return False


def _fallback_project_guide_drift_info(project_path: Path, role: str) -> _LocalGuideDriftInfo | None:
    if role not in _PROJECT_GUIDE_ROLES:
        return None
    path = _project_guide_path(project_path, role)
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    header, body = _parse_guide_front_matter(text)
    upstream_body = _built_in_guide_body(role)
    current_ref = _built_in_guide_ref(role)
    forked_from = str(header.get("forked_from") or "").strip()
    drifted = bool(forked_from) and not _forked_from_matches_current(
        forked_from,
        current_ref,
        upstream_body=upstream_body,
    )
    return _LocalGuideDriftInfo(
        role=role,
        path=path,
        forked_from=forked_from,
        current_ref=current_ref,
        drifted=drifted,
        body=body,
        upstream_body=upstream_body,
    )


def _project_guide_drift_info(project_path: Path, role: str) -> object | None:
    project_guides = _project_guides_module()
    if project_guides is not None:
        helper = getattr(project_guides, "project_guide_drift_info", None)
        if callable(helper):
            try:
                return helper(project_path, role)
            except Exception:  # noqa: BLE001
                pass
    return _fallback_project_guide_drift_info(project_path, role)


def _list_drifted_project_guides(project_path: Path) -> list[object]:
    project_guides = _project_guides_module()
    if project_guides is not None:
        helper = getattr(project_guides, "list_drifted_project_guides", None)
        if callable(helper):
            try:
                return list(helper(project_path) or [])
            except Exception:  # noqa: BLE001
                pass
    drifted: list[object] = []
    for role in _PROJECT_GUIDE_ROLES:
        info = _fallback_project_guide_drift_info(project_path, role)
        if info is not None and info.drifted:
            drifted.append(info)
    return drifted


def _rewrite_planner_enforce_plan(config_path: Path) -> tuple[bool, str]:
    """Flip ``[planner].enforce_plan`` to true in ``config_path``.

    Preserves all other keys. Creates a ``<path>.bak`` sibling before
    writing. Idempotent — calling on a config that already has the key
    true is a no-op that returns success.
    """
    try:
        original = config_path.read_text()
    except OSError as exc:
        return (False, f"read failed: {exc}")
    # Backup first — safety net for manual rollback if the rewrite is
    # surprising. Overwrite any stale prior .bak from an earlier fix so
    # the backup always reflects the pre-fix content.
    try:
        (config_path.with_suffix(config_path.suffix + ".bak")).write_text(original)
    except OSError as exc:
        return (False, f"backup failed: {exc}")

    # Find an existing [planner] section; update or insert the key.
    lines = original.splitlines(keepends=True)
    planner_start: int | None = None
    planner_end: int | None = None
    for idx, raw in enumerate(lines):
        stripped = raw.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped.strip("[]").strip()
            if section == "planner":
                planner_start = idx
                planner_end = len(lines)
            elif planner_start is not None and planner_end == len(lines):
                planner_end = idx
    if planner_start is None:
        # Append a new section at the end. Match the blank-line style
        # of the rest of the file.
        suffix = "" if original.endswith("\n") else "\n"
        new_text = (
            original
            + suffix
            + "\n[planner]\nenforce_plan = true\n"
        )
    else:
        assert planner_end is not None
        new_section_lines: list[str] = []
        replaced = False
        for raw in lines[planner_start:planner_end]:
            stripped = raw.strip()
            if stripped.startswith("enforce_plan"):
                # Rewrite in place, preserving indentation.
                prefix = raw[: len(raw) - len(raw.lstrip())]
                new_section_lines.append(f"{prefix}enforce_plan = true\n")
                replaced = True
            else:
                new_section_lines.append(raw)
        if not replaced:
            # Insert right after the [planner] header.
            new_section_lines.insert(1, "enforce_plan = true\n")
        new_text = "".join(
            lines[:planner_start] + new_section_lines + lines[planner_end:]
        )
    try:
        config_path.write_text(new_text)
    except OSError as exc:
        return (False, f"write failed: {exc}")
    return (True, f"enabled plan-presence gate in {config_path} (backup: {config_path}.bak)")


def check_plan_presence_gate() -> CheckResult:
    """``[planner].enforce_plan`` should be true unless explicitly opted out.

    Warn (not error) if disabled — operators may legitimately bypass the
    gate for migrations or hotfixes — but call it out so a forgotten
    override doesn't leak into production.
    """
    path, config = _safe_load_config()
    if config is None:
        return _skip("plan-gate check skipped (no config)")
    planner = getattr(config, "planner", None)
    enforce = bool(getattr(planner, "enforce_plan", True)) if planner else True
    plan_dir = getattr(planner, "plan_dir", "docs/plan") if planner else "docs/plan"
    if not enforce:
        def _fix() -> tuple[bool, str]:
            if path is None:
                return (False, "no config path resolved")
            return _rewrite_planner_enforce_plan(path)

        return _fail(
            "[planner].enforce_plan = false (plan-presence gate disabled)",
            why=(
                "With the gate off, the task_assignment.sweep handler will "
                "delegate implementation tasks before any approved plan exists. "
                "That's the documented opt-out for hotfixes and migrations, "
                "but it should not be the steady state."
            ),
            fix=(
                "Re-enable the gate in ~/.pollypm/pollypm.toml —\n"
                "  [planner]\n"
                "  enforce_plan = true\n"
                "Or run:  pm doctor --fix   # writes the key with a .bak backup\n"
                "Recheck: pm doctor"
            ),
            severity="warning",
            fixable=True,
            fix_fn=_fix,
            data={"enforce_plan": False, "plan_dir": plan_dir},
        )
    return _ok(
        f"plan-presence gate enabled (plan_dir={plan_dir!r})",
        data={"enforce_plan": True, "plan_dir": plan_dir},
    )


def check_architect_profile() -> CheckResult:
    """The architect control-prompt profile must ship with the package."""
    here = Path(__file__).resolve().parent
    profile = here / "plugins_builtin" / "project_planning" / "profiles" / "architect.md"
    if not profile.is_file():
        return _fail(
            f"architect profile missing at {profile}",
            why=(
                "The architect bootstrap (issue #257) loads its control "
                "prompt from this file. Without it, planner spawn falls "
                "back to a generic prompt and the visual-explainer chain "
                "never wires up."
            ),
            fix=(
                "Reinstall PollyPM —\n"
                "  uv tool install --editable --reinstall .\n"
                "Recheck: pm doctor"
            ),
            data={"path": str(profile)},
        )
    return _ok(
        f"architect profile present ({profile.stat().st_size}B)",
        data={"path": str(profile), "bytes": profile.stat().st_size},
    )


def check_visual_explainer_skill() -> CheckResult:
    """The visual-explainer skill directory must exist under defaults/magic/."""
    here = Path(__file__).resolve().parent
    skill_dir = here / "defaults" / "magic" / "visual-explainer"
    skill_md = skill_dir / "SKILL.md"
    if not skill_dir.is_dir():
        return _fail(
            f"visual-explainer skill missing at {skill_dir}",
            why=(
                "The architect bootstrap drops this skill into agent "
                "worktrees so explainer artifacts (HTML reports linked "
                "from inbox items) can be authored. Missing skill dir "
                "breaks the plan-review explainer link."
            ),
            fix=(
                "Reinstall PollyPM —\n"
                "  uv tool install --editable --reinstall .\n"
                "Recheck: pm doctor"
            ),
            data={"path": str(skill_dir)},
        )
    if not skill_md.is_file():
        return _fail(
            f"visual-explainer SKILL.md missing at {skill_md}",
            why="The skill loader requires a SKILL.md descriptor.",
            fix=(
                "Reinstall PollyPM —\n"
                "  uv tool install --editable --reinstall .\n"
                "Recheck: pm doctor"
            ),
            severity="warning",
            data={"path": str(skill_md)},
        )
    return _ok(
        "visual-explainer skill present",
        data={"path": str(skill_dir)},
    )


def check_project_local_guide_drift() -> CheckResult:
    """Warn when a project-local role guide has drifted from upstream."""
    _path, config = _safe_load_config()
    if config is None:
        return _skip("project-guide drift check skipped (no config)")
    projects = getattr(config, "projects", {}) or {}
    if not projects:
        return _skip("project-guide drift check skipped (no projects)")

    stale: list[dict[str, object]] = []
    for project_key, project in projects.items():
        project_path = getattr(project, "path", None)
        if project_path is None:
            continue
        for info in _list_drifted_project_guides(Path(project_path)):
            role = str(_drift_value(info, "role") or "")
            stale.append(
                {
                    "project": project_key,
                    "project_name": getattr(project, "name", None) or project_key,
                    "role": role,
                    "path": str(_drift_value(info, "path") or _project_guide_path(Path(project_path), role)),
                    "forked_from": str(_drift_value(info, "forked_from") or ""),
                    "current_ref": str(_drift_value(info, "current_ref") or ""),
                }
            )

    if not stale:
        return _ok(
            "no stale project-local guides detected",
            data={"count": 0, "projects": 0, "stale": []},
        )

    projects_with_drift = len({item["project"] for item in stale})
    sample = ", ".join(
        f"{item['project']}:{item['role']}"
        for item in stale[:4]
    )
    fix_lines = ["Refresh each stale guide from the current built-in prompt:"]
    for item in stale[:4]:
        fix_lines.append(
            f"  pm project init-guide {item['role']} --project {item['project']} --force"
        )
    if len(stale) > 4:
        fix_lines.append(f"  ... and {len(stale) - 4} more")
    fix_lines.append("Review local edits before overwriting if needed.")
    fix_lines.append("Recheck: pm doctor")
    n_stale = len(stale)
    guide_word = "guide" if n_stale == 1 else "guides"
    project_word = "project" if projects_with_drift == 1 else "projects"
    return _fail(
        f"{n_stale} stale project-local {guide_word} across "
        f"{projects_with_drift} {project_word}: {sample}",
        why=(
            "Project-local role guides stop inheriting upstream prompt updates "
            "after they are forked. Stale guides can launch sessions with "
            "outdated instructions until they are refreshed."
        ),
        fix="\n".join(fix_lines),
        severity="warning",
        data={
            "count": len(stale),
            "projects": projects_with_drift,
            "stale": stale,
        },
    )


def _initialize_project_state_db(db_path: Path) -> tuple[bool, str]:
    """Create an initialized ``state.db`` at ``db_path`` via StateStore.

    Running StateStore's constructor creates the parent directory,
    applies the full schema, and runs every pending migration, so the
    resulting file is the same shape the sweeper expects. Safe to call
    on a path whose DB already exists — the open is a no-op for schema
    creation (CREATE TABLE IF NOT EXISTS) and a re-run of pending
    migrations.
    """
    try:
        from pollypm.storage.state import StateStore
    except Exception as exc:  # noqa: BLE001
        return (False, f"import failed: {exc}")
    try:
        store = StateStore(db_path)
        try:
            pass
        finally:
            store.close()
        return (True, f"created {db_path}")
    except Exception as exc:  # noqa: BLE001
        return (False, f"StateStore init failed: {exc}")


def check_task_assignment_sweeper_dbs() -> CheckResult:
    """Each tracked project must expose a state.db the sweeper can find."""
    _path, config = _safe_load_config()
    if config is None:
        return _skip("task-assignment sweeper check skipped (no config)")
    projects = getattr(config, "projects", {}) or {}
    if not projects:
        return _skip("task-assignment sweeper check skipped (no projects)")
    missing: list[str] = []
    missing_paths: list[Path] = []
    found = 0
    for key, project in projects.items():
        if not getattr(project, "tracked", False):
            continue
        db_path = project.path / ".pollypm" / "state.db"
        if db_path.is_file():
            found += 1
        else:
            missing.append(f"{key} ({db_path})")
            if project.path.exists():
                missing_paths.append(db_path)

    def _fix() -> tuple[bool, str]:
        if not missing_paths:
            return (False, "no writable project paths to initialize")
        initialized = 0
        errors: list[str] = []
        for db_path in missing_paths:
            ok, msg = _initialize_project_state_db(db_path)
            if ok:
                initialized += 1
            else:
                errors.append(f"{db_path}: {msg}")
        if errors:
            return (initialized > 0, f"initialized {initialized}; errors: {'; '.join(errors[:3])}")
        word = "file" if initialized == 1 else "files"
        return (True, f"initialized {initialized} project state.db {word}")

    if missing and not found:
        return _fail(
            f"no tracked project has a state.db on disk ({len(missing)} missing)",
            why=(
                "task_assignment.sweep iterates registered projects and opens "
                "each one's state.db to look for queued tasks. Zero DBs means "
                "the sweeper has nothing to do — usually because no project "
                "has been booted with `pm up` yet."
            ),
            fix=(
                "Boot at least one project to materialize its state.db —\n"
                "  cd <project> && pm up\n"
                "Or run:  pm doctor --fix   # initializes empty state.db for each\n"
                "Recheck: pm doctor"
            ),
            severity="warning",
            fixable=bool(missing_paths),
            fix_fn=_fix if missing_paths else None,
            data={"missing": missing},
        )
    if missing:
        return _fail(
            f"{len(missing)} tracked project(s) missing state.db: {', '.join(missing[:3])}",
            why=(
                "Projects without a state.db are silently skipped by the "
                "task_assignment.sweep handler. Tasks queued against them "
                "will never get delegated."
            ),
            fix=(
                "Boot each project once to create its state.db —\n"
                "  cd <project> && pm up\n"
                "Or run:  pm doctor --fix   # initializes empty state.db for each\n"
                "Or remove the [projects.*] entry from ~/.pollypm/pollypm.toml.\n"
                "Recheck: pm doctor"
            ),
            severity="warning",
            fixable=bool(missing_paths),
            fix_fn=_fix if missing_paths else None,
            data={"found": found, "missing": missing},
        )
    return _ok(
        f"{found} tracked project(s) have reachable state.db",
        data={"count": found},
    )


def _open_state_db_ro(db_path: Path) -> sqlite3.Connection | None:
    if not db_path.is_file():
        return None
    try:
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
    except sqlite3.Error:
        return None


def _primary_state_db() -> Path | None:
    """First tracked-project state.db, or None.

    Used by checks that need a single representative DB for table-level
    questions (sessions table population, last-fired-at events).
    """
    candidates = _state_db_candidates()
    return candidates[0] if candidates else None


def _parse_iso_or_epoch(value: Any) -> float:
    """Coerce a timestamp string / number / datetime into a POSIX epoch float.

    Accepts ISO-8601 strings ("2026-04-17T12:34:56+00:00"), integer /
    float epochs, and naive/aware ``datetime`` instances. Used by
    :func:`check_scheduler_last_fired` to compare handler rows
    uniformly regardless of whether they came from the unified
    ``messages`` table (datetime-valued ``created_at``) or the legacy
    ``events`` table (ISO string).

    Raises
    ------
    ValueError
        When ``value`` can't be parsed — callers treat this as "no
        event recorded" rather than propagating the failure.
    """
    from datetime import datetime as _dt, timezone as _tz
    if value is None:
        raise ValueError("timestamp is None")
    if isinstance(value, (int, float)):
        return float(value)
    if hasattr(value, "timestamp"):
        # datetime or anything datetime-shaped.
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_tz.utc)
        return dt.timestamp()
    text_value = str(value).strip()
    if not text_value:
        raise ValueError("timestamp is empty")
    try:
        parsed = _dt.fromisoformat(text_value)
    except ValueError:
        # Try fractional-second variants / 'Z' suffixes the stdlib used
        # to choke on before 3.11; normalize and retry.
        normalized = text_value.rstrip("Z") + "+00:00"
        parsed = _dt.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_tz.utc)
    return parsed.timestamp()


def check_sessions_table_populated() -> CheckResult:
    """A non-empty ``sessions`` table is the post-#268 expectation.

    On a healthy boot, ``Supervisor.start()`` calls
    ``repair_sessions_table()`` which back-fills a row for every live
    tmux window. A zero-count after boot means the repair path didn't
    fire and SessionRoleIndex resolution will degrade.
    """
    db_path = _primary_state_db()
    if db_path is None:
        return _skip("sessions-table check skipped (no state.db)")
    conn = _open_state_db_ro(db_path)
    if conn is None:
        return _skip(f"sessions-table check skipped (cannot open {db_path})")
    try:
        try:
            row = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
        except sqlite3.Error:
            return _skip("sessions-table check skipped (table missing)")
        count = int(row[0]) if row and row[0] is not None else 0
    finally:
        conn.close()

    def _fix() -> tuple[bool, str]:
        # Run repair_sessions_table via a transient supervisor.
        try:
            from pollypm.config import DEFAULT_CONFIG_PATH
        except Exception as exc:  # noqa: BLE001
            return (False, f"import failed: {exc}")
        if not DEFAULT_CONFIG_PATH.exists():
            return (False, "no config to load")
        try:
            sup = PollyPMService(DEFAULT_CONFIG_PATH).load_supervisor()
            repaired = sup.repair_sessions_table()
            return (True, f"repaired {repaired} {'session' if repaired == 1 else 'sessions'}")
        except Exception as exc:  # noqa: BLE001
            return (False, f"repair failed: {exc}")

    if count == 0:
        return _fail(
            "sessions table empty",
            why=(
                "Supervisor.start() should call repair_sessions_table() to "
                "back-fill a row for every live tmux window. An empty table "
                "means session-role resolution returns None and the cockpit "
                "can't route assignments to running sessions."
            ),
            fix=(
                "Boot PollyPM (the supervisor will repair) —\n"
                "  pm up\n"
                "Or run:  pm doctor --fix\n"
                "Recheck: pm doctor"
            ),
            severity="warning",
            fixable=True,
            fix_fn=_fix,
            data={"count": 0, "db": str(db_path)},
        )
    return _ok(
        f"sessions table has {count} {'row' if count == 1 else 'rows'}",
        data={"count": count, "db": str(db_path)},
    )


def _expected_handlers_from_plugins() -> set[str]:
    """Collect every handler name registered by the builtin recurring plugins.

    Imports the plugin modules and reads their ``capabilities`` tuples —
    no instantiation, no plugin-host needed. Returns the union of
    ``job_handler`` capability names from every builtin recurring plugin.
    """
    found: set[str] = set()
    candidates = (
        "pollypm.plugins_builtin.core_recurring.plugin",
        "pollypm.plugins_builtin.task_assignment_notify.plugin",
    )
    for mod_name in candidates:
        try:
            mod = __import__(mod_name, fromlist=["plugin"])
            plugin = getattr(mod, "plugin", None)
            if plugin is None:
                continue
            for cap in getattr(plugin, "capabilities", ()) or ():
                kind = getattr(cap, "kind", None)
                name = getattr(cap, "name", None)
                if kind == "job_handler" and isinstance(name, str):
                    found.add(name)
        except Exception:  # noqa: BLE001
            continue
    return found


def check_scheduler_roster_handlers() -> CheckResult:
    """Every handler we expect on the roster must be registered somewhere.

    We can't crack open a live roster without booting the host, but we
    can verify the *plugins* declare each handler — if any handler from
    :data:`_EXPECTED_SCHEDULED_HANDLERS` is absent from the union of
    plugin capabilities we know the roster cannot have registered it.
    """
    declared = _expected_handlers_from_plugins()
    if not declared:
        return _skip("scheduler-handlers check skipped (plugin import failed)")
    missing = [h for h in _EXPECTED_SCHEDULED_HANDLERS if h not in declared]
    if missing:
        return _fail(
            f"missing scheduled handler(s): {', '.join(missing)}",
            why=(
                "These recurring handlers ship with the builtin recurring "
                "plugins. Their absence means the roster never enqueues them "
                "— hygiene jobs (vacuum, log rotate, worktree prune) and "
                "task delegation will not run on schedule."
            ),
            fix=(
                "Reinstall PollyPM to restore the builtin plugins —\n"
                "  uv tool install --editable --reinstall .\n"
                "Recheck: pm doctor"
            ),
            data={"missing": missing, "expected": list(_EXPECTED_SCHEDULED_HANDLERS)},
        )
    return _ok(
        f"all {len(_EXPECTED_SCHEDULED_HANDLERS)} scheduled handler(s) registered",
        data={"handlers": list(_EXPECTED_SCHEDULED_HANDLERS)},
    )


# Per-handler maximum gap (seconds) before we flag the handler as
# "hasn't fired recently". Daily jobs allow ~2x cadence; hourly jobs
# allow ~3x; high-frequency sweeps allow generous slack so transient
# host shutdowns don't false-alarm.
_HANDLER_MAX_GAP_SECONDS: dict[str, int] = {
    "db.vacuum": 2 * 86400,
    "memory.ttl_sweep": 2 * 86400,
    "events.retention_sweep": 6 * 3600,
    "agent_worktree.prune": 6 * 3600,
    "log.rotate": 6 * 3600,
    "notification_staging.prune": 2 * 86400,
    "work.progress_sweep": 30 * 60,
    "task_assignment.sweep": 10 * 60,
}


def check_scheduler_last_fired() -> CheckResult:
    """Confirm scheduled handlers actually fired within their cadence.

    Pulls one reverse-chronological slice of recent ``messages``
    (``type in ('event', 'notify')``) and computes max(created_at) per
    handler in Python. Each handler with an event newer than its
    max-gap is healthy; one with no event ever is *informational*
    (fresh installs); one whose last event is older than the gap is a
    warning.
    """
    db_path = _primary_state_db()
    if db_path is None:
        return _skip("scheduler-cadence check skipped (no state.db)")

    try:
        from pollypm.store import SQLAlchemyStore
        store = SQLAlchemyStore(f"sqlite:///{db_path}")
    except Exception:  # noqa: BLE001
        return _skip("scheduler-cadence check skipped (cannot open store)")

    latest_per_handler: dict[str, float] = {}
    try:
        try:
            # ``limit=10000`` covers plenty of handler history (each
            # scheduled handler fires a few times per day; 10k rows is
            # weeks of events).
            rows = store.query_messages(
                type=["event", "notify"],
                limit=10000,
            )
        except Exception:  # noqa: BLE001
            rows = []
        now_ts = time.time()
        for row in rows:
            # Each handler's ``record_event`` call lands the handler
            # name in ``subject`` for legacy rows and in
            # ``payload['event_type']`` for new rows. Accept either.
            handler_key = row.get("subject") or ""
            payload = row.get("payload") or {}
            if isinstance(payload, dict):
                handler_key = payload.get("event_type") or handler_key
            if handler_key not in _HANDLER_MAX_GAP_SECONDS:
                continue
            ts_raw = row.get("created_at") or ""
            try:
                ts_val = _parse_iso_or_epoch(ts_raw)
            except Exception:  # noqa: BLE001
                continue
            existing = latest_per_handler.get(handler_key)
            if existing is None or ts_val > existing:
                latest_per_handler[handler_key] = ts_val
    finally:
        try:
            store.close()
        except Exception:  # noqa: BLE001
            pass

    overdue: list[tuple[str, float]] = []
    never_seen: list[str] = []
    healthy: list[str] = []
    for handler, max_gap in _HANDLER_MAX_GAP_SECONDS.items():
        last_ts = latest_per_handler.get(handler)
        if last_ts is None:
            never_seen.append(handler)
            continue
        gap = now_ts - last_ts
        if gap > max_gap:
            overdue.append((handler, gap))
        else:
            healthy.append(handler)
    data = {
        "overdue": [h for h, _ in overdue],
        "never_seen": never_seen,
        "healthy": healthy,
    }
    if overdue:
        summary = ", ".join(
            f"{h} ({gap / 3600:.1f}h ago)" for h, gap in overdue[:4]
        )
        return _fail(
            f"{len(overdue)} scheduled handler(s) overdue: {summary}",
            why=(
                "Each handler emits a system event when it runs. A gap "
                "exceeding the per-handler threshold (2x daily / 3x hourly "
                "cadence) means the scheduler isn't ticking — usually a "
                "stopped cockpit + missing cron rail."
            ),
            fix=(
                "Boot the cockpit (or schedule the rail cron) —\n"
                "  pm up\n"
                "Or run a one-off rail tick:  pm rail tick\n"
                "Recheck: pm doctor"
            ),
            severity="warning",
            data=data,
        )
    if not healthy:
        return _ok(
            "no scheduled-handler events yet (fresh install)",
            data=data,
        )
    return _ok(
        f"{len(healthy)} scheduled handler(s) within cadence",
        data=data,
    )


# Resource thresholds. Matched to the spec; tweaks here are cheap and
# don't require renaming existing checks.
_DB_WARN_BYTES = 500 * 1024 * 1024
_DB_ERROR_BYTES = 2 * 1024 * 1024 * 1024
_LOGS_WARN_BYTES = 500 * 1024 * 1024
_WORKTREE_WARN_COUNT = 50
_INBOX_WARN_COUNT = 50
_SESSION_RSS_WARN_BYTES = 1024 * 1024 * 1024


def check_state_db_size() -> CheckResult:
    """Warn at 500 MB, error at 2 GB. Recommends ``pm db vacuum``."""
    candidates = _state_db_candidates()
    if not candidates:
        return _skip("state.db size check skipped (no tracked DBs)")
    biggest = max(candidates, key=lambda p: p.stat().st_size if p.is_file() else 0)
    try:
        size = biggest.stat().st_size
    except OSError:
        return _skip(f"state.db size check skipped (stat failed for {biggest})")
    mb = size / (1024 * 1024)

    def _fix() -> tuple[bool, str]:
        try:
            from pollypm.storage.state import StateStore
        except Exception as exc:  # noqa: BLE001
            return (False, f"import failed: {exc}")
        try:
            store = StateStore(biggest)
            try:
                reclaimed = store.incremental_vacuum()
            finally:
                store.close()
            return (True, f"reclaimed {reclaimed / (1024 * 1024):.1f} MB")
        except Exception as exc:  # noqa: BLE001
            return (False, f"vacuum failed: {exc}")

    if size > _DB_ERROR_BYTES:
        return _fail(
            f"state.db is {mb:.0f} MB (threshold {_DB_ERROR_BYTES // (1024 ** 3)} GB)",
            why=(
                "An oversized state.db slows every read/write and risks "
                "freelist starvation. The daily db.vacuum handler reclaims "
                "freelist pages, but only if the cockpit is running."
            ),
            fix=(
                "Reclaim space —\n"
                "  pm db vacuum\n"
                "Or run:  pm doctor --fix\n"
                f"Path: {biggest}\n"
                "Recheck: pm doctor"
            ),
            fixable=True,
            fix_fn=_fix,
            data={"path": str(biggest), "bytes": size, "mb": round(mb, 1)},
        )
    if size > _DB_WARN_BYTES:
        return _fail(
            f"state.db is {mb:.0f} MB (warn at {_DB_WARN_BYTES // (1024 * 1024)} MB)",
            why=(
                "The DB is approaching the 2 GB error threshold. The daily "
                "db.vacuum handler is the canonical reclaim path — if it has "
                "not fired recently the freelist is growing unbounded."
            ),
            fix=(
                "Reclaim freelist pages —\n"
                "  pm db vacuum\n"
                f"Path: {biggest}\n"
                "Recheck: pm doctor"
            ),
            severity="warning",
            data={"path": str(biggest), "bytes": size, "mb": round(mb, 1)},
        )
    return _ok(
        f"state.db {mb:.1f} MB",
        data={"path": str(biggest), "bytes": size, "mb": round(mb, 1)},
    )


def _agent_worktree_dirs() -> list[Path]:
    """Best-effort enumeration of ``.claude/worktrees/agent-*`` dirs.

    Walks up from the current working directory and from this module's
    location to find a repo root, then lists the agent worktree dir.
    """
    candidates: list[Path] = []
    here = Path(__file__).resolve()
    seen: set[Path] = set()
    for start in (Path.cwd().resolve(), here):
        for parent in (start, *start.parents):
            if parent in seen:
                continue
            seen.add(parent)
            wt = parent / ".claude" / "worktrees"
            if wt.is_dir():
                candidates.extend(sorted(wt.glob("agent-*")))
                break
    return [p for p in candidates if p.is_dir()]


def _invoke_prune_handler() -> tuple[bool, str]:
    """Invoke the ``agent_worktree.prune`` handler directly.

    Bypasses the scheduler so the prune runs immediately rather than
    waiting for the next hourly tick. The handler is idempotent —
    calling it when nothing is prunable is a cheap no-op.
    """
    try:
        from pollypm.plugins_builtin.core_recurring.plugin import (
            agent_worktree_prune_handler,
        )
    except Exception as exc:  # noqa: BLE001
        return (False, f"import failed: {exc}")
    try:
        result = agent_worktree_prune_handler({})
    except Exception as exc:  # noqa: BLE001
        return (False, f"prune handler failed: {exc}")
    pruned = int(result.get("pruned", 0)) if isinstance(result, dict) else 0
    errors = int(result.get("errors", 0)) if isinstance(result, dict) else 0
    warned = int(result.get("warned_stale", 0)) if isinstance(result, dict) else 0
    pruned_word = "worktree" if pruned == 1 else "worktrees"
    error_word = "error" if errors == 1 else "errors"
    return (
        errors == 0,
        f"pruned {pruned} merged {pruned_word}, "
        f"{warned} stale unmerged retained, {errors} {error_word}",
    )


def check_agent_worktree_count() -> CheckResult:
    """Warn when the harness's agent worktree dir has accumulated >50 entries."""
    worktrees = _agent_worktree_dirs()
    count = len(worktrees)
    worktree_word = "worktree" if count == 1 else "worktrees"
    if count > _WORKTREE_WARN_COUNT:
        return _fail(
            f"{count} agent {worktree_word} under .claude/worktrees/ (warn at {_WORKTREE_WARN_COUNT})",
            why=(
                "The Claude Code harness sometimes leaves agent worktrees "
                "behind. The hourly agent_worktree.prune handler reaps "
                "merged branches, but only when the cockpit is running."
            ),
            fix=(
                "Trigger a one-off prune —\n"
                "  pm rail tick   # forces the next scheduled tick\n"
                "Or run:  pm doctor --fix   # runs the prune handler immediately\n"
                "Or manually:  git worktree prune && git worktree list\n"
                "Recheck: pm doctor"
            ),
            severity="warning",
            fixable=True,
            fix_fn=_invoke_prune_handler,
            data={"count": count},
        )
    return _ok(
        f"{count} agent {worktree_word} under .claude/worktrees/",
        data={"count": count},
    )


def _logs_dir_candidates() -> list[Path]:
    """Resolve the configured logs_dir for every tracked project + the global default."""
    out: list[Path] = []
    _path, config = _safe_load_config()
    if config is not None:
        try:
            out.append(config.project.logs_dir)
        except Exception:  # noqa: BLE001
            pass
    out.append(Path.home() / ".pollypm" / "logs")
    return [p for p in out if p.is_dir()]


def _dir_size_bytes(path: Path) -> int:
    total = 0
    try:
        for entry in path.rglob("*"):
            try:
                if entry.is_file():
                    total += entry.stat().st_size
            except OSError:
                continue
    except OSError:
        return total
    return total


def _invoke_log_rotate_handler(logs_dir: Path) -> tuple[bool, str]:
    """Invoke ``log.rotate`` against ``logs_dir`` directly.

    We pass ``logs_dir`` as a payload override so the handler skips the
    config-load path — we already know the directory. The handler
    rotates files past the threshold and prunes retention-exceeded
    siblings; calling it when nothing is over-threshold is a cheap
    no-op.
    """
    try:
        from pollypm.plugins_builtin.core_recurring.plugin import log_rotate_handler
    except Exception as exc:  # noqa: BLE001
        return (False, f"import failed: {exc}")
    try:
        result = log_rotate_handler({"logs_dir": str(logs_dir)})
    except Exception as exc:  # noqa: BLE001
        return (False, f"log.rotate handler failed: {exc}")
    rotated = int(result.get("rotated", 0)) if isinstance(result, dict) else 0
    deleted = int(result.get("deleted", 0)) if isinstance(result, dict) else 0
    errors = int(result.get("errors", 0)) if isinstance(result, dict) else 0
    rotated_word = "log" if rotated == 1 else "logs"
    deleted_word = "old archive" if deleted == 1 else "old archives"
    error_word = "error" if errors == 1 else "errors"
    return (
        errors == 0,
        f"rotated {rotated} {rotated_word}, "
        f"deleted {deleted} {deleted_word}, {errors} {error_word}",
    )


def check_logs_dir_size() -> CheckResult:
    """Warn when the logs dir exceeds 500 MB."""
    dirs = _logs_dir_candidates()
    if not dirs:
        return _skip("logs-dir size check skipped (no logs dir resolved)")
    biggest_dir = dirs[0]
    biggest_size = _dir_size_bytes(biggest_dir)
    for d in dirs[1:]:
        size = _dir_size_bytes(d)
        if size > biggest_size:
            biggest_dir = d
            biggest_size = size
    mb = biggest_size / (1024 * 1024)
    if biggest_size > _LOGS_WARN_BYTES:
        def _fix() -> tuple[bool, str]:
            return _invoke_log_rotate_handler(biggest_dir)

        return _fail(
            f"logs dir {biggest_dir} is {mb:.0f} MB (warn at {_LOGS_WARN_BYTES // (1024 * 1024)} MB)",
            why=(
                "Unbounded tmux pipe-pane captures can balloon individual "
                "logs to tens of megabytes. The hourly log.rotate handler "
                "rotates + gzips files past the threshold, but only when "
                "the cockpit is running."
            ),
            fix=(
                "Trigger a one-off rotation —\n"
                "  pm rail tick\n"
                "Or run:  pm doctor --fix   # runs log.rotate handler immediately\n"
                f"Path: {biggest_dir}\n"
                "Recheck: pm doctor"
            ),
            severity="warning",
            fixable=True,
            fix_fn=_fix,
            data={"path": str(biggest_dir), "bytes": biggest_size, "mb": round(mb, 1)},
        )
    return _ok(
        f"logs dir {mb:.1f} MB ({biggest_dir})",
        data={"path": str(biggest_dir), "bytes": biggest_size, "mb": round(mb, 1)},
    )


def _ps_claude_rss_kb() -> list[tuple[int, int, str]]:
    """Return ``(pid, rss_kb, command)`` for PollyPM-managed provider processes.

    We only count claude/codex processes that PollyPM launched — not
    the user's own dev-env Claude Code or an unrelated terminal
    ``codex`` session. The tell is the provider-home env var PollyPM
    always sets for managed launches (``CLAUDE_CONFIG_DIR`` /
    ``CODEX_HOME``) — ``ps -axeE`` reveals per-process environments
    on macOS so we filter to ones whose env pins back to PollyPM's
    ``agent_homes/`` or ``control-homes/`` directories.

    Without this filter, ``pm doctor`` session-memory flagged the
    user's interactive Claude Code session when they ran ``pm doctor``
    from inside it (confusing — "why is my IDE's Claude showing as a
    session?"). A pre-filter here keeps the check focused on
    sessions PollyPM actually owns.
    """
    rc, out = _run_cmd(["ps", "-axeE", "-o", "pid=,rss=,command="], timeout=3.0)
    if rc != 0:
        # Fall back to the plain form without env. On macOS this
        # should always succeed, but we keep a path so doctor never
        # breaks just because env dumping isn't available.
        rc, out = _run_cmd(["ps", "-axo", "pid=,rss=,command="], timeout=2.0)
        if rc != 0:
            return []
        env_scan = False
    else:
        env_scan = True
    rows: list[tuple[int, int, str]] = []
    for raw in out.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            rss_kb = int(parts[1])
        except ValueError:
            continue
        cmd_and_env = parts[2]
        lc = cmd_and_env.lower()
        if "claude" not in lc and "codex" not in lc:
            continue
        if "ps -axo" in cmd_and_env or "ps -axee" in cmd_and_env.lower():
            continue  # skip our own probe
        if env_scan:
            # Only count processes whose env pins them to a PollyPM-
            # managed account home. The env portion of ``-axeE`` output
            # trails the command, so a substring match is sufficient.
            is_pollypm = (
                "CLAUDE_CONFIG_DIR=/Users/" in cmd_and_env  # real user home
                and "/.pollypm/" in cmd_and_env
            ) or (
                "CODEX_HOME=/Users/" in cmd_and_env
                and "/.pollypm/" in cmd_and_env
            ) or (
                # Fallback heuristic — ``/agent_homes/`` or
                # ``/control-homes/`` substring anywhere in the env dump
                # indicates PollyPM ownership even for custom home dirs.
                "/agent_homes/" in cmd_and_env
                or "/control-homes/" in cmd_and_env
            )
            if not is_pollypm:
                continue
            # Keep only the command portion (before the first env var)
            # so downstream consumers see a tidy ``claude --flags`` form.
            cmd = cmd_and_env.split(" TERM=")[0].split(" COLORTERM=")[0]
        else:
            cmd = cmd_and_env
        rows.append((pid, rss_kb, cmd))
    return rows


def check_session_memory_usage() -> CheckResult:
    """Warn when any provider session's RSS exceeds 1 GB."""
    rows = _ps_claude_rss_kb()
    if not rows:
        return _skip("session-memory check skipped (no claude/codex processes)")
    over = [
        (pid, rss_kb, cmd)
        for pid, rss_kb, cmd in rows
        if rss_kb * 1024 > _SESSION_RSS_WARN_BYTES
    ]
    if over:
        biggest = max(over, key=lambda t: t[1])
        rss_mb = biggest[1] / 1024
        return _fail(
            f"{len(over)} session(s) over 1 GB RSS (largest pid {biggest[0]} = {rss_mb:.0f} MB)",
            why=(
                "A claude/codex process leaking past 1 GB usually means a "
                "long-running session has accumulated context the harness "
                "isn't reaping. Restarting the session reclaims the memory."
            ),
            fix=(
                "Restart the heavy session —\n"
                "  pm session restart <name>\n"
                "Or kill the process directly if it's hung:\n"
                f"  kill {biggest[0]}\n"
                "Recheck: pm doctor"
            ),
            severity="warning",
            data={
                "over": [{"pid": p, "rss_mb": round(r / 1024, 1)} for p, r, _ in over],
                "total_sessions": len(rows),
            },
        )
    biggest_rss_mb = max(r for _, r, _ in rows) / 1024 if rows else 0
    return _ok(
        f"{len(rows)} session(s), largest {biggest_rss_mb:.0f} MB RSS",
        data={"total_sessions": len(rows), "largest_rss_mb": round(biggest_rss_mb, 1)},
    )


def check_inbox_aggregator_path() -> CheckResult:
    """Echo the resolved ``pm notify`` default DB and verify it's the workspace-root one."""
    try:
        from pollypm.work.cli import _resolve_db_path
    except Exception as exc:  # noqa: BLE001
        return _skip(f"inbox-aggregator check skipped ({exc})")
    try:
        resolved = _resolve_db_path(".pollypm/state.db", project="inbox")
    except Exception as exc:  # noqa: BLE001
        return _fail(
            f"inbox aggregator path resolution raised {type(exc).__name__}: {exc}",
            why=(
                "_resolve_db_path is the canonical path resolver for both "
                "`pm notify` and `pm inbox`. If it raises, every inbox "
                "operation is broken before it touches the DB."
            ),
            fix=(
                "Inspect the workspace-root config —\n"
                "  cat ~/.pollypm/pollypm.toml\n"
                "Recheck: pm doctor"
            ),
        )
    _path, config = _safe_load_config()
    workspace_root = None
    if config is not None:
        workspace_root = getattr(config.project, "workspace_root", None)
    expected_under_workspace = (
        workspace_root is not None
        and Path(workspace_root) in resolved.parents
    )
    if workspace_root is not None and not expected_under_workspace:
        return _fail(
            f"pm notify path {resolved} is not under workspace root {workspace_root}",
            why=(
                "Per #271, `pm notify`/`pm inbox` must default to the "
                "workspace-root DB so notifications are visible regardless "
                "of which worktree the caller invoked from. A path elsewhere "
                "means notifications will land in a project-local DB the "
                "cockpit aggregator may not scan."
            ),
            fix=(
                "Set [project].workspace_root in ~/.pollypm/pollypm.toml "
                "and re-run.\n"
                f"Resolved: {resolved}\n"
                f"Workspace root: {workspace_root}\n"
                "Recheck: pm doctor"
            ),
            severity="warning",
            data={"resolved": str(resolved), "workspace_root": str(workspace_root)},
        )
    return _ok(
        f"pm notify default → {resolved}",
        data={"resolved": str(resolved), "workspace_root": str(workspace_root) if workspace_root else None},
    )


def check_inbox_open_count() -> CheckResult:
    """Suggest triage when open inbox items exceed 50."""
    _path, config = _safe_load_config()
    if config is None:
        return _skip("inbox-count check skipped (no config)")
    try:
        from pollypm.dashboard_data import _count_inbox_tasks
    except Exception as exc:  # noqa: BLE001
        return _skip(f"inbox-count check skipped ({exc})")
    try:
        count = _count_inbox_tasks(config)
    except Exception as exc:  # noqa: BLE001
        return _skip(f"inbox-count check skipped ({exc})")
    item_word = "item" if count == 1 else "items"
    if count > _INBOX_WARN_COUNT:
        return _fail(
            f"{count} open inbox {item_word} (warn at {_INBOX_WARN_COUNT})",
            why=(
                "A large backlog of inbox items signals neglected attention. "
                "Each item represents a request from Polly or another agent "
                "that's still waiting for the user."
            ),
            fix=(
                "Triage the inbox —\n"
                "  pm inbox\n"
                "Recheck: pm doctor"
            ),
            severity="warning",
            data={"count": count},
        )
    return _ok(f"{count} open inbox {item_word}", data={"count": count})


def check_sessions_table_vs_tmux() -> CheckResult:
    """Diff live tmux windows against the sessions table; flag drift."""
    if _tool_path("tmux") is None:
        return _skip("session-drift check skipped (tmux not installed)")
    rc, out = _run_cmd(
        ["tmux", "list-windows", "-a", "-F", "#{session_name}:#{window_name}"],
        timeout=2.0,
    )
    if rc != 0:
        # No tmux server running is a clean skip — this isn't an error.
        return _skip("session-drift check skipped (no tmux server)")
    tmux_windows: set[str] = set()
    for raw in out.splitlines():
        line = raw.strip()
        if not line or ":" not in line:
            continue
        # We only care about the window-name half — that's what
        # the supervisor stores in the sessions row.
        _session, window = line.split(":", 1)
        if window:
            tmux_windows.add(window)
    if not tmux_windows:
        return _skip("session-drift check skipped (no tmux windows)")
    db_path = _primary_state_db()
    if db_path is None:
        return _skip("session-drift check skipped (no state.db)")
    conn = _open_state_db_ro(db_path)
    if conn is None:
        return _skip("session-drift check skipped (db unreadable)")
    db_windows: set[str] = set()
    try:
        try:
            for row in conn.execute("SELECT window_name FROM sessions"):
                if row and row[0]:
                    db_windows.add(str(row[0]))
        except sqlite3.Error:
            return _skip("session-drift check skipped (sessions table missing)")
    finally:
        conn.close()

    # We only flag tmux windows that the supervisor *should* have
    # registered: those whose name matches a known PollyPM role prefix.
    known_prefixes = (
        "pm-", "polly", "operator", "reviewer", "worker-", "architect",
        "planner", "critic-",
    )
    pollypm_windows = {
        w for w in tmux_windows
        if any(w.startswith(prefix) for prefix in known_prefixes)
    }
    drift = sorted(pollypm_windows - db_windows)
    if drift:
        def _fix() -> tuple[bool, str]:
            try:
                from pollypm.config import DEFAULT_CONFIG_PATH
            except Exception as exc:  # noqa: BLE001
                return (False, f"import failed: {exc}")
            if not DEFAULT_CONFIG_PATH.exists():
                return (False, "no config to load")
            try:
                sup = PollyPMService(DEFAULT_CONFIG_PATH).load_supervisor()
                repaired = sup.repair_sessions_table()
                return (True, f"repaired {repaired} {'session' if repaired == 1 else 'sessions'}")
            except Exception as exc:  # noqa: BLE001
                return (False, f"repair failed: {exc}")

        return _fail(
            f"{len(drift)} tmux window(s) without a sessions row: {', '.join(drift[:5])}",
            why=(
                "Every PollyPM-managed tmux window should have a row in the "
                "sessions table so SessionRoleIndex can resolve it. Drift "
                "here means assignment routing will silently miss those "
                "windows."
            ),
            fix=(
                "Re-run the supervisor session repair —\n"
                "  pm doctor --fix   # invokes repair_sessions_table()\n"
                "Or restart the cockpit:  pm up\n"
                "Recheck: pm doctor"
            ),
            severity="warning",
            fixable=True,
            fix_fn=_fix,
            data={
                "drift": drift,
                "tmux_count": len(tmux_windows),
                "db_count": len(db_windows),
            },
        )
    return _ok(
        f"sessions table aligned with tmux ({len(db_windows)} row(s))",
        data={"tmux_count": len(tmux_windows), "db_count": len(db_windows)},
    )


def check_persona_swap_defense_wired() -> CheckResult:
    """Issue #266 — the supervisor must assert session launches match persona."""
    here = Path(__file__).resolve().parent
    supervisor_py = here / "supervisor.py"
    if not supervisor_py.is_file():
        return _fail(
            f"supervisor.py missing at {supervisor_py}",
            why="The supervisor module must ship with the package.",
            fix=(
                "Reinstall PollyPM —\n"
                "  uv tool install --editable --reinstall .\n"
                "Recheck: pm doctor"
            ),
        )
    try:
        text = supervisor_py.read_text()
    except OSError as exc:
        return _skip(f"persona-swap check skipped ({exc})")
    if "_assert_session_launch_matches" not in text:
        return _fail(
            "_assert_session_launch_matches not found in supervisor.py",
            why=(
                "Issue #266 wired an assertion that catches accidental "
                "persona swaps at session launch. Its absence means a "
                "regression silently re-opened the door."
            ),
            fix=(
                "Restore the assertion —\n"
                "  git log --diff-filter=A -- src/pollypm/supervisor.py | grep _assert\n"
                "  git checkout main -- src/pollypm/supervisor.py\n"
                "Recheck: pm doctor"
            ),
        )
    return _ok("persona-swap assertion wired in supervisor.py")


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
        Check("claude-cli", check_claude_cli, "system"),
        Check("codex-cli", check_codex_cli, "system"),
        Check("terminal-color", check_terminal_color_support, "system", severity="warning"),
        # Install state
        Check("pm-binary", check_pm_binary_resolves, "install"),
        Check("pollypm-version-matches", check_installed_version_matches_pyproject, "install", severity="warning"),
        Check("config-file", check_config_file, "install"),
        Check("provider-account", check_provider_account_configured, "install"),
        Check("storage-backend", check_storage_backend, "install"),
        Check("registered-providers", check_registered_providers, "install"),
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
        Check("db-layout-canonical", check_db_layout_canonical, "filesystem", severity="warning"),
        Check("disk-space", check_disk_space, "filesystem"),
        # Tmux session state
        Check("tmux-daemon", check_tmux_daemon, "tmux"),
        Check("storage-closet", check_storage_closet_reachable, "tmux"),
        Check("stale-dead-panes", check_no_stale_dead_panes, "tmux", severity="warning"),
        Check("rail-daemon-alive", check_rail_daemon_alive, "tmux", severity="warning"),
        # Network
        Check("network-github", check_network_github, "network", severity="warning"),
        Check("network-anthropic", check_network_anthropic, "network", severity="warning"),
        Check("network-openai", check_network_openai, "network", severity="warning"),
        # Roles
        *_role_assignment_checks(),
        # Pipeline (plan gate, architect bootstrap, sweepers)
        Check("plan-gate", check_plan_presence_gate, "pipeline", severity="warning"),
        Check("architect-profile", check_architect_profile, "pipeline"),
        Check("visual-explainer-skill", check_visual_explainer_skill, "pipeline"),
        Check("task-assignment-sweeper-dbs", check_task_assignment_sweeper_dbs, "pipeline", severity="warning"),
        Check("sessions-table-populated", check_sessions_table_populated, "pipeline", severity="warning"),
        # Guide drift
        Check("project-guide-drift", check_project_local_guide_drift, "guides", severity="warning"),
        # Schedulers
        Check("scheduler-handlers", check_scheduler_roster_handlers, "schedulers"),
        Check("scheduler-cadence", check_scheduler_last_fired, "schedulers", severity="warning"),
        # Resources
        Check("state-db-size", check_state_db_size, "resources"),
        Check("agent-worktree-count", check_agent_worktree_count, "resources", severity="warning"),
        Check("logs-dir-size", check_logs_dir_size, "resources", severity="warning"),
        Check("session-memory", check_session_memory_usage, "resources", severity="warning"),
        # Inbox
        Check("inbox-aggregator-path", check_inbox_aggregator_path, "inbox", severity="warning"),
        Check("inbox-open-count", check_inbox_open_count, "inbox", severity="warning"),
        # Sessions
        Check("session-drift", check_sessions_table_vs_tmux, "sessions", severity="warning"),
        Check("persona-swap-defense", check_persona_swap_defense_wired, "sessions"),
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

from pollypm.doctor import rendering as _doctor_rendering

render_human = _doctor_rendering.render_human
render_json = _doctor_rendering.render_json


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
        if result.fix_fn is not None and result.fixable:
            try:
                success, message = result.fix_fn()
            except Exception as exc:  # noqa: BLE001
                results.append((check.name, False, f"fix_fn raised {exc}"))
                continue
            results.append((check.name, success, message))
            continue
        if _auto_fix_supported(result.auto_fix):
            success, message = run_auto_fix(result.auto_fix)
            results.append((check.name, success, message))
    return results


def planned_fixes(report: DoctorReport) -> list[tuple[str, str]]:
    """List the fixes that ``apply_fixes`` *would* run, without running them.

    Returns ``(check_name, intention)`` tuples. ``intention`` is a short
    human-readable summary derived from the check's ``fix`` text — we
    use the first non-empty line so the output stays concise.
    """
    planned: list[tuple[str, str]] = []
    for check, result in report.results:
        if result.passed or result.skipped:
            continue
        if not ((result.fixable and result.fix_fn is not None) or _auto_fix_supported(result.auto_fix)):
            continue
        # First non-blank line of the fix block makes a readable
        # intention summary for dry-run output.
        intention = ""
        for line in (result.fix or "").splitlines():
            stripped = line.strip()
            if stripped and not stripped.lower().startswith("or"):
                intention = stripped
                break
        if not intention:
            intention = result.status or check.name
        planned.append((check.name, intention))
    return planned


def manual_fixes(report: DoctorReport) -> list[tuple[str, str]]:
    """List failures that ``--fix`` cannot auto-resolve.

    Returns ``(check_name, fix_hint)`` tuples where ``fix_hint`` is the
    check's ``fix`` text (manual instructions). Warnings and errors both
    count; skipped checks never do.
    """
    manual: list[tuple[str, str]] = []
    for check, result in report.results:
        if result.passed or result.skipped:
            continue
        if (result.fixable and result.fix_fn is not None) or _auto_fix_supported(result.auto_fix):
            continue
        manual.append((check.name, result.fix or result.status))
    return manual


def render_fix_summary(
    fix_results: list[tuple[str, bool, str]],
    manual: list[tuple[str, str]],
) -> str:
    """Render the post-``--fix`` summary footer.

    Format:
        Applied N fix(es): [name, name, ...]. K issue(s) remain (require manual intervention).

    When every fix succeeds and nothing manual is pending, the footer
    collapses to a single "Applied N fix(es)" line.
    """
    applied = [name for name, ok, _ in fix_results if ok]
    failed = [name for name, ok, _ in fix_results if not ok]
    parts: list[str] = []
    if applied:
        parts.append(f"Applied {len(applied)} fix(es): [{', '.join(applied)}]")
    else:
        parts.append("Applied 0 fixes")
    if failed:
        parts.append(f"{len(failed)} fix(es) failed: [{', '.join(failed)}]")
    if manual:
        names = [n for n, _ in manual]
        parts.append(
            f"{len(manual)} issue(s) remain (require manual intervention): [{', '.join(names)}]"
        )
    return ". ".join(parts) + "."


def render_fix_dry_run(planned: list[tuple[str, str]], manual: list[tuple[str, str]]) -> str:
    """Render ``--fix-dry-run`` output.

    Lists the fixes that would run, then the issues that can't be
    auto-fixed. Mirrors the shape of :func:`render_fix_summary` so
    scripts can diff the two outputs.
    """
    lines: list[str] = []
    lines.append(f"Would apply {len(planned)} fix(es):")
    for name, intention in planned:
        lines.append(f"  [would fix] {name}: {intention}")
    if manual:
        lines.append("")
        lines.append(
            f"{len(manual)} issue(s) require manual intervention (not auto-fixable):"
        )
        for name, hint in manual:
            first = (hint or "").splitlines()[0] if hint else ""
            lines.append(f"  [manual] {name}: {first.strip()}")
    return "\n".join(lines)
