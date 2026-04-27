"""Tests for prompt/docs/CLI contract verification helpers (#888)."""

from __future__ import annotations

from pathlib import Path

import pytest

from pollypm.contract_audit import (
    UnmarkedSnippet,
    WorkerGuideDuplicate,
    detect_worker_guide_duplication,
    extract_actor_references,
    find_unmarked_generated_snippets,
    known_actor_names,
    known_role_guide_paths,
    legacy_role_guide_paths_exist,
    looks_like_rich_traceback,
    role_guide_paths_exist,
)


# ---------------------------------------------------------------------------
# extract_actor_references
# ---------------------------------------------------------------------------


def test_extract_actor_references_finds_flags(tmp_path: Path) -> None:
    """The audit's #387 case: ``--actor user`` references must be
    extracted so the audit can verify them."""
    text = (
        "Run `pm task claim demo/5 --actor worker`.\n"
        "Then `pm task review demo/5 --actor reviewer`.\n"
    )
    refs = extract_actor_references(text, path=tmp_path / "doc.md")
    actors = {r.actor for r in refs}
    assert actors == {"worker", "reviewer"}


def test_extract_actor_references_records_line_numbers(tmp_path: Path) -> None:
    """Line numbers are part of the audit output so a developer
    can navigate to the source quickly."""
    text = "first line\nsecond line --actor worker\n"
    refs = extract_actor_references(text, path=tmp_path / "doc.md")
    assert len(refs) == 1
    assert refs[0].line == 2


def test_extract_actor_references_handles_empty(tmp_path: Path) -> None:
    """No matches → empty tuple, never raises."""
    assert extract_actor_references("", path=tmp_path / "doc.md") == ()


# ---------------------------------------------------------------------------
# known_actor_names
# ---------------------------------------------------------------------------


def test_known_actor_names_includes_role_keys() -> None:
    """Every canonical role key must be a known actor."""
    actors = known_actor_names()
    assert "operator_pm" in actors
    assert "worker" in actors
    assert "reviewer" in actors
    assert "architect" in actors


def test_known_actor_names_includes_user_sentinel() -> None:
    """``--actor user`` is the legitimate value for user-driven
    paths. The audit's #387 was about confusion, not banning ``user``."""
    actors = known_actor_names()
    assert "user" in actors


def test_known_actor_names_includes_persona_aliases() -> None:
    """Some docs use the persona name (``polly``, ``russell``)
    instead of the canonical role key. Those references are
    valid as long as the persona maps to a registered role."""
    actors = known_actor_names()
    assert "polly" in actors
    assert "russell" in actors


def test_real_docs_actor_references_are_known() -> None:
    """Cross-check: every ``--actor <name>`` reference in the
    actively-maintained worker guide is a known actor.

    A failing test here is the audit's #387 shape: docs naming
    an actor that no longer exists / never existed."""
    repo_root = Path(__file__).resolve().parent.parent
    guide = repo_root / "docs" / "worker-guide.md"
    if not guide.exists():
        pytest.skip("worker-guide.md not present")
    text = guide.read_text()
    refs = extract_actor_references(text, path=guide)
    known = known_actor_names()
    bad = [r for r in refs if r.actor not in known]
    assert not bad, f"unknown actors referenced: {[r.actor for r in bad]}"


# ---------------------------------------------------------------------------
# Role guide path verification
# ---------------------------------------------------------------------------


def test_known_role_guide_paths_returns_resolved_paths() -> None:
    """Every guide path must be returned as an absolute resolved
    Path so existence checks are unambiguous."""
    paths = known_role_guide_paths()
    for path in paths:
        assert path.is_absolute()


def test_role_guide_paths_resolve_on_disk() -> None:
    """Every guide path the role contract names must exist on
    disk. The audit (#258 / #390) cites the failure shape: docs
    point at a renamed file."""
    missing = role_guide_paths_exist()
    assert missing == (), f"missing guides: {missing}"


def test_legacy_role_guide_paths_resolve_on_disk() -> None:
    """#897 — any legacy heartbeat persona table that still
    publishes a ``role -> guide_path`` mapping must point at
    files that exist. Today the heartbeat's table is derived
    from the canonical registry so this check is structurally
    safe; the audit catches any future refactor that
    re-introduces a hand-maintained legacy table."""
    missing = legacy_role_guide_paths_exist()
    assert missing == (), f"legacy heartbeat tables: {missing}"


# ---------------------------------------------------------------------------
# Rich-traceback detection
# ---------------------------------------------------------------------------


def test_traceback_detector_flags_obvious_traceback() -> None:
    """The audit's #851 case must be detected."""
    text = (
        'Traceback (most recent call last):\n'
        '  File "x.py", line 1, in <module>\n'
        '    raise ValueError("nope")\n'
        'ValueError: nope\n'
    )
    assert looks_like_rich_traceback(text) is True


def test_traceback_detector_passes_clean_error_message() -> None:
    """A normal CLI error (no Python frames) must NOT be flagged
    — that's the user-facing shape we want."""
    text = "Error: missing role 'worker' for task demo/5. Try `pm sessions`."
    assert looks_like_rich_traceback(text) is False


def test_traceback_detector_handles_empty() -> None:
    assert looks_like_rich_traceback("") is False


def test_traceback_detector_requires_two_fragments() -> None:
    """A single ``File "..."`` mention without other traceback
    markers is not enough — too noisy otherwise."""
    text = 'See File "thing.md" for context.'
    assert looks_like_rich_traceback(text) is False


# ---------------------------------------------------------------------------
# Generated-snippet marker
# ---------------------------------------------------------------------------


def test_unmarked_generated_snippets_flags_missing_marker(
    tmp_path: Path,
) -> None:
    """A doc named like a CLI reference but missing the marker
    is a drift candidate."""
    path = tmp_path / "cli-reference.md"
    path.write_text("# CLI Reference\n\n## pm up\n\nDescription...\n")
    out = find_unmarked_generated_snippets([path])
    assert len(out) == 1
    assert isinstance(out[0], UnmarkedSnippet)


def test_unmarked_generated_snippets_passes_when_marker_present(
    tmp_path: Path,
) -> None:
    """A doc carrying the canonical marker passes."""
    path = tmp_path / "cli-reference.md"
    path.write_text(
        "<!-- generated-from-cli -->\n# CLI Reference\n"
    )
    out = find_unmarked_generated_snippets([path])
    assert out == ()


def test_unmarked_generated_snippets_skips_unrelated_filenames(
    tmp_path: Path,
) -> None:
    """Only filenames that look generated are inspected."""
    path = tmp_path / "architecture.md"
    path.write_text("# Architecture\n\nNothing CLI-y.\n")
    out = find_unmarked_generated_snippets([path])
    assert out == ()


# ---------------------------------------------------------------------------
# Worker-guide duplication
# ---------------------------------------------------------------------------


def test_worker_guide_duplication_detects_double_inject(
    tmp_path: Path,
) -> None:
    """Two copies of the canonical section in one doc → flagged."""
    path = tmp_path / "guide.md"
    path.write_text(
        "## Your job in one paragraph\nFirst copy\n"
        "## Your job in one paragraph\nSecond copy\n"
    )
    out = detect_worker_guide_duplication(paths=[path])
    assert len(out) == 1
    assert out[0].canonical_section_count == 2


def test_worker_guide_duplication_passes_on_single_copy(
    tmp_path: Path,
) -> None:
    """One copy is fine."""
    path = tmp_path / "guide.md"
    path.write_text(
        "## Your job in one paragraph\nOnly copy\n"
    )
    out = detect_worker_guide_duplication(paths=[path])
    assert out == ()


def test_worker_guide_duplication_default_paths_are_clean() -> None:
    """The live worker-guide doc set must not contain duplicate
    canonical sections — a passing test confirms the
    #487/#488/#489/#490 family stays fixed."""
    out = detect_worker_guide_duplication()
    assert out == (), [str(d) for d in out]


# ---------------------------------------------------------------------------
# Type sanity
# ---------------------------------------------------------------------------


def test_referenced_actor_dataclass_is_immutable(tmp_path: Path) -> None:
    """Frozen dataclass — callers cannot mutate audit results."""
    refs = extract_actor_references("--actor worker", path=tmp_path / "x.md")
    with pytest.raises((AttributeError, TypeError)):
        refs[0].actor = "hacked"  # type: ignore[misc]


def test_worker_guide_duplicate_is_simple_dataclass(tmp_path: Path) -> None:
    dup = WorkerGuideDuplicate(
        path=tmp_path / "x.md",
        canonical_section_count=2,
    )
    assert dup.canonical_section_count == 2
