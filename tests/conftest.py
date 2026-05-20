"""Project-wide pytest config.

Test-hygiene defaults that should apply to every test in this repo.
Module-specific fixtures live beside their tests.
"""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

import pytest


# Subdirectories of ``~/.pollypm/`` that production code legitimately
# writes to during normal operation but tests MUST NEVER touch outside
# their own ``tmp_path``. The write-guard fixture below snapshots these
# before each test and fails the test if any new entry appears.
#
# Background (issue #1902): a runaway pytest in a stalled Codex
# worktree was caught writing to ``~/.pollypm/artifacts/checkpoints/``
# AND spawning an orphaned ``claude --append-system-prompt-file ...``
# subprocess that survived the parent's exit by 87 minutes. The
# orphan's transcript leaked pytest-fixture paths into the operator's
# checkpoints, which the cockpit then rendered to the user during
# v1-RC user-flow testing. This guard is the defensive backstop:
# tests that *would* have polluted production state now fail loudly
# instead of silently corrupting the dev machine.
_POLLYPM_HOME_GUARDED_DIRS = (
    "artifacts",
    "checkpoints",
    "agent_homes",
    "homes",
    "transcripts",
    "snapshots",
    "worktrees",
    "dossier",
)

# Subprocess command names whose orphans the reaper fixture should
# clean up. These are the binaries PollyPM spawns into tmux panes;
# any process by these names that appears DURING a test but persists
# AFTER the test is an orphan the test failed to tear down.
_POLLYPM_REAP_NAMES = ("claude", "codex")


def pytest_configure(config):
    """Opt every test out of side-effectful daemon spawns.

    ``pm up`` normally spawns a detached ``pollypm.rail_daemon``
    process so auto-recovery runs without the cockpit. Tests that
    invoke the ``pm up`` codepath (``tests/integration/test_config_split_integration.py``
    among others) would each leak a detached daemon pointing at
    their pytest-tmp config path. Setting the env var here blocks
    the spawn across the whole test run; real integration tests that
    want to exercise the daemon can clear the var in their own fixture.
    """
    os.environ.setdefault("POLLYPM_SKIP_RAIL_DAEMON", "1")
    os.environ.setdefault("POLLYPM_DISABLE_ERROR_NOTIFICATIONS", "1")
    os.environ.setdefault("POLLYPM_DISABLE_AGENTIC_REVIEW_SUMMARIES", "1")
    os.environ.setdefault("POLLYPM_DISABLE_WORK_DB_OPENED_AUDIT", "1")
    os.environ.setdefault(
        "POLLYPM_ERROR_LOG_PATH",
        str(
            Path(tempfile.gettempdir())
            / f"pollypm-pytest-{os.getpid()}"
            / "errors.log"
        ),
    )
    # ``pollypm.audit.log`` writes JSONL tails under ``~/.pollypm/audit/``
    # by default. Without this redirect every test that triggers a task
    # lifecycle event (worker register, marker reap, work-service hooks)
    # leaks audit rows into the dev machine's real audit dir. Mirror the
    # error-log pattern above and point at a pytest-tmp dir so the user's
    # real audit history stays clean. Tests that exercise the audit-log
    # itself (see ``tests/test_audit_log.py``) override via monkeypatch.
    os.environ.setdefault(
        "POLLYPM_AUDIT_HOME",
        str(
            Path(tempfile.gettempdir())
            / f"pollypm-pytest-{os.getpid()}"
            / "audit"
        ),
    )
    # #1902 — ``pollypm.pm_turn_state`` writes ``pm_turn_state.json``
    # under ``~/.pollypm/`` by default. The env override exists; honour
    # it project-wide so heartbeat/turn-tracking tests don't pollute
    # the dev machine's turn-state file.
    os.environ.setdefault(
        "POLLYPM_PM_TURN_STATE_HOME",
        str(
            Path(tempfile.gettempdir())
            / f"pollypm-pytest-{os.getpid()}"
            / "pm_turn_state"
        ),
    )
    # Tests build their config in pytest tmp dirs but ``state_db``
    # defaults to ``~/.pollypm/state.db`` on the dev machine — which
    # may legitimately have pending migrations. Skip the refuse-start
    # gate globally so CLI plumbing tests don't pick up the real DB's
    # migration state. Tests that exercise the gate itself clear the
    # env var in their own monkeypatch fixture (see
    # ``tests/test_migration_gate.py``).
    os.environ.setdefault("POLLYPM_SKIP_MIGRATION_GATE", "1")
    # #1902 — register the opt-out marker so tests can declare they
    # intentionally write to ``~/.pollypm/`` (the guard then skips
    # the snapshot/diff for that test).
    config.addinivalue_line(
        "markers",
        "allows_home_writes: opt-out of the ~/.pollypm/ write-guard "
        "(use only when a test explicitly monkeypatches Path.home / "
        "GLOBAL_CONFIG_DIR to a sandbox).",
    )


@pytest.fixture(autouse=True)
def _reset_store_cache_between_tests():
    """Drain the process-wide store cache before + after every test.

    ``pollypm.store.registry.get_store`` caches backend instances by
    ``(backend, db_path)`` so every caller in a process shares the
    same engine pool (prevents the FD exhaustion that bit us on
    2026-04-20). Tests build config against ``tmp_path``, so without
    this fixture an earlier test's cached engine would point at a
    now-deleted path and the next test would reuse it. Drain before
    + after so state from one test never leaks into another.
    """
    try:
        from pollypm.store.registry import reset_store_cache
    except ImportError:
        reset_store_cache = None  # type: ignore[assignment]
    if reset_store_cache is not None:
        reset_store_cache()
    yield
    if reset_store_cache is not None:
        reset_store_cache()


# Pg-backed test fixtures (issue #1737, Slice A). Lives in a sibling
# module so the heavy testcontainers / psycopg imports stay lazy
# (``pytest_plugins`` is registered up-front but the fixtures within
# only do their import work when actually invoked by a test).
pytest_plugins = ["tests.conftest_pg"]


# ----------------------------------------------------------------------
# Work-service backend dispatch (issue #1737, Slice F)
# ----------------------------------------------------------------------
#
# The ``work_service`` fixture below is the single entry point tests
# should reach for when they want a work-service instance and don't care
# which backend is providing it. Behaviour is controlled by an opt-in
# ``@pytest.mark.backend(...)`` marker:
#
# * ``@pytest.mark.backend("sqlite")`` — force the sqlite path.
# * ``@pytest.mark.backend("postgres")`` — force the pg path (requires
#   the ``pg_schema_pool`` machinery in ``conftest_pg.py``; skipped if
#   Docker / a local pg is unavailable).
# * ``@pytest.mark.backend("both")`` — parameterise the test against
#   both backends; the test runs twice and the active backend is
#   identifiable via ``request.node.callspec.id``.
# * No marker — sqlite. Slice K (the ripout) flips this default to pg
#   and deletes the sqlite branch.
#
# Tests that need the concrete service class (i.e. that today instantiate
# ``SQLiteWorkService(...)`` directly) should migrate to the dispatch
# fixture incrementally as their owners port them. The fixture is
# intentionally not auto-applied — Slice F is plumbing, not a rewrite.


def _build_sqlite_work_service(tmp_path):
    """Build a fresh ``SQLiteWorkService`` against a tmp-path DB."""
    from pollypm.work.sqlite_service import SQLiteWorkService

    return SQLiteWorkService(db_path=tmp_path / "work.db")


def _build_pg_work_service(request):
    """Pull the per-test ``pg_work_service`` fixture via ``request``.

    The pg fixture chain may ``pytest.skip`` if Docker / a local pg with
    pgvector isn't reachable; we let that propagate so tests pinned to
    the pg backend skip cleanly on dev machines without Docker.
    """
    return request.getfixturevalue("pg_work_service")


def _resolve_backend_marker(request) -> str:
    """Read the ``@pytest.mark.backend(...)`` marker, default ``sqlite``.

    Returns the marker argument as a lowercase string. Unknown values
    fall back to sqlite so a typo doesn't silently route to the wrong
    backend — the dispatch path logs a warning when this happens.
    """
    marker = request.node.get_closest_marker("backend")
    if marker is None:
        return "sqlite"
    if not marker.args:
        return "sqlite"
    raw = str(marker.args[0]).strip().lower()
    if raw not in {"sqlite", "postgres", "both"}:
        import warnings

        warnings.warn(
            f"Unknown @pytest.mark.backend({marker.args[0]!r}); "
            "defaulting to 'sqlite'. Valid values: 'sqlite', 'postgres', 'both'.",
            stacklevel=2,
        )
        return "sqlite"
    return raw


@pytest.fixture
def work_service(request, tmp_path):
    """Dispatch fixture returning a work-service backed by the marker.

    See the comment block above for marker semantics. Tests that need
    a backend-specific service (e.g. they assert on pg row counts or
    sqlite pragmas) should reach for the concrete fixtures
    (``pg_work_service`` or build a ``SQLiteWorkService`` directly)
    instead.
    """
    backend = _resolve_backend_marker(request)
    if backend == "both":
        # ``both`` is implemented via the ``_work_service_backend``
        # parametrize hook below — when this fixture is invoked under a
        # ``both`` marker, the parametrize layer has already picked one
        # of ``sqlite`` / ``postgres`` and stashed it on the request.
        chosen = getattr(request, "param", "sqlite")
        backend = chosen
    if backend == "postgres":
        return _build_pg_work_service(request)
    return _build_sqlite_work_service(tmp_path)


def pytest_generate_tests(metafunc):
    """Parameterise ``work_service`` against both backends when marked.

    Implements the ``@pytest.mark.backend('both')`` half of the dispatch
    contract: when the marker is present AND the test asks for the
    ``work_service`` fixture, expand into two test items — one per
    backend. The ``work_service`` fixture above reads ``request.param``
    to pick the right side.
    """
    if "work_service" not in metafunc.fixturenames:
        return
    backend_marker = metafunc.definition.get_closest_marker("backend")
    if backend_marker is None or not backend_marker.args:
        return
    if str(backend_marker.args[0]).strip().lower() != "both":
        return
    metafunc.parametrize(
        "work_service",
        ["sqlite", "postgres"],
        indirect=True,
        ids=["sqlite", "postgres"],
    )


# ----------------------------------------------------------------------
# Production-state pollution guard — issue #1902
# ----------------------------------------------------------------------
#
# Two autouse fixtures defend against the failure mode that bit us
# during v1-RC user-flow testing: a runaway pytest writing to the
# real ``~/.pollypm/`` and spawning orphan Claude subprocesses that
# survived the test's exit.
#
# 1) ``_pollypm_home_write_guard`` — snapshots the entry-set of
#    write-prone subdirectories under ``~/.pollypm/`` before the
#    test, then diffs the post-test set. Any new files written
#    DURING the test that landed in the real home dir are surfaced
#    as a hard test failure. Tests that genuinely need to write to
#    ``~/.pollypm/`` (typically because they've already monkeypatched
#    ``Path.home``) opt out via ``@pytest.mark.allows_home_writes``.
#
# 2) ``_pollypm_orphan_reaper`` — records the PIDs of any running
#    ``claude``/``codex`` processes (owned by the current user)
#    BEFORE the test runs, and after the test KILLS any new PIDs
#    of those names that appeared. This catches the 87-minute orphan
#    Claude scenario from #1902: a test spawns ``claude --append-
#    system-prompt-file ...`` (directly or indirectly), the parent
#    pytest exits without reaping it, and the orphan keeps writing
#    to the real cockpit.
#
# Both fixtures are best-effort: a missing home dir, a stat race, or
# an inaccessible /proc on a sandboxed CI worker is treated as
# "nothing to guard / nothing to reap" rather than blocking the run.


def _real_pollypm_home() -> Path | None:
    """Return the dev machine's ``~/.pollypm`` (or ``None`` if unset).

    Uses ``os.path.expanduser`` rather than ``Path.home()`` so a test
    that monkeypatched ``Path.home`` doesn't accidentally redirect the
    guard at its own sandbox — we want the *real* home here.
    """
    home = os.environ.get("HOME") or os.path.expanduser("~")
    if not home or home == "~":
        return None
    candidate = Path(home) / ".pollypm"
    return candidate if candidate.is_dir() else None


# Cap the per-subdir scan so a pre-existing 281K-file scaffold leak
# (the doubled-path family from #1810, now fixed but possibly still
# on long-lived dev machines) doesn't make every test pay an
# O(scaffold) walk cost. When a subdir exceeds the cap, the guard
# falls back to the entry-set of its immediate children, which is
# still enough to catch a NEW top-level write (the #1902 shape
# was a fresh ``operator/`` checkpoint dir appearing).
_GUARD_SNAPSHOT_FILE_CAP = 5000


def _snapshot_guarded_dirs(pollypm_home: Path) -> dict[str, set[Path]]:
    """Return ``{subdir: {paths under that subdir}}`` for the guarded set.

    Each value is the recursive set of file paths under the subdir at
    snapshot time, capped at ``_GUARD_SNAPSHOT_FILE_CAP``. Comparison
    post-test uses set difference so the guard surfaces NEW files
    specifically (rather than counting or mtime-windowing, which is
    racy under parallel pytest workers).
    """
    snapshot: dict[str, set[Path]] = {}
    for name in _POLLYPM_HOME_GUARDED_DIRS:
        root = pollypm_home / name
        if not root.is_dir():
            snapshot[name] = set()
            continue
        try:
            entries: set[Path] = set()
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                entries.add(path)
                if len(entries) >= _GUARD_SNAPSHOT_FILE_CAP:
                    # Bail out — the dir is too big for a fine-grained
                    # diff. Switch to the shallow entry-set of the
                    # subdir's top-level children so the diff still
                    # catches new session/checkpoint dirs.
                    entries = {p for p in root.iterdir()}
                    break
            snapshot[name] = entries
        except OSError:
            # Walk failures (permissions, races) shouldn't break the
            # test run — fall back to an empty set so the post-diff
            # records every file as "new" only if the dir later
            # becomes readable. This is conservative.
            snapshot[name] = set()
    return snapshot


@pytest.fixture(autouse=True)
def _pollypm_home_write_guard(request):
    """Fail any test that writes new files to the real ``~/.pollypm/``.

    See module docstring above for the why (#1902). Skips when:

    - The test carries ``@pytest.mark.allows_home_writes`` (explicit
      opt-out — typically because the test monkeypatched ``Path.home``).
    - The real ``~/.pollypm/`` dir doesn't exist (clean dev machine /
      CI worker; nothing to pollute).
    """
    if request.node.get_closest_marker("allows_home_writes") is not None:
        yield
        return
    pollypm_home = _real_pollypm_home()
    if pollypm_home is None:
        yield
        return
    before = _snapshot_guarded_dirs(pollypm_home)
    yield
    after = _snapshot_guarded_dirs(pollypm_home)
    leaks: list[str] = []
    for subdir, before_set in before.items():
        new_files = after.get(subdir, set()) - before_set
        for path in sorted(new_files):
            leaks.append(str(path))
    if leaks:
        # Best-effort cleanup of the leaked files so the next test
        # starts clean and the dev machine doesn't accumulate cruft.
        # Failures to unlink are swallowed — the assertion error
        # below is the load-bearing signal.
        for leak in leaks:
            try:
                Path(leak).unlink()
            except OSError:
                pass
        joined = "\n  - ".join(leaks)
        pytest.fail(
            "Test wrote new files to the real ~/.pollypm/ "
            "(see issue #1902). Use tmp_path / monkeypatch.setattr "
            "on pollypm.config.GLOBAL_CONFIG_DIR + Path.home, or mark "
            "the test with @pytest.mark.allows_home_writes if the "
            "writes are genuinely intended.\n"
            f"  - {joined}",
            pytrace=False,
        )


def _list_process_table() -> list[tuple[int, int, str]]:
    """Return ``[(pid, ppid, comm), ...]`` for every visible process.

    ``ps -A -o pid=,ppid=,comm=`` works on macOS + Linux and doesn't
    require ``/proc``. Failures return an empty list — the caller
    treats that as "no candidates to reap" rather than blocking the
    test run.
    """
    try:
        out = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid=,comm="],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    rows: list[tuple[int, int, str]] = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid_str, ppid_str, comm = parts
        try:
            pid_i = int(pid_str)
            ppid_i = int(ppid_str)
        except ValueError:
            continue
        rows.append((pid_i, ppid_i, comm))
    return rows


def _list_pids_by_name(names: tuple[str, ...]) -> set[int]:
    """Return PIDs of processes whose ``comm`` basename matches ``names``.

    Used by the reaper to take a before/after snapshot of all
    claude/codex processes visible to the current user. The
    descendant-filter (below) is what decides which of those PIDs
    we're actually allowed to kill — the snapshot itself is
    intentionally broad so reparented orphans (ppid=1 after the
    test's pytest worker exited) stay visible.
    """
    pids: set[int] = set()
    for pid, _ppid, comm in _list_process_table():
        base = os.path.basename(comm)
        # Match exact basename so e.g. ``claude-flow`` doesn't get
        # reaped by a ``claude`` filter.
        if base in names:
            pids.add(pid)
    return pids


def _descendants_of(root_pid: int) -> set[int]:
    """Return the transitive set of descendant PIDs of ``root_pid``.

    Walks the process tree from a fresh ``ps`` snapshot — does not
    include ``root_pid`` itself. Used by the orphan reaper to scope
    its kill set: only processes whose parent chain traces back to
    the current pytest worker get reaped, so a cockpit Claude
    running on the dev machine in parallel with pytest never gets
    nuked by accident.
    """
    rows = _list_process_table()
    children: dict[int, list[int]] = {}
    for pid, ppid, _comm in rows:
        children.setdefault(ppid, []).append(pid)
    descendants: set[int] = set()
    stack = [root_pid]
    while stack:
        current = stack.pop()
        for child in children.get(current, ()):
            if child in descendants:
                continue
            descendants.add(child)
            stack.append(child)
    return descendants


def _reap_pids(pids: set[int]) -> None:
    """SIGTERM then SIGKILL each PID — best-effort, silent on failure.

    Most well-behaved Claude/Codex CLI children exit cleanly on
    SIGTERM; the SIGKILL fallback handles the bug-mode case from
    #1902 where the child had detached from its parent and stuck in
    a read loop. We pause briefly between signals to give the child
    a chance to clean up its own tmux pane and transcript file.
    """
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            continue
    # Give SIGTERMed processes a moment to exit cleanly before the
    # KILL fallback. 0.2s is short enough not to noticeably slow
    # tests but long enough for a Python/Node CLI to honour SIGTERM.
    if pids:
        time.sleep(0.2)
    for pid in pids:
        try:
            os.kill(pid, 0)  # probe — raises if already dead
        except (OSError, ProcessLookupError):
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            continue


@pytest.fixture(autouse=True)
def _pollypm_orphan_reaper():
    """Kill any ``claude``/``codex`` processes the test left behind.

    Two filters narrow what we actually reap:

    1. PID delta — only PIDs that appeared DURING the test (post-set
       minus pre-set) are candidates. A claude that was already
       running when the test started (e.g. the dev's own cockpit
       pane) is never touched.
    2. Descendant filter — even among new PIDs, we only kill those
       whose parent chain traces back to the current pytest worker
       PID. This protects against the race where a parallel cockpit
       (running outside pytest) happens to spawn a claude during
       the test window.

    Both filters re-run from a fresh ``ps`` snapshot so the
    descendant check sees the post-test process tree, including
    orphans whose ppid has been reparented to 1 (the canonical
    failure mode from #1902 — the orphaned ``claude --append-
    system-prompt-file`` survived parent exit, so by the time we
    look its ppid was 1, not its original test-spawner). For that
    reparented case the descendant walk wouldn't find it; we treat
    a ppid=1 claude that appeared during the test window as an
    orphan and reap it too.

    Best-effort: ``ps`` failures are swallowed so this fixture can
    never block a test run.
    """
    before = _list_pids_by_name(_POLLYPM_REAP_NAMES)
    yield
    after = _list_pids_by_name(_POLLYPM_REAP_NAMES)
    new_pids = after - before
    if not new_pids:
        return
    # Build a pid -> ppid map from a fresh post-test snapshot so we
    # can decide which new PIDs are reachable from the pytest worker.
    rows = _list_process_table()
    ppid_by_pid = {pid: ppid for pid, ppid, _comm in rows}
    own_descendants = _descendants_of(os.getpid())
    reap: set[int] = set()
    for pid in new_pids:
        if pid in own_descendants:
            reap.add(pid)
            continue
        # The reparented-orphan case: a Claude that the test spawned
        # but has since detached from pytest (ppid=1). We can't
        # prove pytest spawned it, but it (a) didn't exist before
        # the test, (b) is claude/codex, (c) is now an init child —
        # that's the exact #1902 shape and the right call is to
        # reap it. A user-launched cockpit claude would have ppid
        # pointing at the cockpit pane's tmux server, not init.
        if ppid_by_pid.get(pid) == 1:
            reap.add(pid)
    if reap:
        _reap_pids(reap)
