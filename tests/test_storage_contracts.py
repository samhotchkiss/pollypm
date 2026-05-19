"""Tests for the storage source-of-truth read API contract (#887)."""

from __future__ import annotations

import importlib

import pytest

from pollypm.storage_contracts import (
    LEGACY_WRITERS,
    ReadAPI,
    STORAGE_CONTRACTS,
    StorageConcept,
    all_concepts_have_canonical_reader,
    audit_legacy_writers,
    canonical_reader_for,
    reader_module_paths,
    tracked_legacy_writers,
)


# ---------------------------------------------------------------------------
# Coverage: every concept has a canonical reader
# ---------------------------------------------------------------------------


def test_every_concept_has_a_canonical_reader() -> None:
    """The release gate (#889) consults this. A new concept added
    without a canonical reader is a launch blocker."""
    assert all_concepts_have_canonical_reader() == ()


def test_canonical_reader_for_returns_read_api() -> None:
    api = canonical_reader_for(StorageConcept.INBOX_ITEM)
    assert isinstance(api, ReadAPI)
    assert api.module
    assert api.function


# ---------------------------------------------------------------------------
# Reader symbols actually exist
# ---------------------------------------------------------------------------


def test_canonical_readers_resolve_at_import() -> None:
    """Every registered ``module:function`` pair must resolve.

    The audit's recurring shape was: a documented canonical
    reader pointed at a function that no longer existed (#377).
    Resolving every pair at import time turns that into an
    immediate test failure rather than a runtime traceback."""
    failures: list[str] = []
    for concept, api in STORAGE_CONTRACTS.items():
        try:
            mod = importlib.import_module(api.module)
        except ImportError as exc:  # noqa: PERF203 — error path is rare
            failures.append(
                f"{concept.name}: cannot import module {api.module}: {exc}"
            )
            continue
        # Walk dotted paths so "Class.method" works.
        target = mod
        for part in api.function.split("."):
            target = getattr(target, part, None)
            if target is None:
                failures.append(
                    f"{concept.name}: {api.module}.{api.function} "
                    f"does not exist"
                )
                break
    assert not failures, "\n".join(failures)


def test_dotted_string_format() -> None:
    """``ReadAPI.dotted`` produces ``module:function`` for log lines."""
    api = canonical_reader_for(StorageConcept.SESSION)
    assert ":" in api.dotted
    assert api.module in api.dotted
    assert api.function in api.dotted


# ---------------------------------------------------------------------------
# Module-path helper
# ---------------------------------------------------------------------------


def test_reader_module_paths_returns_unique_sorted() -> None:
    """Used by the boundary tests; must be deterministic."""
    paths = reader_module_paths()
    assert paths == tuple(sorted(paths))
    assert len(paths) == len(set(paths))


def test_reader_module_paths_includes_known_modules() -> None:
    """Sanity: the high-traffic modules appear in the path set.

    Slice K-tests: ``pollypm.work.sqlite_service`` still appears in the
    contract today; K-source-ripout flips this to ``pollypm.work.pg_service``
    when the sqlite module is removed. The assertion accepts either to
    stay green across the source-ripout boundary."""
    paths = set(reader_module_paths())
    assert "pollypm.signal_routing" in paths
    assert (
        "pollypm.work.sqlite_service" in paths
        or "pollypm.work.pg_service" in paths
    )
    assert "pollypm.storage.state" in paths


# ---------------------------------------------------------------------------
# Legacy writer audit
# ---------------------------------------------------------------------------


def test_audit_legacy_writers_returns_only_blocking_seams() -> None:
    """``audit_legacy_writers`` returns *blocking* entries — those
    that are neither isolated nor tracked under a migration issue.
    Tracked entries surface via :func:`tracked_legacy_writers`
    instead so the release gate can downgrade them to warnings.

    Today every entry is either isolated or tracked, so the
    blocking list is empty. Adding an untracked, unisolated entry
    is a launch blocker — that is the launch-hardening invariant
    we want here."""
    assert audit_legacy_writers() == ()


def test_tracked_legacy_writers_surface_notification_staging() -> None:
    """notification_staging migration is tracked under #704 and
    must surface in the warning lane, not the blocking lane."""
    tracked = tracked_legacy_writers()
    assert any("notification_staging" in line for line in tracked)
    assert any("#704" in line for line in tracked)


def test_audit_legacy_writers_skips_isolated() -> None:
    """A legacy writer marked ``is_isolated=True`` does not appear
    in the audit output. That is the path #704 is migrating
    toward."""
    rows = audit_legacy_writers()
    # The "per-task workspace DB writes" entry is is_isolated=True
    # and must not be in the audit.
    assert all("workspace DB" not in r for r in rows), rows


def test_legacy_writer_records_migration_plan() -> None:
    """Every legacy writer must declare a migration plan and a
    removal condition. Half-described entries leak intent."""
    for writer in LEGACY_WRITERS:
        assert writer.migration_plan, writer.name
        assert writer.removal_condition, writer.name


def test_legacy_writers_each_reference_known_concept() -> None:
    """A writer whose concept is not in :class:`StorageConcept`
    is an outdated entry."""
    for writer in LEGACY_WRITERS:
        assert isinstance(writer.concept, StorageConcept), writer.name


# ---------------------------------------------------------------------------
# Notes / descriptions
# ---------------------------------------------------------------------------


def test_every_read_api_has_description() -> None:
    """Empty descriptions slip through code review; the audit
    asserts every entry is documented."""
    for concept, api in STORAGE_CONTRACTS.items():
        assert api.description.strip(), concept.name


# ---------------------------------------------------------------------------
# Reader stability (release-gate adjacent)
# ---------------------------------------------------------------------------


def test_inbox_count_canonical_is_signal_routing() -> None:
    """Cycle 87 / #883 made signal_routing the canonical
    re-export for inbox count. The contract must agree."""
    api = canonical_reader_for(StorageConcept.INBOX_ITEM)
    assert api.module == "pollypm.signal_routing"
    assert api.function == "shared_inbox_count"


def test_alert_count_canonical_is_signal_routing() -> None:
    """Same as inbox: alerts go through signal_routing for the
    operational filter (#879)."""
    api = canonical_reader_for(StorageConcept.ALERT)
    assert api.module == "pollypm.signal_routing"
    assert api.function == "shared_alert_count"
