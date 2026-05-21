"""Performance + correctness regression tests for ``_snapshot_guarded_dirs``.

Issue #2035 — the original autouse home-write guard recursively walked
every guarded subdir under ``~/.pollypm/`` twice per test via
``Path.rglob('*')`` + ``is_file()``. On dev machines whose
``~/.pollypm/snapshots/`` had grown to ~1.4M files, every pytest
invocation hung for minutes before the first test even ran. The fix
switched to a metadata-tuple snapshot (``os.scandir``-based, shallow for
known append-only dirs).

These tests lock in two properties:

1. **Performance** — snapshot of a 1000-file ``~/.pollypm/``-shaped
   tree completes in < 500 ms.
2. **Correctness** — a new file appearing under any guarded subdir
   (top-level OR deep, append-only OR not) still shows up in the
   ``before`` → ``after`` diff. The opt-out / detection contract is
   preserved.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from tests.conftest import (
    _GUARD_SHALLOW_ONLY,
    _POLLYPM_HOME_GUARDED_DIRS,
    _shallow_diff_after_sentinel_change,
    _snapshot_guarded_dirs,
)


# Pytest tmp paths carry ``pytest-of-<user>`` markers; without the
# opt-out the home-write guard fixture itself would treat fixture
# scaffolding inside ``tmp_path`` as a leak when we point the snapshot
# at a synthetic home under ``tmp_path``. We bypass it here — the
# fixture-under-test is the snapshot helper, not the guard wrapper.
pytestmark = pytest.mark.allows_home_writes


def _build_pollypm_shaped_tree(root: Path, total_files: int = 1000) -> int:
    """Build a synthetic ``~/.pollypm/`` mirror with ``total_files`` files.

    The layout mirrors the shape of a real long-lived dev machine: most
    files concentrated under ``snapshots/`` (which is in
    ``_GUARD_SHALLOW_ONLY``), the rest spread across ``homes/`` and
    ``artifacts/``. Returns the actual file count created.
    """
    root.mkdir(parents=True, exist_ok=True)
    layout = [
        ("snapshots", int(total_files * 0.60), 4),
        ("homes", int(total_files * 0.20), 3),
        ("artifacts", int(total_files * 0.10), 3),
        ("agent_homes", int(total_files * 0.04), 2),
        ("transcripts", int(total_files * 0.04), 2),
        ("dossier", int(total_files * 0.02), 2),
    ]
    created = 0
    for name, n, depth in layout:
        base = root / name
        for i in range(n):
            sub = base
            # Cluster every 50 files into the same leaf dir so the tree
            # has realistic fan-out (not 1 file per dir).
            for d in range(depth):
                sub = sub / f"d{d}_{i // 50}"
            sub.mkdir(parents=True, exist_ok=True)
            (sub / f"f_{i}.txt").write_text(f"content {i}")
            created += 1
    return created


def test_snapshot_guarded_dirs_completes_under_budget(tmp_path: Path) -> None:
    """Snapshot of a ~1000-file pollypm-shaped tree finishes well under 500 ms.

    The 500 ms budget is the public contract from #2035 — the actual
    measured time on the synthetic tree is in the low double-digit
    milliseconds, so this leaves >10x headroom for slower CI runners.
    """
    home = tmp_path / ".pollypm"
    created = _build_pollypm_shaped_tree(home, total_files=1000)
    assert created >= 900, "synthetic tree should hit ~1000 files"

    # Warm the page cache (the cold-run cost is dominated by disk I/O on
    # the first scan; the per-test cost during a pytest run is the warm
    # number, which is what users actually feel).
    _snapshot_guarded_dirs(home)

    t0 = time.monotonic()
    snap = _snapshot_guarded_dirs(home)
    elapsed = time.monotonic() - t0

    # Sanity: the snapshot actually captured entries (not silently empty).
    total_entries = sum(len(v) for v in snap.values())
    assert total_entries > 0, "snapshot returned no entries — fixture broken"

    # Headline assertion.
    assert elapsed < 0.5, (
        f"_snapshot_guarded_dirs took {elapsed * 1000:.1f} ms on a "
        f"{created}-file synthetic tree; budget is 500 ms (#2035)."
    )


def test_snapshot_diffs_new_top_level_dir(tmp_path: Path) -> None:
    """A new top-level dir under a guarded subdir shows up in the diff.

    This is the #1902 leak shape: a test (or orphaned subprocess)
    creating ``~/.pollypm/artifacts/operator/...``.
    """
    home = tmp_path / ".pollypm"
    (home / "artifacts").mkdir(parents=True)

    before = _snapshot_guarded_dirs(home)
    (home / "artifacts" / "operator").mkdir()
    (home / "artifacts" / "operator" / "leaked.json").write_text("oops")
    after = _snapshot_guarded_dirs(home)

    diff = after["artifacts"] - before["artifacts"]
    rel_paths = {entry[0] for entry in diff}
    assert "operator" in rel_paths or any(
        p.startswith("operator") for p in rel_paths
    ), f"expected new operator/ entry in diff; got {rel_paths!r}"


def test_snapshot_diffs_new_deep_file_in_recursing_dir(tmp_path: Path) -> None:
    """A deep new file in a non-shallow guarded dir surfaces in the diff."""
    home = tmp_path / ".pollypm"
    deep_parent = home / "artifacts" / "a" / "b" / "c"
    deep_parent.mkdir(parents=True)

    before = _snapshot_guarded_dirs(home)
    (deep_parent / "leaked.json").write_text("oops")
    after = _snapshot_guarded_dirs(home)

    diff = after["artifacts"] - before["artifacts"]
    rel_paths = {entry[0] for entry in diff}
    # The leaked file's relative path uses os.sep.
    expected = os.path.join("a", "b", "c", "leaked.json")
    assert expected in rel_paths, (
        f"expected {expected!r} in diff; got {rel_paths!r}"
    )


def test_snapshot_diffs_new_top_level_under_shallow_only_dir(
    tmp_path: Path,
) -> None:
    """Shallow-only dirs (snapshots/, transcripts/, ...) still detect
    new top-level entries via the directory-mtime sentinel.

    The sentinel encodes ``(__sentinel__, "s", mtime_ns, inode)`` — when
    a top-level child is added, the kernel bumps the dir mtime so the
    sentinel tuple differs and the set-diff is non-empty. This is the
    documented #1902-style leak shape.
    """
    home = tmp_path / ".pollypm"
    (home / "snapshots").mkdir(parents=True)

    before = _snapshot_guarded_dirs(home)
    # Sleep just enough to guarantee the dir mtime tick crosses a
    # nanosecond boundary on filesystems that round mtime.
    time.sleep(0.01)
    new_dir = home / "snapshots" / "fresh_session"
    new_dir.mkdir()
    (new_dir / "checkpoint.json").write_text("oops")
    after = _snapshot_guarded_dirs(home)

    # Sentinel diff must be non-empty (mtime changed).
    diff = after["snapshots"] - before["snapshots"]
    assert diff, (
        "expected sentinel diff after writing into snapshots/; "
        f"before={before['snapshots']!r} after={after['snapshots']!r}"
    )
    # And the sentinel-changed fallback enumeration must surface the
    # new top-level entry.
    names = _shallow_diff_after_sentinel_change(home / "snapshots")
    assert "fresh_session" in names, (
        f"expected fresh_session in enumeration; got {names!r}"
    )


def test_snapshot_returns_empty_for_missing_pollypm_home(tmp_path: Path) -> None:
    """Snapshot of a non-existent ~/.pollypm/ is empty, not raising."""
    home = tmp_path / "does_not_exist"
    snap = _snapshot_guarded_dirs(home)
    assert set(snap.keys()) == set(_POLLYPM_HOME_GUARDED_DIRS)
    assert all(len(v) == 0 for v in snap.values())


def test_shallow_only_set_is_subset_of_guarded_dirs() -> None:
    """Every shallow-only entry must exist in the guarded-dirs tuple.

    Otherwise the shallow-only set would silently no-op for a typo'd
    name and we'd quietly do a full recursion on a dir we meant to
    keep shallow.
    """
    assert _GUARD_SHALLOW_ONLY.issubset(set(_POLLYPM_HOME_GUARDED_DIRS)), (
        f"_GUARD_SHALLOW_ONLY has names not in _POLLYPM_HOME_GUARDED_DIRS: "
        f"{_GUARD_SHALLOW_ONLY - set(_POLLYPM_HOME_GUARDED_DIRS)}"
    )


def test_guard_fixture_detects_write_to_recursive_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: the autouse fixture's diff flow detects a leak.

    Points ``_real_pollypm_home`` at a sandbox + drives the snapshot
    flow manually so we can assert the diff surfaces a deliberate write
    under a recursive (non-shallow) guarded dir. This is the contract
    that protects the v1-RC test isolation guarantee (#1902).
    """
    import tests.conftest as conftest_mod

    home = tmp_path / ".pollypm"
    (home / "artifacts").mkdir(parents=True)
    monkeypatch.setattr(conftest_mod, "_real_pollypm_home", lambda: home)

    before = conftest_mod._snapshot_guarded_dirs(home)
    # Deliberate "test leak" — a fresh top-level dir + file.
    leak_dir = home / "artifacts" / "leaked_checkpoint"
    leak_dir.mkdir()
    (leak_dir / "leak.json").write_text("pytest-of-sam wrote here")
    after = conftest_mod._snapshot_guarded_dirs(home)

    diff = after["artifacts"] - before["artifacts"]
    assert diff, "diff should be non-empty for a recursive-dir leak"
    # The diff should rebuild back to absolute paths.
    paths = [
        conftest_mod._resolve_guard_entry(home, "artifacts", e)
        for e in diff
    ]
    assert any(str(p).endswith("leak.json") for p in paths), (
        f"expected leak.json in reconstructed paths; got {paths!r}"
    )


def test_guard_fixture_detects_write_to_shallow_only_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a new top-level entry under a SHALLOW_ONLY dir
    surfaces via the sentinel diff + fallback enumeration.

    Confirms the v1-RC isolation guarantee survives the perf rewrite
    for dirs that we no longer enumerate on the hot path.
    """
    import tests.conftest as conftest_mod

    home = tmp_path / ".pollypm"
    (home / "snapshots").mkdir(parents=True)
    monkeypatch.setattr(conftest_mod, "_real_pollypm_home", lambda: home)

    before = conftest_mod._snapshot_guarded_dirs(home)
    time.sleep(0.01)
    (home / "snapshots" / "test_leak").mkdir()
    after = conftest_mod._snapshot_guarded_dirs(home)

    assert after["snapshots"] - before["snapshots"], (
        "sentinel diff should fire for top-level write to snapshots/"
    )
    names = conftest_mod._shallow_diff_after_sentinel_change(
        home / "snapshots",
    )
    assert "test_leak" in names
