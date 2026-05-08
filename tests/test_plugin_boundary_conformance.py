"""Plugin boundary + protocol conformance tests (#890).

Covers the audit gap (`docs/launch-issue-audit-2026-04-27.md`
§9): no built-in plugin may import another plugin's private
helpers, and built-in providers / services must conform to
their declared protocols.

The companion :mod:`tests.test_import_boundary` already guards
the Supervisor private-attribute reach-through and StateStore
``_conn`` rules. This file is the *cross-plugin* layer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pollypm.plugin_boundaries import (
    BoundaryException,
    CrossPluginPrivateImport,
    ProtocolMismatch,
    assert_implements_protocol,
    discover_builtin_plugins,
    scan_plugin_for_private_imports,
)


REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Plugin discovery
# ---------------------------------------------------------------------------


def test_discover_builtin_plugins_returns_known_packages() -> None:
    """Sanity: discovery finds the high-traffic plugins that
    exist today. A passing test means the discovery logic
    survived a plugin rename."""
    plugins = discover_builtin_plugins(REPO_ROOT)
    names = {p.name for p in plugins}
    assert "advisor" in names
    assert "core_recurring" in names
    assert "task_assignment_notify" in names


def test_discover_builtin_plugins_skips_pycache() -> None:
    plugins = discover_builtin_plugins(REPO_ROOT)
    assert all(p.name != "__pycache__" for p in plugins)


def test_discover_builtin_plugins_skips_underscore_dirs() -> None:
    """Top-level directories starting with `_` are package
    internals, not plugins."""
    plugins = discover_builtin_plugins(REPO_ROOT)
    assert all(not p.name.startswith("_") for p in plugins)


# ---------------------------------------------------------------------------
# Cross-plugin private-import contract
# ---------------------------------------------------------------------------


def test_no_cross_plugin_private_imports() -> None:
    """The headline contract: no built-in plugin imports another
    plugin's underscored helper unless explicitly allowlisted.

    The 2026-04-26 cleanup retired every previously-known
    instance. Adding a new violation requires an explicit
    BoundaryException with owner / reason / removal condition."""
    plugins = discover_builtin_plugins(REPO_ROOT)
    violations: list[CrossPluginPrivateImport] = []
    for plugin in plugins:
        violations.extend(scan_plugin_for_private_imports(
            plugin, all_plugins=plugins,
        ))
    assert violations == [], "\n".join(v.summary for v in violations)


def test_transition_query_plugin_handlers_do_not_open_sqlite_directly() -> None:
    """Issue #1376: plugin handlers route work-transition reads via storage."""
    rels = (
        "src/pollypm/plugins_builtin/activity_feed/handlers/event_projector.py",
        "src/pollypm/plugins_builtin/advisor/handlers/detect_changes.py",
    )
    offenders: list[str] = []
    for rel in rels:
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        if "sqlite3.connect" in text or "import sqlite3" in text:
            offenders.append(rel)
    assert offenders == []


def test_scanner_recognizes_private_symbol() -> None:
    """Scanner detects ``from X import _foo`` correctly."""
    # Synthetic test using the public `_split_imports` indirectly.
    from pollypm.plugin_boundaries import _split_imports

    parts = _split_imports("foo, _bar, baz as renamed")
    assert "foo" in parts
    assert "_bar" in parts
    assert "renamed" not in parts  # `as` keeps the original name
    assert "baz" in parts


def test_scanner_handles_parenthesised_imports() -> None:
    from pollypm.plugin_boundaries import _split_imports

    parts = _split_imports("(foo,\n    _bar,\n    baz,\n)")
    assert "_bar" in parts


# ---------------------------------------------------------------------------
# Allowlist machinery
# ---------------------------------------------------------------------------


def test_allowlist_is_empty_today() -> None:
    """The 2026-04-26 cleanup retired every prior exception.
    A new entry must come with owner / reason / removal."""
    from pollypm.plugin_boundaries import BOUNDARY_EXCEPTIONS
    # Keeping the exception count at 0 is the launch-hardening
    # invariant — any future entry that lands here should be
    # tracked under a follow-up issue with a removal condition.
    assert len(BOUNDARY_EXCEPTIONS) == 0


def test_allowlisted_violation_does_not_fail_scan(tmp_path: Path) -> None:
    """When an entry IS on the allowlist, the scanner must skip
    it. Built with a synthetic plugin tree because we don't keep
    real allowlisted violations in the live tree."""
    from pollypm.plugin_boundaries import (
        PluginPackage,
        scan_plugin_for_private_imports,
    )
    # Synthesize two plugin dirs with a private import between them.
    p_a = tmp_path / "src" / "pollypm" / "plugins_builtin" / "alpha"
    p_b = tmp_path / "src" / "pollypm" / "plugins_builtin" / "beta"
    (p_a / "core").mkdir(parents=True)
    (p_b / "core").mkdir(parents=True)
    importer = p_a / "core" / "x.py"
    importer.write_text(
        "from pollypm.plugins_builtin.beta.api import _hidden\n"
    )

    plugin_a = PluginPackage(
        name="alpha", root=p_a, module_path="pollypm.plugins_builtin.alpha"
    )
    plugin_b = PluginPackage(
        name="beta", root=p_b, module_path="pollypm.plugins_builtin.beta"
    )

    # Without an allowlist entry, the scan should report it.
    violations = scan_plugin_for_private_imports(
        plugin_a, all_plugins=(plugin_a, plugin_b)
    )
    assert any(v.target_symbol == "_hidden" for v in violations)

    # With the entry on the allowlist, it must be silenced.
    allow = (
        BoundaryException(
            importer=str(importer),
            target_module="pollypm.plugins_builtin.beta.api",
            target_symbol="_hidden",
            owner="cleanup-team",
            reason="hand-off in progress",
            removal_condition="follow-up issue closed",
        ),
    )
    silenced = scan_plugin_for_private_imports(
        plugin_a, all_plugins=(plugin_a, plugin_b), allowlist=allow
    )
    assert silenced == ()


# ---------------------------------------------------------------------------
# Protocol conformance — built-in providers and services
# ---------------------------------------------------------------------------


def test_assert_implements_protocol_passes_on_clean_impl() -> None:
    """A clean impl produces no mismatches."""

    class P:
        def hello(self, name: str) -> str: ...
        def goodbye(self) -> None: ...

    class Impl:
        def hello(self, name: str) -> str:
            return f"hi {name}"

        def goodbye(self) -> None:
            return None

    assert assert_implements_protocol(protocol=P, impl=Impl) == ()


def test_assert_implements_protocol_flags_missing_method() -> None:
    """Impl missing a protocol method must be reported."""

    class P:
        def hello(self) -> None: ...
        def goodbye(self) -> None: ...

    class Impl:
        def hello(self) -> None: ...

    out = assert_implements_protocol(protocol=P, impl=Impl)
    assert any(m.method == "goodbye" for m in out)


def test_assert_implements_protocol_flags_missing_parameter() -> None:
    """The audit (#802) cites the case: impl drops a parameter
    the protocol declares. The check must catch it."""

    class P:
        def create(self, *, title: str, owner: str) -> None: ...

    class Impl:
        def create(self, *, title: str) -> None: ...

    out = assert_implements_protocol(protocol=P, impl=Impl)
    assert any("owner" in m.detail for m in out)


def test_assert_implements_protocol_allows_impl_extra_parameter() -> None:
    """Impl may add parameters; only missing ones are violations."""

    class P:
        def create(self, *, title: str) -> None: ...

    class Impl:
        def create(self, *, title: str, extra: str = "") -> None: ...

    assert assert_implements_protocol(protocol=P, impl=Impl) == ()


def test_protocol_mismatch_summary_is_human_readable() -> None:
    pm = ProtocolMismatch(
        protocol_name="MyProto",
        impl_name="MyImpl",
        method="do_thing",
        detail="missing param",
    )
    s = pm.summary
    assert "MyProto" in s and "MyImpl" in s and "do_thing" in s


# ---------------------------------------------------------------------------
# Real protocol conformance — work service
# ---------------------------------------------------------------------------


def test_work_service_protocol_real_implementations_conform() -> None:
    """Both built-in WorkService implementations
    (SQLiteWorkService, MockWorkService) must conform to the
    WorkService protocol on the methods the cockpit relies on.

    A failing test means the audit's #802 / #803 / #804 / #805
    pattern — protocols promising methods not delivered — has
    recurred."""
    try:
        from pollypm.work.service import WorkService
        from pollypm.work.sqlite_service import SQLiteWorkService
        from pollypm.work.mock_service import MockWorkService
    except ImportError:
        pytest.skip("work-service modules not importable")

    sqlite_mismatches = assert_implements_protocol(
        protocol=WorkService, impl=SQLiteWorkService
    )
    mock_mismatches = assert_implements_protocol(
        protocol=WorkService, impl=MockWorkService
    )

    # The existing test_work_service_protocol_conformance test
    # already runs deeper checks; this is the boundary smoke
    # signal that a method existence regression cannot slip in.
    public_methods = {
        m for m in dir(WorkService) if not m.startswith("_")
    }
    assert public_methods, "WorkService protocol exposes no public surface"

    # Filter to *missing methods*; parameter checks are the deeper
    # test_work_service_protocol_conformance suite's job.
    missing_methods = [
        m for m in sqlite_mismatches
        if m.method != "_unused" and "does not declare" in m.detail
    ]
    assert missing_methods == [], (
        "SQLiteWorkService missing protocol methods: "
        + ", ".join(m.method for m in missing_methods)
    )
    missing_methods_mock = [
        m for m in mock_mismatches
        if m.method != "_unused" and "does not declare" in m.detail
    ]
    assert missing_methods_mock == [], (
        "MockWorkService missing protocol methods: "
        + ", ".join(m.method for m in missing_methods_mock)
    )
