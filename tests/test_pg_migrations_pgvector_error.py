"""Tests for the pgvector-missing friendly error (issue #1750).

A brand-new Postgres install does not have the ``vector`` extension on
disk; ``CREATE EXTENSION vector`` then fails with a low-signal psycopg
error ("could not open extension control file..."). #1750 wraps that
in :class:`PgVectorExtensionMissing` with a copy-paste install hint
(brew install pgvector + restart + retry).

These tests use a fake connection so they run without a real pg, but
exercise the exact ``_ensure_bookkeeping`` codepath the migration
applier hits.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from pollypm.storage.pg_migrations import (
    PgVectorExtensionMissing,
    _ensure_bookkeeping,
)


class _FailingCursor:
    """Cursor whose ``execute`` raises on the first call (the EXTENSION DDL)."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def __enter__(self) -> "_FailingCursor":
        return self

    def __exit__(self, *exc_info) -> None:  # noqa: ANN001
        return None

    def execute(self, sql: str, *params) -> None:  # noqa: ARG002
        raise self._exc


class _FakeConn:
    """Minimal psycopg-shaped conn that hands out a failing cursor."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.rolled_back = False
        self.committed = False

    @contextmanager
    def cursor(self):
        yield _FailingCursor(self._exc)

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True


def test_create_extension_failure_raises_friendly_error() -> None:
    """``CREATE EXTENSION vector`` failure surfaces with the install hint."""
    underlying = RuntimeError(
        'extension "vector" is not available; '
        'could not open extension control file'
    )
    conn = _FakeConn(underlying)

    with pytest.raises(PgVectorExtensionMissing) as exc_info:
        _ensure_bookkeeping(conn)

    message = str(exc_info.value)
    # Operator-facing instructions are present.
    assert "brew install pgvector" in message
    assert "pgvector/pgvector" in message
    # Underlying error is surfaced for debugging without burying the hint.
    assert "could not open extension control file" in message
    # The conn must have been rolled back before re-raising.
    assert conn.rolled_back is True


def test_pgvector_extension_missing_is_runtime_error_subclass() -> None:
    """The friendly error is a RuntimeError so callers catching that still work."""
    assert issubclass(PgVectorExtensionMissing, RuntimeError)
