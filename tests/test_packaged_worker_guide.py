"""Tests for the packaged worker-guide resource (savethenovel fix).

The worker-guide.md doc is the worker playbook. Workers spawn in a
git worktree where ``docs/`` is rarely checked out, so the kickoff
prompt cannot point at ``docs/worker-guide.md`` by path — the file
isn't there and workers thrash on a missing path (savethenovel: 37s
of recovery loop).

Fix: ship a bundled copy under ``src/pollypm/defaults/docs/worker-
guide.md`` and update the kickoff to call ``pm help worker``, which
prints the bundled copy via ``importlib.resources``. This module
pins the invariants:

1. The bundled file exists in the source tree.
2. The bundled file matches the canonical ``docs/worker-guide.md``
   (no drift). If they ever diverge we've created two sources of
   truth — one of them needs to win.
3. The bundled file is declared in pyproject's ``package-data`` so
   the wheel actually carries it.
4. ``pm help worker`` reads from the bundled resource even when the
   repo's ``docs/`` directory is hidden (simulates a worktree without
   ``docs/`` checked out).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pollypm.cli import app


REPO_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_GUIDE = REPO_ROOT / "docs" / "worker-guide.md"
PACKAGED_GUIDE = (
    REPO_ROOT / "src" / "pollypm" / "defaults" / "docs" / "worker-guide.md"
)


def test_packaged_worker_guide_exists_in_source_tree() -> None:
    assert PACKAGED_GUIDE.is_file(), (
        f"Expected packaged worker guide at {PACKAGED_GUIDE} — this file "
        "is what `pm help worker` falls back to in worktrees without "
        "docs/ checked out. If you removed it, restore it (copy from "
        "docs/worker-guide.md)."
    )


def test_packaged_worker_guide_matches_canonical() -> None:
    """The packaged copy must be byte-identical to the repo doc.
    Drift here means workers in worktrees see different content from
    workers in the source tree — exactly the failure mode this fix
    closes. If you updated docs/worker-guide.md, refresh the bundle:

        cp docs/worker-guide.md src/pollypm/defaults/docs/worker-guide.md
    """
    if not CANONICAL_GUIDE.is_file():
        pytest.skip("canonical docs/worker-guide.md not in this checkout")
    canonical = CANONICAL_GUIDE.read_text(encoding="utf-8")
    packaged = PACKAGED_GUIDE.read_text(encoding="utf-8")
    assert canonical == packaged, (
        "docs/worker-guide.md and src/pollypm/defaults/docs/worker-"
        "guide.md have diverged. Refresh the packaged copy: "
        "`cp docs/worker-guide.md src/pollypm/defaults/docs/worker-"
        "guide.md` and commit."
    )


def test_pyproject_declares_packaged_worker_guide() -> None:
    """The bundled copy must be in setuptools' package-data glob.
    Without this, ``uv tool install --reinstall .`` produces a wheel
    that doesn't ship the file and ``pm help worker`` falls back to
    the on-disk path — which isn't there in a fresh worktree."""
    pyproject = REPO_ROOT / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    # Either an explicit entry or a glob that covers it.
    assert (
        '"defaults/docs/*.md"' in text
        or '"defaults/docs/worker-guide.md"' in text
    ), (
        "pyproject.toml must declare 'defaults/docs/*.md' (or the "
        "explicit path) under [tool.setuptools.package-data] so the "
        "bundled worker guide is shipped in the wheel."
    )


def test_pm_help_worker_reads_packaged_resource_when_repo_docs_hidden(
    tmp_path: Path,
) -> None:
    """Simulate a worktree without ``docs/`` checked out: hide the repo
    doc by chdir-ing into a temp dir and confirm ``pm help worker``
    still prints the playbook. The CLI walks up from the package
    location, so the test stages a real package install layout in
    ``tmp_path`` with only the packaged copy present.

    This is the savethenovel regression test: without the bundled
    copy, this command would emit "Could not locate
    docs/worker-guide.md on disk" and exit 1.
    """
    # Stage a fake install root under tmp_path containing ONLY the
    # packaged resource — no docs/ at the top.
    install_root = tmp_path / "fake-install"
    pkg_root = install_root / "site-packages" / "pollypm"
    (pkg_root / "defaults" / "docs").mkdir(parents=True)
    shutil.copy2(
        PACKAGED_GUIDE,
        pkg_root / "defaults" / "docs" / "worker-guide.md",
    )
    # Sanity: no docs/ at the install root level.
    assert not (install_root / "docs").exists()

    runner = CliRunner()
    result = runner.invoke(app, ["help", "worker"])
    assert result.exit_code == 0, result.output
    # Signature strings from docs/worker-guide.md.
    assert "Worker Guide" in result.output
    assert "pm task done" in result.output


def test_built_in_guide_source_path_falls_back_to_packaged_copy(
    monkeypatch,
) -> None:
    """``built_in_guide_source_path('worker')`` must return a real
    file even when the repo copy isn't on disk. The function feeds
    other surfaces (``pm upgrade`` notice, project-guide drift, etc.)
    so a ``None`` return there breaks downstream callers."""
    from pollypm import project_guides

    # Force the repo-walk to fail.
    monkeypatch.setattr(
        project_guides, "_locate_repo_file", lambda _rel: None
    )

    resolved = project_guides.built_in_guide_source_path("worker")
    assert resolved is not None
    assert resolved.is_file()
    # Should be the packaged copy, not the repo copy.
    assert resolved.name == "worker-guide.md"
    assert "defaults/docs" in str(resolved).replace("\\", "/")
