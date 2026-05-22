"""Regression test: ``state_cache.refresh_impl`` must not depend on ``StateStore``.

PR #2026 re-review (Codex blocker). An earlier fix replaced the per-project
``Supervisor(load_config(DEFAULT_CONFIG_PATH))`` call with a memoised
``StateStore`` open keyed by ``id(config)``. That re-introduced the legacy
sqlite ``pollypm.storage.state`` dependency on the state-cache hot path —
the exact runtime branch #1737 / #2039 had just removed — and returned
stale rows under the pg-backed heartbeat path because the unified
``heartbeats`` table lives in Postgres post-cutover.

The fix routes ``_heartbeats_for_sessions`` through the pg facade
(``pollypm.storage.pg_heartbeats.latest_heartbeat``). This test exists
solely to make sure no future change quietly reintroduces the legacy
StateStore on the refresher: any source-level import or reference trips
it.

Two complementary assertions:

1. **Source-text grep** — the refresher source must contain neither
   ``StateStore`` nor ``pollypm.storage.state``. Cheap, deterministic,
   catches a lazy local import inside a helper just as well as a top-of-
   file one.
2. **AST scan** — walks every ``Import`` / ``ImportFrom`` node so a
   future hidden import (e.g. inside ``if TYPE_CHECKING:``) also trips.

If either assertion fires, the no-sqlite invariant on
``state_cache.refresh_impl`` has regressed — route the heartbeat
prefetch through the pg facade (or a backend-neutral / bulk reader
that resolves to pg for the active config) instead.
"""

from __future__ import annotations

import ast
from pathlib import Path

REFRESH_IMPL = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "pollypm"
    / "state_cache"
    / "refresh_impl.py"
)


def _source() -> str:
    return REFRESH_IMPL.read_text(encoding="utf-8")


def test_refresh_impl_source_does_not_mention_statestore() -> None:
    """Plain-text guard: the file must contain no ``StateStore`` identifier."""

    source = _source()
    assert REFRESH_IMPL.exists(), f"missing refresher source: {REFRESH_IMPL}"
    assert "StateStore" not in source, (
        "state_cache.refresh_impl mentions StateStore — heartbeat prefetch "
        "must route through the pg facade (pollypm.storage.pg_heartbeats) "
        "instead of reopening the legacy sqlite StateStore. See PR #2026 "
        "re-review."
    )


def test_refresh_impl_source_does_not_import_pollypm_storage_state() -> None:
    """Plain-text guard: the file must not reference ``pollypm.storage.state``."""

    source = _source()
    assert "pollypm.storage.state" not in source, (
        "state_cache.refresh_impl references pollypm.storage.state — the "
        "legacy sqlite StateStore module. Route heartbeat prefetch through "
        "pollypm.storage.pg_heartbeats instead. See PR #2026 re-review."
    )


def test_refresh_impl_ast_has_no_statestore_imports() -> None:
    """AST guard: walk every Import / ImportFrom node — catches hidden imports.

    A source-text grep already covers most cases, but a lazy import
    inside a helper (``from pollypm.storage.state import StateStore``)
    or a deferred ``if TYPE_CHECKING:`` block could in principle slip
    through future edits if the literal string were obfuscated. The AST
    scan asserts the structural property directly.
    """

    tree = ast.parse(_source(), filename=str(REFRESH_IMPL))
    offending: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "pollypm.storage.state" or module.startswith(
                "pollypm.storage.state."
            ):
                offending.append(f"from {module} import ...")
            for alias in node.names:
                if alias.name == "StateStore":
                    offending.append(
                        f"from {module} import {alias.name}"
                    )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "pollypm.storage.state" or alias.name.startswith(
                    "pollypm.storage.state."
                ):
                    offending.append(f"import {alias.name}")
    assert not offending, (
        "state_cache.refresh_impl has forbidden imports: "
        + ", ".join(offending)
        + " — route heartbeat prefetch through pollypm.storage.pg_heartbeats. "
        "See PR #2026 re-review."
    )
