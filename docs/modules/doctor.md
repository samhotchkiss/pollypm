**Last Verified:** 2026-04-22

## Summary

`pm doctor` is the environment validator for brand-new PollyPM users — and a sanity check for existing ones. Every check answers three questions on failure: *what is wrong* (name + status line), *why PollyPM needs it* (the `why` field), and *how to fix it* (a multi-line `fix` field). Checks complete in under 5 seconds on a healthy system and are safe before any PollyPM state exists. A crashing check cannot poison the rest of the run — the `run` callable returns a `CheckResult` instead of raising.

`doctor.py` is a module-level facade that also hosts a `__path__` extension so internal submodules under `doctor/` can be imported as `pollypm.doctor.<name>` while keeping the public import path stable.

Touch this module when adding a check to the startup-readiness set. Do not import `Supervisor`, the work service's `SQLiteWorkService`, `plugin_api/v1`, or `memory_backends/*` — those are forbidden so doctor stays runnable even when those subsystems crash.

## Core Contracts

```python
# src/pollypm/doctor.py
@dataclass(slots=True)
class Check:
    name: str
    run: Callable[[], CheckResult]
    why: str
    fix: str

@dataclass(slots=True)
class CheckResult:
    status: str       # "pass" | "warn" | "fail"
    message: str
    details: dict | None = None

def _registered_checks() -> list[Check]: ...

def run_checks(checks: Iterable[Check]) -> list[tuple[Check, CheckResult]]: ...
```

## File Structure

- `src/pollypm/doctor.py` — registration, runner, module facade.
- `src/pollypm/doctor/system.py` — Python version, tmux, git, SQLite.
- `src/pollypm/doctor/filesystem.py` — `~/.pollypm/`, project state dirs.
- `src/pollypm/doctor/install_state.py` — PollyPM install version + release-check banner.
- `src/pollypm/doctor/plugins.py` — plugin discovery, manifest validation.
- `src/pollypm/doctor/rendering.py` — human-friendly CLI output.
- `tests/test_doctor.py` — pins the check set + runner.

## Implementation Details

- **Hard constraints.** `doctor.py:25`–27 lists imports that are explicitly *not allowed*: `supervisor.py`, `work/session_manager.py`, `work/sqlite_service.py`, `plugin_api/v1.py`, `memory_backends/*`. First-run users whose `~/.pollypm/` has never been touched must still get actionable output.
- **Ordering.** `_registered_checks()` defines a top-down order so users see fixable environment issues before subsystem issues.
- **Release-channel banner.** If `release_check.update_banner_line()` returns a non-empty string, the doctor header prints it. A missing network is silent — the banner is a nice-to-have.
- **JSON mode.** `pm doctor --json` emits a machine-readable result list for scripts.

## Related Docs

- [modules/release-check.md](release-check.md) — update banner source.
- [modules/config.md](config.md) — checked for well-formedness.
- [features/cli.md](../features/cli.md) — `pm doctor` entry point.
