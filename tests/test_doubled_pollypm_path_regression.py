"""Regression tests for the doubled-pollypm-path leak (#1810, #1950).

Background
----------
``_resolve_pollypm_root(project_path)`` in :mod:`pollypm.projects`
collapses ``GLOBAL_CONFIG_DIR / ".pollypm"`` back to
``GLOBAL_CONFIG_DIR`` so callers that pass the user-global config dir
itself don't accumulate a phantom ``~/.pollypm/.pollypm/...`` tree.

PR #1898 wired the ``project_*_dir()`` helpers in
``pollypm.projects`` through that guard, which fixed scaffold and
artifact paths. But #1950 found additional leak sites that bypass the
helpers and construct ``<path> / ".pollypm" / ...`` directly:

  - ``doc_scaffold.scaffold_docs`` / ``verify_docs`` — SYSTEM.md,
    rules-manifest.md, docs/reference/*.md
  - ``ensure_project_scaffold`` — the ``PROJECT_CONFIG_DIRNAME`` arm
    that hard-coded ``.pollypm/config``
  - ``session_intelligence._cursor_path`` /
    ``_pending_knowledge_dir`` / ``_read_new_events`` /
    ``sweep_all_sessions``
  - ``knowledge_extract._transcript_root``
  - ``memory_curator`` log + inbox-summary paths

These tests assert that calling each previously-leaky entrypoint with
``project_path == GLOBAL_CONFIG_DIR`` does NOT create any files under
``<GLOBAL_CONFIG_DIR>/.pollypm/``.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def fake_global_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Patch ``GLOBAL_CONFIG_DIR`` to a fresh sandbox per test."""
    fake = tmp_path / ".pollypm"
    fake.mkdir()
    import pollypm.config as config_mod

    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_DIR", fake)
    return fake


def _assert_no_doubled(global_dir: Path) -> None:
    doubled = global_dir / ".pollypm"
    if not doubled.exists():
        return
    leaked = sorted(p.relative_to(doubled) for p in doubled.rglob("*") if p.is_file())
    assert not leaked, (
        f"Doubled path leak: {len(leaked)} files under "
        f"{doubled}: {leaked[:5]}..."
    )


# ---------------------------------------------------------------------------
# Direct resolver checks — sanity for the doubled-path guard itself.
# ---------------------------------------------------------------------------


def test_resolve_pollypm_root_collapses_global_dir(fake_global_dir: Path) -> None:
    from pollypm.projects import _resolve_pollypm_root

    assert _resolve_pollypm_root(fake_global_dir) == fake_global_dir


def test_resolve_pollypm_root_appends_for_normal_project(tmp_path: Path) -> None:
    from pollypm.projects import _resolve_pollypm_root

    project = tmp_path / "myproject"
    project.mkdir()
    assert _resolve_pollypm_root(project) == project / ".pollypm"


# ---------------------------------------------------------------------------
# ensure_project_scaffold + doc_scaffold (#1950 scaffolding path).
# ---------------------------------------------------------------------------


def test_ensure_project_scaffold_with_global_dir_does_not_double(
    fake_global_dir: Path,
) -> None:
    from pollypm.projects import ensure_project_scaffold

    ensure_project_scaffold(fake_global_dir)
    _assert_no_doubled(fake_global_dir)


def test_scaffold_docs_with_global_dir_does_not_double(
    fake_global_dir: Path,
) -> None:
    from pollypm.doc_scaffold import scaffold_docs

    scaffold_docs(fake_global_dir)
    _assert_no_doubled(fake_global_dir)


def test_verify_docs_with_global_dir_does_not_double(
    fake_global_dir: Path,
) -> None:
    from pollypm.doc_scaffold import verify_docs

    # Should run without raising and without touching the doubled tree.
    verify_docs(fake_global_dir)
    _assert_no_doubled(fake_global_dir)


# ---------------------------------------------------------------------------
# session_intelligence (5-minute sweep iterates over root_dir).
# ---------------------------------------------------------------------------


def test_session_intelligence_cursor_path_with_global_dir(
    fake_global_dir: Path,
) -> None:
    from pollypm.session_intelligence import _cursor_path, _save_cursors

    p = _cursor_path(fake_global_dir)
    assert ".pollypm/.pollypm" not in str(p), p
    _save_cursors(fake_global_dir, {"session1/events.jsonl": 0})
    _assert_no_doubled(fake_global_dir)


def test_session_intelligence_pending_knowledge_with_global_dir(
    fake_global_dir: Path,
) -> None:
    from pollypm.session_intelligence import _pending_knowledge_dir

    p = _pending_knowledge_dir(fake_global_dir)
    assert ".pollypm/.pollypm" not in str(p), p


# ---------------------------------------------------------------------------
# knowledge_extract (15-minute extractor iterates over root_dir).
# ---------------------------------------------------------------------------


def test_knowledge_extract_transcript_root_with_global_dir(
    fake_global_dir: Path,
) -> None:
    from pollypm.knowledge_extract import _save_checkpoint, _transcript_root

    p = _transcript_root(fake_global_dir)
    assert ".pollypm/.pollypm" not in str(p), p
    _save_checkpoint(fake_global_dir, {"foo": 1})
    _assert_no_doubled(fake_global_dir)


# ---------------------------------------------------------------------------
# history_import — used by ``pm import`` against any project root.
# ---------------------------------------------------------------------------


def test_history_import_state_path_with_global_dir(
    fake_global_dir: Path,
) -> None:
    from pollypm.history_import import _import_state_path

    p = _import_state_path(fake_global_dir)
    assert ".pollypm/.pollypm" not in str(p), p


# ---------------------------------------------------------------------------
# memory_curator — iterates ``_all_project_roots`` including root_dir.
# ---------------------------------------------------------------------------


def test_memory_curator_inbox_summary_with_global_dir(
    fake_global_dir: Path,
) -> None:
    from pollypm.plugins_builtin.memory_curator.plugin import _emit_inbox_summary

    _emit_inbox_summary(fake_global_dir, "summary text\n")
    _assert_no_doubled(fake_global_dir)
