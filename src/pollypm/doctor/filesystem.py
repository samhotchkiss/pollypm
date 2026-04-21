"""Filesystem checks extracted from :mod:`pollypm.doctor`."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pollypm.doctor as doctor


def check_pollypm_home_writable() -> doctor.CheckResult:
    home = doctor._pollypm_home()

    def _fix() -> tuple[bool, str]:
        try:
            home.mkdir(parents=True, exist_ok=True)
            probe = home / ".doctor_probe"
            probe.write_text("ok")
            probe.unlink(missing_ok=True)
            return (True, f"created {home}")
        except Exception as exc:  # noqa: BLE001
            return (False, f"mkdir failed: {exc}")

    if not home.exists():
        return doctor._fail(
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
        return doctor._fail(
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
    return doctor._ok(f"{home} writable", data={"path": str(home)})


def check_pollypm_plugins_dir() -> doctor.CheckResult:
    plugins_dir = doctor._pollypm_home() / "plugins"

    def _fix() -> tuple[bool, str]:
        try:
            plugins_dir.mkdir(parents=True, exist_ok=True)
            return (True, f"created {plugins_dir}")
        except Exception as exc:  # noqa: BLE001
            return (False, f"mkdir failed: {exc}")

    if plugins_dir.is_dir():
        return doctor._ok(f"{plugins_dir} exists", data={"path": str(plugins_dir)})
    return doctor._fail(
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


def check_tracked_project_state_parents() -> doctor.CheckResult:
    from pollypm.config import DEFAULT_CONFIG_PATH, load_config

    if not DEFAULT_CONFIG_PATH.exists():
        return doctor._skip("tracked-project check skipped (no config)")
    try:
        config = load_config(DEFAULT_CONFIG_PATH)
    except Exception:  # noqa: BLE001
        return doctor._skip("tracked-project check skipped (config parse error)")
    missing: list[Path] = []
    for project in (getattr(config, "projects", {}) or {}).values():
        if not getattr(project, "tracked", False):
            continue
        parent = project.path
        if not parent.exists():
            missing.append(parent)
    if missing:
        return doctor._fail(
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
    return doctor._ok("tracked project paths exist")


_LEGACY_STATE_DIRNAME = ".pollypm" + "-state"


def check_db_layout_canonical() -> doctor.CheckResult:
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
        return doctor._fail(
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
    return doctor._ok("DB layout canonical (two scopes)", data=data)


def check_disk_space() -> doctor.CheckResult:
    """At least 1 GB free in $HOME."""
    try:
        usage = shutil.disk_usage(Path.home())
    except Exception as exc:  # noqa: BLE001
        return doctor._skip(f"disk space check skipped ({exc})")
    gb_free = usage.free / (1024 ** 3)
    if gb_free < 1.0:
        return doctor._fail(
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
    return doctor._ok(f"{gb_free:.1f} GB free on $HOME", data={"free_gb": round(gb_free, 2)})
