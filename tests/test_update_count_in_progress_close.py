"""Regression test for #1377 — ``count_in_progress_tasks`` must close
the SQLiteWorkService it opens.

The previous implementation opened the service but never closed it,
relying on process exit to reap the SQLite connection. Process exit
does that for ``pm update``'s one-shot CLI, but the leaky pattern
would bite any future caller that loops.

Mirrors the ``with`` / ``.close()`` pattern from PR #1069 / PR #1381.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from pollypm import update as update_mod


def _install_fake_modules(
    monkeypatch: pytest.MonkeyPatch, db_path: Path, fake_svc_cls: type,
) -> None:
    """Stub the three imports inside ``count_in_progress_tasks`` so the
    test never touches a real SQLite file or config."""
    fake_resolver = types.ModuleType("pollypm.work.db_resolver")
    fake_resolver.resolve_work_db_path = lambda: db_path  # type: ignore[attr-defined]

    fake_service = types.ModuleType("pollypm.work.sqlite_service")
    fake_service.SQLiteWorkService = fake_svc_cls  # type: ignore[attr-defined]

    fake_config = types.ModuleType("pollypm.config")
    fake_config.DEFAULT_CONFIG_PATH = Path("/tmp/ignored")  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "pollypm.work.db_resolver", fake_resolver)
    monkeypatch.setitem(sys.modules, "pollypm.work.sqlite_service", fake_service)
    monkeypatch.setitem(sys.modules, "pollypm.config", fake_config)


def test_count_in_progress_tasks_closes_workservice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The work-service connection is closed via the context manager.

    Before #1377 the service was instantiated but never closed; this
    asserts both ``__enter__`` and ``__exit__`` fire.
    """
    db_path = tmp_path / "state.db"
    db_path.write_bytes(b"")

    events: list[str] = []

    class _FakeSvc:
        def __init__(self, *, db_path: Path) -> None:
            events.append("init")

        def __enter__(self) -> "_FakeSvc":
            events.append("enter")
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            events.append("exit")

        def list_tasks(self, *, work_status: str = "") -> list:
            events.append(f"list:{work_status}")
            return [object(), object(), object()]

        def close(self) -> None:  # pragma: no cover — unused by the fix
            events.append("close")

    _install_fake_modules(monkeypatch, db_path, _FakeSvc)

    count = update_mod.count_in_progress_tasks()

    assert count == 3
    # Order matters: init -> enter -> list -> exit. The exit event is
    # the load-bearing assertion — the previous implementation skipped
    # it entirely.
    assert events == ["init", "enter", "list:in_progress", "exit"]


def test_count_in_progress_tasks_closes_workservice_on_query_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even when ``list_tasks`` raises, the context manager fires
    ``__exit__`` so the connection is released."""
    db_path = tmp_path / "state.db"
    db_path.write_bytes(b"")

    events: list[str] = []

    class _FakeSvc:
        def __init__(self, *, db_path: Path) -> None:
            events.append("init")

        def __enter__(self) -> "_FakeSvc":
            events.append("enter")
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            events.append("exit")

        def list_tasks(self, *, work_status: str = "") -> list:
            raise RuntimeError("simulated DB failure")

    _install_fake_modules(monkeypatch, db_path, _FakeSvc)

    # The function is best-effort; a query failure returns 0 rather
    # than raising.
    assert update_mod.count_in_progress_tasks() == 0
    # exit must still fire — the leak this test guards against is
    # exactly the missing cleanup-on-error path.
    assert "enter" in events
    assert "exit" in events
