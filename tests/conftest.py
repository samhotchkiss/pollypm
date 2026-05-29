"""Project-wide pytest config.

Test-hygiene defaults that should apply to every test in this repo.
Module-specific fixtures live beside their tests.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
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

# Capture the operator home before any test fixture can monkeypatch
# ``HOME``. The real-home pollution guard below must continue watching
# the operator's ``~/.pollypm`` even for tests that deliberately run with
# a sandboxed home.
_OPERATOR_HOME = Path(os.environ.get("HOME") or os.path.expanduser("~")).expanduser()
_OPERATOR_POLLYPM_HOME = _OPERATOR_HOME / ".pollypm"


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
    # Post-sqlite-ripout (refs #1971): ``SQLAlchemyStore`` and the
    # ``sqlite`` backend entry are gone. The earlier in-process
    # re-registration this block performed (so legacy
    # ``@pytest.mark.backend("sqlite")`` tests and direct
    # ``get_store_by_url("sqlite:///...")`` calls kept resolving)
    # is intentionally dropped — any test that still pins itself
    # to sqlite will fail loudly at runtime, which is the correct
    # signal for the migration sweep.


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
# Work-service backend dispatch (issue #1737, Slice F; #1942; #1971)
# ----------------------------------------------------------------------
#
# The ``work_service`` fixture below is the single entry point tests
# should reach for when they want a work-service instance and don't care
# which backend is providing it.
#
# Post-sqlite-ripout (refs #1971) the only supported backend is pg.
# The ``@pytest.mark.backend(...)`` marker is retained as a no-op shim
# for legacy call sites — values other than ``postgres`` raise
# ``pytest.UsageError`` so the rewrite sweep surfaces stragglers.


def _build_pg_work_service(request):
    """Pull the per-test ``pg_work_service`` fixture via ``request``.

    The pg fixture chain may ``pytest.skip`` if Docker / a local pg with
    pgvector isn't reachable; we let that propagate so tests pinned to
    the pg backend skip cleanly on dev machines without Docker.
    """
    return request.getfixturevalue("pg_work_service")


def _resolve_backend_marker(request) -> str:
    """Read the ``@pytest.mark.backend(...)`` marker.

    Post-sqlite-ripout (refs #1971) only ``postgres`` is valid. Any
    other value (including the previously-supported ``sqlite`` /
    ``both``) raises ``pytest.UsageError`` so the migration finishes
    explicitly instead of leaking a silent skip.
    """
    marker = request.node.get_closest_marker("backend")
    if marker is None:
        return "postgres"
    if not marker.args:
        return "postgres"
    raw = str(marker.args[0]).strip().lower()
    if raw != "postgres":
        raise pytest.UsageError(
            f"Unsupported @pytest.mark.backend({marker.args[0]!r}). "
            "Only 'postgres' is valid post-sqlite-ripout (refs #1971)."
        )
    return raw


def _node_relpath(request) -> str:
    """Return the current test path relative to the repo root."""
    raw_path = getattr(request.node, "path", None)
    if raw_path is None:
        raw_path = getattr(request.node, "fspath", "")
    path = Path(str(raw_path)).resolve()
    root = Path(__file__).resolve().parents[1]
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _needs_live_state_isolation(request) -> bool:
    """True for tests that must not touch operator HOME or ambient pg."""
    rel = _node_relpath(request)
    return (
        rel.startswith("tests/web_api/")
        or rel == "tests/test_supervisor.py"
        or rel.startswith("tests/test_work_service")
    )


def _needs_pg_schema_isolation(request) -> bool:
    """True when a module must run through ``pg_schema_pool``."""
    return _node_relpath(request) == "tests/test_supervisor.py"


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _patch_module_path(module_name: str, attr: str, value: Path, monkeypatch) -> None:
    module = sys.modules.get(module_name)
    if module is not None and hasattr(module, attr):
        monkeypatch.setattr(module, attr, value)


@pytest.fixture(autouse=True)
def _isolate_web_api_and_work_service_live_state(
    request,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Sandbox HOME/config and guard PG DSN resolution for live-state tests.

    The targeted web_api/work_service/supervisor tests should never read the
    operator's ``~/.pollypm`` or fall through to the default local
    ``pollypm`` Postgres database. Production code still sees normal
    public config/env seams; the fixture just binds those seams to a
    pytest-owned sandbox for this subset.
    """
    if not _needs_live_state_isolation(request):
        yield
        return

    sandbox_home = tmp_path / "home"
    sandbox_pollypm = sandbox_home / ".pollypm"
    sandbox_pollypm.mkdir(parents=True, exist_ok=True)
    sandbox_config = sandbox_pollypm / "pollypm.toml"

    monkeypatch.setenv("HOME", str(sandbox_home))
    monkeypatch.setenv("POLLYPM_HOME", str(sandbox_pollypm))

    import pollypm.config as config_mod

    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_DIR", sandbox_pollypm)
    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", sandbox_config)
    _patch_module_path(
        "pollypm.cli_features.web_api",
        "DEFAULT_CONFIG_PATH",
        sandbox_config,
        monkeypatch,
    )
    _patch_module_path(
        "pollypm.web_api.token",
        "DEFAULT_TOKEN_PATH",
        sandbox_pollypm / "api-token",
        monkeypatch,
    )

    if _needs_pg_schema_isolation(request):
        # Supervisor construction opens the unified pg message Store
        # immediately. Bind those tests to the same per-test schema seam
        # used by the pg storage suites before any Supervisor can fall
        # through to pg_pool.DEFAULT_DSN.
        request.getfixturevalue("pg_schema_pool")

    try:
        from pollypm.storage import pg_pool
        from tests.conftest_pg import _is_ambient_live_pg_dsn
    except ImportError:
        pg_pool = None  # type: ignore[assignment]
        _is_ambient_live_pg_dsn = None  # type: ignore[assignment]

    if pg_pool is not None and _is_ambient_live_pg_dsn is not None:
        real_resolve_dsn = pg_pool.resolve_dsn

        def _guarded_resolve_dsn(config=None):
            dsn = real_resolve_dsn(config)
            if _is_ambient_live_pg_dsn(dsn):
                pytest.fail(
                    "Targeted test attempted to use the ambient local "
                    "pollypm Postgres database. Request pg_schema_pool "
                    "or pass an explicit isolated test DSN.",
                    pytrace=False,
                )
            return dsn

        monkeypatch.setattr(pg_pool, "resolve_dsn", _guarded_resolve_dsn)

    yield

    current_home = Path(os.environ.get("HOME", ""))
    if current_home == _OPERATOR_HOME:
        pytest.fail(
            "Targeted test restored HOME to the operator home; keep "
            "web_api/work_service tests bound to tmp_path.",
            pytrace=False,
        )

    if Path(config_mod.GLOBAL_CONFIG_DIR) == _OPERATOR_POLLYPM_HOME:
        pytest.fail(
            "Targeted test restored pollypm.config.GLOBAL_CONFIG_DIR to "
            "the operator ~/.pollypm.",
            pytrace=False,
        )

    if not _is_under(Path(config_mod.DEFAULT_CONFIG_PATH), sandbox_pollypm):
        pytest.fail(
            "Targeted test moved pollypm.config.DEFAULT_CONFIG_PATH outside "
            "the pytest PollyPM sandbox.",
            pytrace=False,
        )


@pytest.fixture
def work_service(request, tmp_path):
    """Dispatch fixture returning a pg-backed work service.

    Post-sqlite-ripout (refs #1971) this always returns the pg fixture
    — kept as a thin shim so the call-site API stays stable while
    callers migrate to ``pg_work_service`` directly.
    """
    _resolve_backend_marker(request)
    return _build_pg_work_service(request)


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

    Uses the import-time operator home so tests that monkeypatch
    ``HOME`` / ``Path.home`` don't accidentally redirect the guard at
    their own sandbox — we want the *real* home here.
    """
    if str(_OPERATOR_HOME) in {"", "~"}:
        return None
    candidate = _OPERATOR_POLLYPM_HOME
    return candidate if candidate.is_dir() else None


# Subdirs that are append-only on a busy dev machine and routinely
# grow into the hundreds of thousands of entries (the user's
# ``~/.pollypm/snapshots/`` carried ~1.4M files at the time of #2035).
# Even an ``os.scandir`` + ``stat`` per top-level child cost ~35s warm
# on that tree, which still blew the per-test budget. For these dirs
# we use a directory-mtime sentinel: the parent dir's mtime bumps when
# a top-level child is added/removed, so an unchanged mtime proves
# nothing was added during the test without enumerating ANY children.
# When the sentinel DOES change, we fall back to a shallow scandir to
# identify the specific new entry.
_GUARD_SHALLOW_ONLY: frozenset[str] = frozenset({
    "snapshots",
    "transcripts",
    "homes",
    "agent_homes",
    "worktrees",
})

# Safety cap across the recursive scan of a single subdir. The cap
# exists purely so a corrupted-tree symlink loop or a bug-introduced
# explosion of nested dirs can't make the guard itself hang. 200k
# tuples comfortably covers every non-append-only guarded subdir on a
# healthy dev machine.
_GUARD_SNAPSHOT_ENTRY_CAP = 200_000


# ``_GuardEntry`` shapes a single snapshot row.
# - For shallow-sentinel dirs: ``("__sentinel__", "s", mtime_ns, inode)``
#   — one tuple per dir, no enumeration cost on the hot path.
# - For recursive dirs (and shallow dirs whose sentinel changed):
#   ``(rel_path, "d"|"f", mtime_ns, size)`` — one tuple per entry.
# All fields are primitives so the frozenset hash is fast and diff
# is pure set arithmetic.
_GuardEntry = tuple[str, str, int, int]


def _shallow_sentinel(root: Path) -> _GuardEntry | None:
    """Return a one-tuple summary of ``root`` (no enumeration).

    Stats only the directory itself: ``(mtime_ns, inode)``. Adding /
    removing a top-level child bumps ``mtime_ns``. Returning ``None``
    means the dir vanished mid-stat — caller treats that as the
    empty-snapshot fallback (matches the previous OSError handling).
    """
    try:
        st = os.stat(root, follow_symlinks=False)
    except OSError:
        return None
    # ``st_mtime_ns`` gives nanosecond resolution on macOS / Linux,
    # which is enough to disambiguate two writes inside the same test.
    return ("__sentinel__", "s", st.st_mtime_ns, st.st_ino)


def _snapshot_guarded_dirs(
    pollypm_home: Path,
) -> dict[str, frozenset[_GuardEntry]]:
    """Return ``{subdir: frozenset of metadata tuples}`` for the guarded set.

    #2035 — the original implementation snapshotted recursive ``Path``
    sets using ``rglob('*')`` + ``is_file()``. On a long-lived dev
    machine (the user's ``~/.pollypm/snapshots/`` had 1.4M files), the
    per-entry stat cost made every pytest invocation hang for minutes,
    twice (pre + post snapshot per test). Even a shallow ``os.scandir``
    of that dir cost ~35s warm because each ``stat`` is a syscall and
    the cost is O(top-level children).

    The new shape:

    - Each value is a ``frozenset`` of metadata tuples.
    - Subdirs in ``_GUARD_SHALLOW_ONLY`` (append-only operator state
      like ``snapshots/``, ``transcripts/``, ``homes/``) record a
      one-tuple sentinel: ``(mtime_ns, inode)`` of the dir itself.
      The kernel bumps the dir's mtime when a child is added/removed,
      so the sentinel detects the #1902 leak shape (fresh top-level
      child appearing) without enumerating ANY children.
    - Other subdirs (``artifacts/``, ``checkpoints/``, ``dossier/``)
      recurse depth-first via ``os.scandir`` (one stat per entry,
      no ``Path.is_file()`` double-stat). Capped at
      ``_GUARD_SNAPSHOT_ENTRY_CAP``.
    - ``stat`` / ``scandir`` failures (permissions, races, ENOENT
      mid-walk) are swallowed per entry; the snapshot continues. A
      whole-dir ``OSError`` falls back to an empty frozenset —
      conservative, matches the previous behaviour.
    """
    snapshot: dict[str, frozenset[_GuardEntry]] = {}
    for name in _POLLYPM_HOME_GUARDED_DIRS:
        root = pollypm_home / name
        if not root.is_dir():
            snapshot[name] = frozenset()
            continue
        if name in _GUARD_SHALLOW_ONLY:
            sentinel = _shallow_sentinel(root)
            snapshot[name] = (
                frozenset((sentinel,)) if sentinel is not None
                else frozenset()
            )
            continue
        entries: list[_GuardEntry] = []
        try:
            # Iterative DFS with os.scandir so we never pay the
            # ``Path.is_file()`` double-stat per entry the original
            # impl did.
            stack: list[str] = [str(root)]
            root_str = str(root)
            done = False
            while stack and not done:
                current = stack.pop()
                try:
                    it = os.scandir(current)
                except OSError:
                    continue
                with it:
                    for de in it:
                        try:
                            is_dir = de.is_dir(follow_symlinks=False)
                            st = de.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        rel = os.path.relpath(de.path, root_str)
                        entries.append((
                            rel,
                            "d" if is_dir else "f",
                            int(st.st_mtime),
                            st.st_size,
                        ))
                        if len(entries) >= _GUARD_SNAPSHOT_ENTRY_CAP:
                            done = True
                            break
                        if is_dir:
                            stack.append(de.path)
        except OSError:
            # Whole-dir walk failure (e.g. root vanished between is_dir
            # check and scandir). Treat as nothing-to-guard rather than
            # blowing up the test run.
            snapshot[name] = frozenset()
            continue
        snapshot[name] = frozenset(entries)
    return snapshot


def _shallow_diff_after_sentinel_change(
    root: Path,
) -> list[str]:
    """Enumerate top-level children of ``root`` for leak attribution.

    Only called when a SHALLOW_ONLY dir's sentinel changed — at that
    point we know a top-level entry was added/removed/touched, so the
    O(top-level-children) cost is unavoidable. Returns relative names
    so the caller can rebuild absolute Paths.
    """
    names: list[str] = []
    try:
        with os.scandir(root) as it:
            for de in it:
                names.append(de.name)
                if len(names) >= _GUARD_SNAPSHOT_ENTRY_CAP:
                    break
    except OSError:
        return []
    return names


def _resolve_guard_entry(
    pollypm_home: Path,
    subdir: str,
    entry: _GuardEntry,
) -> Path:
    """Reconstruct an absolute ``Path`` from a snapshot diff tuple.

    The snapshot stores ``(rel_path, kind, mtime, size)``. The downstream
    leak-attribution helpers (``_looks_test_induced``,
    ``_file_written_during_test``) take a ``Path``; this rebuilds it from
    the subdir root + relative path captured at snapshot time.
    """
    rel_path = entry[0]
    return pollypm_home / subdir / rel_path


def _pytest_provenance_tokens() -> tuple[str, ...]:
    """Return string markers that identify writes from THIS pytest run.

    A file under ``~/.pollypm/`` whose content contains any of these
    tokens almost certainly originated from a test in this pytest
    process (vs. a concurrent live PollyPM cockpit/heartbeat running
    outside the test). Used by the home-write guard (#1938) to
    distinguish test-induced leaks from background-process writes.

    Tokens include:

    - The pytest-pid tmp prefix this conftest configured via the env
      vars at ``pytest_configure`` time (``pollypm-pytest-<pid>``).
    - The conventional pytest tmp marker (``pytest-of-<user>``).
    - The literal ``tmp_path`` token, which appears in tracebacks /
      transcripts when a test path leaks into rendered output (#1955 —
      the docstring claimed this was in the list, but it wasn't).
    """
    tokens: list[str] = []
    tokens.append(f"pollypm-pytest-{os.getpid()}")
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    if user:
        tokens.append(f"pytest-of-{user}")
    # ``pytest-of-`` without the user works as a weaker fallback for
    # CI runners where USER may not be populated; still pytest-specific
    # enough that no production cockpit code would emit it.
    tokens.append("pytest-of-")
    # #1955 — the original docstring claimed ``tmp_path`` was in this
    # list but the implementation never included it. A test that
    # rendered ``tmp_path`` into a cockpit artifact and dropped it in
    # the real ~/.pollypm/ would slip past the token check.
    tokens.append("tmp_path")
    return tuple(tokens)


def _live_cockpit_running() -> bool:
    """Return True iff a live PollyPM cockpit appears to be running.

    Checks ``~/.pollypm/rail_daemon.pid`` for a PID that names a live
    process. When the cockpit is up, the home-write guard can't safely
    treat every post-test write as a leak — the rail daemon and
    heartbeat continuously stage checkpoints / snapshots / state.db
    journals into the same dirs. Without this check we'd flag real
    operator artifacts and unlink them mid-flight (the #1938
    regression). When NO cockpit is running, any new file under
    ``~/.pollypm/`` written during the test is by definition test
    induced — that's the #1955 surface the original token-only guard
    was missing.

    Best-effort: a missing pid file, an unreadable pid value, or a
    process-table probe that fails are all treated as "no live cockpit"
    so the guard falls back to the stricter timestamp-based check.
    """
    pollypm_home = _real_pollypm_home()
    if pollypm_home is None:
        return False
    pid_path = pollypm_home / "rail_daemon.pid"
    try:
        pid_text = pid_path.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    try:
        pid = int(pid_text.splitlines()[0])
    except (ValueError, IndexError):
        return False
    if pid <= 0:
        return False
    # ``os.kill(pid, 0)`` is the portable liveness probe — ``OSError``
    # with ``errno == ESRCH`` means the PID is dead, ``EPERM`` means
    # alive-but-not-ours (still alive). Anything else is treated as
    # "can't tell, assume no cockpit" so the guard stays strict.
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _file_written_during_test(path: Path, test_start: float) -> bool:
    """Return True iff ``path`` was created/modified after ``test_start``.

    Uses ``mtime`` because it's the field PollyPM writers actually
    update on each rewrite. ``ctime`` would catch the inode change too
    but is platform-dependent. A stat race that loses the file is
    treated as "not during the test" — the snapshot diff already proved
    the path is new since pre-test, so the worst case is we miss a
    single leak we'd otherwise have caught by mtime, never a false
    positive.
    """
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return False
    return mtime >= test_start


def _looks_test_induced(path: Path, tokens: tuple[str, ...]) -> bool:
    """Return True iff ``path`` looks like it was written by this test.

    Two-pass check:

    1. Path heritage: if the file's path string itself contains a
       pytest tmp marker (``pytest-of-<user>``, ``pollypm-pytest-<pid>``,
       or the literal ``tmp_path``), it's clearly test-scoped.
    2. Content heritage: read up to 64 KiB of the file and check for
       the same tokens. Catches the case where a test fed pytest tmp
       paths into operator transcripts / checkpoints. Binary / unreadable
       files are treated as NOT test-induced (the safer default — we'd
       rather miss a leak than delete a live cockpit artifact).

    Anything else (no marker in path, no marker in content) is treated
    as a background-process write and left alone.
    """
    path_str = str(path)
    for token in tokens:
        if token in path_str:
            return True
    try:
        with path.open("rb") as fh:
            head = fh.read(65536)
    except OSError:
        return False
    try:
        text = head.decode("utf-8", errors="ignore")
    except (UnicodeDecodeError, AttributeError):
        return False
    return any(token in text for token in tokens)


@pytest.fixture(autouse=True)
def _pollypm_home_write_guard(request):
    """Fail any test that writes new files to the real ``~/.pollypm/``.

    See module docstring above for the why (#1902). Skips when:

    - The test carries ``@pytest.mark.allows_home_writes`` (explicit
      opt-out — typically because the test monkeypatched ``Path.home``).
    - The real ``~/.pollypm/`` dir doesn't exist (clean dev machine /
      CI worker; nothing to pollute).

    #1938 — narrow the guard so it only treats a new file as a leak
    when there's positive evidence it came from THIS pytest process.
    On a developer machine running a live cockpit/heartbeat alongside
    pytest, the snapshot-diff would otherwise flag (and delete) real
    background-process writes — operator checkpoints, snapshots,
    agent_home backups — as "test leaks". The original #1902 bug was
    a test writing pytest-fixture paths into the real cockpit; that
    shape is still caught because the writes carry pytest provenance
    tokens (pytest-of-<user>, pollypm-pytest-<pid>, ``tmp_path``).

    #1955 — the #1938 narrowing was too aggressive: a test that
    writes a plain artifact (no pytest token in path or content) to
    the real ~/.pollypm/ silently passed. When no live PollyPM
    cockpit is running, ANY new file written under ~/.pollypm/ during
    the test is by definition test induced — we fail those regardless
    of token presence. When a live cockpit IS running, we still
    require either a pytest provenance token OR mtime evidence that
    falls outside what a background writer could plausibly have done
    in the test window; in practice the simpler signal there is the
    token, so we keep the original token-only fallback to avoid
    flagging real operator artifacts.
    """
    if request.node.get_closest_marker("allows_home_writes") is not None:
        yield
        return
    pollypm_home = _real_pollypm_home()
    if pollypm_home is None:
        yield
        return
    test_start = time.time()
    cockpit_live = _live_cockpit_running()
    before = _snapshot_guarded_dirs(pollypm_home)
    yield
    after = _snapshot_guarded_dirs(pollypm_home)
    # #2035 — snapshot values are now ``frozenset[tuple]`` (metadata
    # tuples), not ``set[Path]``. For SHALLOW_ONLY dirs the value is a
    # single ``("__sentinel__", "s", mtime_ns, inode)`` tuple; if the
    # sentinel diff is non-empty, the dir's mtime changed during the
    # test and we enumerate its top-level children to find the new
    # entry. For recursive dirs the diff is a set of per-entry tuples.
    candidates: list[Path] = []
    for subdir, before_set in before.items():
        new_entries = after.get(subdir, frozenset()) - before_set
        if not new_entries:
            continue
        if subdir in _GUARD_SHALLOW_ONLY:
            # Sentinel changed → a top-level child was added/removed
            # OR the dir itself was touched during the test.
            #
            # If a live cockpit is running it constantly bumps these
            # dirs (writing snapshots/transcripts/homes/...), so the
            # sentinel will ALWAYS diff and any enumeration we do
            # here is wasted work that would just be logged as
            # unattributed. Skip the O(top-level-children) re-enum in
            # that case — it'd cost ~35s on a 200k-child snapshots/
            # dir and wouldn't change the leak verdict.
            if cockpit_live:
                continue
            # Cockpit NOT live → enumerate top-level children and let
            # ``_file_written_during_test`` filter to those whose
            # mtime falls inside the test window. The "before" set
            # isn't recoverable from the sentinel, so we treat ALL
            # current top-level children as candidates and rely on
            # the downstream mtime / token attribution to reject
            # pre-existing entries.
            child_names = _shallow_diff_after_sentinel_change(
                pollypm_home / subdir,
            )
            for child_name in sorted(child_names):
                candidates.append(pollypm_home / subdir / child_name)
            continue
        for entry in sorted(new_entries):
            candidates.append(
                _resolve_guard_entry(pollypm_home, subdir, entry),
            )
    if not candidates:
        return
    tokens = _pytest_provenance_tokens()
    leaks: list[str] = []
    unattributed: list[str] = []
    for path in candidates:
        token_match = _looks_test_induced(path, tokens)
        # #1955 — when NO live cockpit is running, any file written
        # during the test window is test induced (there's no other
        # writer that could have produced it). When a cockpit IS
        # running, fall back to the token-only check so we don't
        # delete real background-process artifacts.
        if token_match:
            leaks.append(str(path))
        elif not cockpit_live and _file_written_during_test(
            path, test_start,
        ):
            leaks.append(str(path))
        else:
            unattributed.append(str(path))
    if unattributed:
        # Background-process writes (live cockpit/heartbeat running in
        # parallel). Do NOT unlink — those are real operator artifacts.
        # Surface a debug-level note via the pytest captured output so a
        # developer chasing test flakiness can see what slipped through.
        print(
            "\n[home-write guard] ignored "
            f"{len(unattributed)} background ~/.pollypm/ write(s) "
            "(no pytest provenance markers): "
            + ", ".join(unattributed[:5])
            + (" ..." if len(unattributed) > 5 else "")
        )
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
