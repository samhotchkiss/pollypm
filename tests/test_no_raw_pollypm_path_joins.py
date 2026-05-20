"""Lint gate: ratchet on raw ``/ ".pollypm" /`` joins in ``src/pollypm/`` (#1972).

Why this exists
---------------
The doubled-pollypm-path leak (#1810, #1950, #1966, #1972) keeps
returning because ``_resolve_pollypm_root()`` is opt-in: every new
feature that joins ``some_path / ".pollypm" / ...`` inline can
silently produce ``~/.pollypm/.pollypm/...`` when its caller happens
to be operating on the global config dir as a project root.

Per-PR audits (#1898, #1954) close known leak sites but don't
prevent the next one. The architectural fix (#1972) is to:

1. Funnel every ``.pollypm/<subdir>`` construction through a typed
   helper in :mod:`pollypm.projects` (``project_state_db_path``,
   ``project_audit_log_path``, etc.). Helpers chain through
   ``_resolve_pollypm_root`` so the doubled-path guard always fires.
2. Mechanically forbid NEW raw ``/ ".pollypm" /`` joins in
   ``src/pollypm/``. This test is the gate.

Ratchet design
--------------
Migrating all 155+ existing call sites in one PR is too big for v1
RC. So the gate ships as a **ratchet**: ``_BASELINE`` captures the
pre-fix count of raw joins per file. The test fails if:

* A file's count INCREASES (new leak site introduced), OR
* A file appears in the diff that isn't in the baseline (new file
  with a raw join), OR
* The baseline lists a file that no longer exists / has zero
  offenders (must shrink the baseline so future regressions land
  inside it).

The migration PRs (PR 2 + PR 3 under #1972) shrink the baseline
counts to zero. Once everything's zero, this test enforces "no
raw joins, ever."

Exclusion list
--------------
The gate is intentionally strict. The only allowed call sites are:

* ``src/pollypm/projects.py`` — the helper module itself defines
  ``_resolve_pollypm_root`` and the typed helpers, all of which
  construct the ``.pollypm`` segment internally. This is the one
  legitimate constructor.
* ``src/pollypm/config.py`` — defines ``GLOBAL_CONFIG_DIR =
  Path.home() / ".pollypm"``, the canonical source of truth that
  the resolver and helpers reference. Allowed at the constant
  definition only; other joins in this file should use the helpers.

Per-line escape hatch
---------------------
A line carrying ``# noqa: pollypm-path-join`` is permitted and is
NOT counted against the baseline. Use this sparingly — comments
accompanying these joins should explain why the helper API doesn't
fit (e.g. constructing a fixture path in the package install tree,
where no project root exists).
"""

from __future__ import annotations

import re
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_ROOT = _REPO_ROOT / "src" / "pollypm"

# The literal pattern we ban. Matches ``/ ".pollypm" /`` and
# ``/ ".pollypm"`` followed by a closing context (string end, paren,
# comma, etc.). We use a deliberately tight pattern so we catch the
# load-bearing case (``X / ".pollypm" / "..."``) and the trailing
# case (``X / ".pollypm"`` building the pollypm dir itself) without
# matching the string ``".pollypm"`` in comments / docstrings.
_BAN_PATTERN = re.compile(r'/\s*"\.pollypm"')

# Files that ARE allowed to construct the ``.pollypm`` segment — see
# module docstring for rationale.
_ALLOWED_FILES = {
    "projects.py",  # typed helpers + resolver
    "config.py",    # GLOBAL_CONFIG_DIR constant
}

# Per-line escape hatch.
_NOQA_PRAGMA = "noqa: pollypm-path-join"


# ---------------------------------------------------------------------------
# Baseline (#1972 PR 1 — design fix slice 1).
#
# Pre-migration count of raw joins per file in src/pollypm/. PRs 2
# and 3 under #1972 shrink these to zero. New files with raw joins
# (not in the baseline) and any increase against a baselined file
# both fail this test.
#
# When you migrate a file in #1972 PR 2 / PR 3:
#   - Reduce the entry's count by the number of joins you routed
#     through a typed helper.
#   - When the entry hits 0, remove it.
#
# When a legitimate raw join must remain (e.g. a constructor for a
# path inside the package install tree, where no project root
# exists), tag the line with ``# noqa: pollypm-path-join`` instead
# of leaving it in the baseline.
# ---------------------------------------------------------------------------
_BASELINE: dict[str, int] = {
    "src/pollypm/__main__.py": 1,
    "src/pollypm/accounts.py": 1,
    "src/pollypm/agent_profiles/defaults.py": 4,
    "src/pollypm/audit/log.py": 2,
    "src/pollypm/cli_features/projects.py": 2,
    "src/pollypm/cockpit.py": 1,
    "src/pollypm/cockpit_inbox.py": 2,
    "src/pollypm/cockpit_inbox_sources.py": 2,
    "src/pollypm/cockpit_project_settings.py": 4,
    "src/pollypm/cockpit_sections/insights.py": 1,
    "src/pollypm/cockpit_sections/project_dashboard.py": 2,
    "src/pollypm/cockpit_settings_gather.py": 1,
    "src/pollypm/cockpit_settings_history.py": 1,
    "src/pollypm/cockpit_settings_projects.py": 1,
    "src/pollypm/cockpit_tasks.py": 3,
    "src/pollypm/cockpit_ui.py": 13,
    "src/pollypm/config_patches.py": 10,
    "src/pollypm/control_tui.py": 1,
    "src/pollypm/dashboard_data.py": 2,
    "src/pollypm/doctor.py": 10,
    "src/pollypm/doctor/filesystem.py": 2,
    "src/pollypm/heartbeats/local.py": 2,
    "src/pollypm/heartbeats/stall_classifier.py": 1,
    "src/pollypm/itsalive.py": 1,
    "src/pollypm/llm_runner.py": 1,
    "src/pollypm/memory_cli.py": 1,
    "src/pollypm/model_registry.py": 1,
    "src/pollypm/onboarding.py": 2,
    "src/pollypm/plugin_cli.py": 2,
    "src/pollypm/plugin_host.py": 6,
    "src/pollypm/plugins_builtin/activity_feed/projector_factory.py": 1,
    "src/pollypm/plugins_builtin/core_rail_items/plugin.py": 2,
    "src/pollypm/plugins_builtin/core_recurring/audit_watchdog.py": 2,
    "src/pollypm/plugins_builtin/core_recurring/sweeps.py": 1,
    "src/pollypm/plugins_builtin/default_launch_planner/planner.py": 1,
    "src/pollypm/plugins_builtin/morning_briefing/handlers/gather_yesterday.py": 3,
    "src/pollypm/plugins_builtin/morning_briefing/handlers/identify_priorities.py": 1,
    "src/pollypm/plugins_builtin/project_planning/cli/project.py": 4,
    "src/pollypm/plugins_builtin/project_planning/memory.py": 1,
    "src/pollypm/plugins_builtin/project_planning/plugin.py": 2,
    "src/pollypm/plugins_builtin/task_assignment_notify/handlers/sweep.py": 1,
    "src/pollypm/pm_turn_state.py": 1,
    "src/pollypm/project_paths.py": 2,
    "src/pollypm/project_planning_protocol.py": 1,
    "src/pollypm/providers/claude/adapter.py": 2,
    "src/pollypm/providers/codex/adapter.py": 2,
    "src/pollypm/rail_cli.py": 2,
    "src/pollypm/rail_daemon_launchd.py": 1,
    "src/pollypm/recovery_prompt.py": 1,
    "src/pollypm/release_check.py": 1,
    "src/pollypm/rules.py": 4,
    "src/pollypm/runtime_services.py": 1,
    "src/pollypm/session_services/tmux.py": 1,
    "src/pollypm/storage/legacy_per_project_db.py": 1,
    "src/pollypm/storage/pg_migration_tool.py": 2,
    "src/pollypm/store/migrations.py": 1,
    "src/pollypm/supervisor.py": 4,
    "src/pollypm/task_backends/__init__.py": 1,
    "src/pollypm/upgrade.py": 1,
    "src/pollypm/web_api/token.py": 1,
    "src/pollypm/work/db_resolver.py": 2,
    "src/pollypm/work/first_shipped.py": 2,
    "src/pollypm/work/flow_engine.py": 4,
    "src/pollypm/work/gates.py": 4,
    "src/pollypm/work/inbox_actions.py": 1,
    "src/pollypm/work/service_factory.py": 1,
    "src/pollypm/work/session_manager.py": 8,
    "src/pollypm/work/worker_marker_reaper.py": 1,
}


def _iter_src_files() -> list[Path]:
    return sorted(_SRC_ROOT.rglob("*.py"))


def _is_excluded(path: Path) -> bool:
    """Return True for files allowed to construct ``.pollypm`` joins.

    Exclusion is by filename, not full path, because the helper file
    is ``pollypm/projects.py`` regardless of where the package lives
    on disk. ``config.py`` likewise.
    """
    if path.name not in _ALLOWED_FILES:
        return False
    # Sanity check the file lives directly under ``src/pollypm/``,
    # not a third-party plugin shadowing the name.
    return path.parent == _SRC_ROOT


def _find_offending_lines(path: Path) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return out
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not _BAN_PATTERN.search(line):
            continue
        # Skip if the line carries the noqa escape hatch.
        if _NOQA_PRAGMA in line:
            continue
        # Skip comment-only lines (docstrings are harder to detect
        # without parsing; rely on the noqa pragma for those).
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        out.append((lineno, line.rstrip()))
    return out


def _current_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in _iter_src_files():
        if _is_excluded(path):
            continue
        n = len(_find_offending_lines(path))
        if n:
            rel = str(path.relative_to(_REPO_ROOT))
            counts[rel] = n
    return counts


def test_lint_gate_ratchet() -> None:
    """Fail on new raw joins or increases over the baseline.

    Three failure modes:

    1. A file's offender count INCREASED — new leak site introduced.
    2. A file appeared with offenders that isn't in the baseline — new
       leak site in a new file.
    3. A baseline entry no longer exists / has zero offenders — the
       baseline must shrink as we migrate. Stale entries hide future
       regressions inside the cleaned file.

    Migration PRs (#1972 PRs 2 + 3) shrink the baseline. Once
    everything is zero we have an absolute "no raw joins" gate.
    """
    current = _current_counts()
    failures: list[str] = []

    # Check every baseline entry vs current.
    for rel, baseline in sorted(_BASELINE.items()):
        actual = current.get(rel, 0)
        if actual > baseline:
            failures.append(
                f"{rel}: offender count went UP "
                f"({baseline} → {actual}). Migrate the new join "
                "through a typed helper in pollypm.projects, or "
                "add `# noqa: pollypm-path-join` with a comment."
            )
        elif actual < baseline:
            failures.append(
                f"{rel}: offender count went DOWN "
                f"({baseline} → {actual}). Update _BASELINE in "
                "tests/test_no_raw_pollypm_path_joins.py to match "
                "(remove the entry if it hit zero) so the ratchet "
                "stays tight."
            )

    # Check for new files with offenders not in the baseline.
    for rel, actual in sorted(current.items()):
        if rel not in _BASELINE:
            failures.append(
                f"{rel}: new file with {actual} raw ``.pollypm`` "
                "join(s). Route through a typed helper in "
                "pollypm.projects, or add `# noqa: pollypm-path-join`."
            )

    if failures:
        msg = "\n".join(["#1972 lint-gate ratchet failed:", "", *failures])
        raise AssertionError(msg)


def test_helper_module_only_internal_joins() -> None:
    """The helper module is the ONLY constructor of ``.pollypm``.

    Sanity-check the exclusion list: the helper file must still
    contain the joins (otherwise the helpers can't construct
    paths), and ``config.py`` must still define ``GLOBAL_CONFIG_DIR``.
    If a future refactor moves the resolver elsewhere, this test
    catches the drift so the exclusion list gets updated.
    """
    helpers = _SRC_ROOT / "projects.py"
    config = _SRC_ROOT / "config.py"

    helpers_text = helpers.read_text(encoding="utf-8")
    assert "_resolve_pollypm_root" in helpers_text, (
        f"{helpers}: expected to host _resolve_pollypm_root (helper module). "
        "If this moved, update _ALLOWED_FILES in this test."
    )
    assert 'GLOBAL_CONFIG_DIR = Path.home() / ".pollypm"' in (
        config.read_text(encoding="utf-8")
    ), (
        f"{config}: expected to define GLOBAL_CONFIG_DIR. "
        "If this moved, update _ALLOWED_FILES in this test."
    )


def test_noqa_pragma_recognised(tmp_path: Path) -> None:
    """A line tagged with the noqa pragma is permitted.

    Belt-and-suspenders for the pragma path: if we ever break the
    escape hatch, the gate becomes unusable. This test pins it.
    """
    fake = tmp_path / "fake.py"
    fake.write_text(
        'x = some_path / ".pollypm" / "foo"  # noqa: pollypm-path-join — test\n'
    )
    offenders = _find_offending_lines(fake)
    assert offenders == [], (
        f"Expected noqa pragma to suppress the gate but got: {offenders}"
    )


def test_inline_join_without_pragma_caught(tmp_path: Path) -> None:
    """A bare inline join is caught by the gate.

    Pin the positive case: if the regex stops matching, the gate
    silently lets new leak sites through.
    """
    fake = tmp_path / "fake.py"
    fake.write_text('x = some_path / ".pollypm" / "foo"\n')
    offenders = _find_offending_lines(fake)
    assert len(offenders) == 1, (
        f"Expected gate to catch raw join but got: {offenders}"
    )
    assert offenders[0][0] == 1
