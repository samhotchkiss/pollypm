**Last Verified:** 2026-04-22

## Summary

`pm upgrade` installs a newer PollyPM release. It detects the installed installer (`uv`, `pip`, `brew`, `npm`), plans the install command, optionally runs a pre-migration check, executes the upgrade, and records a notice for the next `pm` invocation showing what changed. The command supports stable and beta channels.

Touch this module when changing installer detection, migration-check wiring, or the upgrade UX. Do not touch version detection — that's `release_check`.

## Core Contracts

```python
# src/pollypm/upgrade.py
@dataclass(slots=True)
class UpgradePlan: ...

@dataclass(slots=True)
class UpgradeResult: ...

def detect_installer(overrides: dict[str, bool] | None = None) -> "Installer": ...
def plan_upgrade(installer: "Installer", channel: str) -> UpgradePlan: ...

def upgrade(
    *,
    channel: str = "stable",
    check_only: bool = False,
    recycle_all: bool = False,
    recycle_idle: bool = False,
    emit: Callable[[str], None] | None = None,
    installer_overrides: dict[str, bool] | None = None,
    plan_only: bool = False,
) -> UpgradeResult: ...

def run_migration_check() -> tuple[bool, str]: ...
def inject_notice(old_version: str, new_version: str) -> tuple[bool, str]: ...
def read_changelog_diff(since: str, *, path: Path | None = None) -> str: ...
def unsupported_installer_help() -> str: ...
```

`pm upgrade` flags (via `cli_features/upgrade.py`):

```
pm upgrade                  # detect installer, plan, install
pm upgrade --check-only     # print plan without installing
pm upgrade --channel beta   # switch channels for this invocation
```

## File Structure

- `src/pollypm/upgrade.py` — the installer.
- `src/pollypm/upgrade_notice.py` — cockpit notice helpers.
- `src/pollypm/release_check.py` — the source of truth for "is an upgrade available."
- `src/pollypm/cli_features/upgrade.py` — `pm upgrade` Typer command.

## Implementation Details

- **Installer detection.** `detect_installer` probes `uv`, `pip`, `brew`, and `npm` for the installed pollypm. First hit wins. `installer_overrides={"uv": True, ...}` is the test seam.
- **Unsupported installer.** If no installer matches, `unsupported_installer_help()` returns a multi-line message the CLI prints. The upgrade aborts.
- **Migration check.** `run_migration_check()` returns `(ok, detail)`. A failing check surfaces as an error in the CLI and blocks the install — this is where schema-migration safety lives.
- **Post-install notice.** `inject_notice(old_version, new_version)` writes a notice file so the next `pm` invocation (cockpit or CLI) shows a "you're now on vN, here's the changelog diff" banner.
- **Recycle flags.** `recycle_all` and `recycle_idle` are wired in the CLI for future behavior (#720). They are recorded on `UpgradeResult` but do not currently recycle sessions.

## Related Docs

- [modules/release-check.md](../modules/release-check.md) — upgrade-available detection.
- [modules/rail-daemon.md](../modules/rail-daemon.md) — what gets restarted.
- [features/cli.md](cli.md) — CLI wiring.
