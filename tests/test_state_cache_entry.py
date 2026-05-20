"""Tests for :mod:`pollypm.state_cache.entry`.

The entry is the contract between refreshers (writers) and rail /
dashboard call sites (readers). Pin the frozen-dataclass invariants
so future edits can't accidentally make payload fields mutable —
mutability would break the lock-free read story described in
``docs/design/move-a-state-cache.md`` §3.6.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from pollypm.state_cache.entry import ProjectStateCacheEntry, empty_entry


def test_entry_is_frozen() -> None:
    entry = empty_entry("alpha")
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.glyph = "X"  # type: ignore[misc]


def test_entry_equality_by_field_value() -> None:
    a = ProjectStateCacheEntry(project_key="p", project_path=Path("/tmp/p"))
    b = ProjectStateCacheEntry(project_key="p", project_path=Path("/tmp/p"))
    c = ProjectStateCacheEntry(project_key="q", project_path=Path("/tmp/p"))
    assert a == b
    assert a != c


def test_entry_replace_creates_new_instance() -> None:
    a = empty_entry("alpha")
    b = dataclasses.replace(a, version=5)
    # Replace must construct a fresh frozen instance, not mutate.
    assert a.version == 0
    assert b.version == 5
    assert a is not b


def test_empty_entry_carries_project_metadata() -> None:
    entry = empty_entry("alpha", Path("/tmp/alpha"))
    assert entry.project_key == "alpha"
    assert entry.project_path == Path("/tmp/alpha")
    # All payload counts default to "no data yet" — readers must
    # never see torn data.
    assert entry.awaits_user_count == 0
    assert entry.awaits_user_items == ()
    assert entry.task_status_counts == {}
    assert entry.live_worker_sessions == ()


def test_empty_entry_default_path_is_empty() -> None:
    entry = empty_entry("alpha")
    # ``Path("")`` is the documented sentinel for "path not known".
    assert entry.project_path == Path("")


def test_entry_slots_prevents_attr_attach() -> None:
    """``slots=True`` guards against ad-hoc mutation via attribute set."""

    entry = empty_entry("alpha")
    # On Python 3.13 the frozen+slots combination raises before slots
    # checks kick in, so the concrete exception type is
    # ``FrozenInstanceError``; older / future runtimes may surface
    # ``AttributeError`` (slots) or ``TypeError`` (descriptor) instead.
    # Pin the behaviour at the union, not the exact class.
    with pytest.raises(
        (dataclasses.FrozenInstanceError, AttributeError, TypeError),
    ):
        entry.new_field = "nope"  # type: ignore[attr-defined]


def test_entry_payload_collections_are_immutable_shapes() -> None:
    """Tuples + frozensets, not lists / sets — readers must not mutate."""

    entry = empty_entry("alpha")
    assert isinstance(entry.awaits_user_items, tuple)
    assert isinstance(entry.live_worker_sessions, tuple)
    assert isinstance(entry.db_paths, tuple)
    assert isinstance(entry.on_hold_task_ids, frozenset)
    assert isinstance(entry.review_task_ids, frozenset)


def test_entry_versioning_fields_default_zero() -> None:
    entry = empty_entry("alpha")
    assert entry.version == 0
    # ``computed_at`` is monotonic time at construction, so non-negative.
    assert entry.computed_at >= 0.0
