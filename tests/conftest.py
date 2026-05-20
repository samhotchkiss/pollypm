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
    candidates: list[Path] = []
    for subdir, before_set in before.items():
        new_files = after.get(subdir, set()) - before_set
        for path in sorted(new_files):
            candidates.append(path)
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
