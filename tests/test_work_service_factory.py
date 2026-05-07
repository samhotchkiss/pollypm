"""Contract tests for ``pollypm.work.create_work_service`` (#1369).

The factory is the canonical construction surface for the work service
outside ``pollypm.work.*``. These tests pin down the public contract:

1. ``create_work_service`` is exported from ``pollypm.work``.
2. With no ``db_path``, it routes through
   :func:`pollypm.work.db_resolver.resolve_work_db_path` to find the
   canonical DB.
3. With an explicit ``db_path``, the resolver is *not* consulted (the
   escape valve for legacy / migration / test paths).
4. The constructed service is a real ``SQLiteWorkService`` with an
   open connection — basic CRUD round-trips work.
5. ``project_path`` and ``config`` are forwarded as documented.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


def test_factory_is_exported_from_work_package() -> None:
    """``from pollypm.work import create_work_service`` must work."""
    from pollypm.work import create_work_service

    assert callable(create_work_service)


def test_factory_with_explicit_db_path_skips_resolver(tmp_path: Path) -> None:
    """An explicit ``db_path`` is the documented escape valve."""
    from pollypm.work import create_work_service

    db_path = tmp_path / "work.db"
    with patch("pollypm.work.db_resolver.resolve_work_db_path") as mock_resolve:
        svc = create_work_service(db_path=db_path)
        try:
            # Resolver MUST NOT be consulted when caller passes db_path.
            mock_resolve.assert_not_called()
        finally:
            svc.close()
    assert db_path.exists()


def test_factory_without_db_path_calls_resolver(tmp_path: Path) -> None:
    """When ``db_path`` is omitted, the canonical resolver runs."""
    from pollypm.work import create_work_service

    canonical = tmp_path / "canonical.db"
    with patch(
        "pollypm.work.db_resolver.resolve_work_db_path", return_value=canonical,
    ) as mock_resolve:
        svc = create_work_service()
        try:
            mock_resolve.assert_called_once()
        finally:
            svc.close()
    assert canonical.exists()


def test_factory_passes_config_and_project_key_to_resolver(tmp_path: Path) -> None:
    """``config`` and ``project_key`` must reach the resolver verbatim."""
    from pollypm.work import create_work_service

    canonical = tmp_path / "canonical.db"
    sentinel_config = object()
    with patch(
        "pollypm.work.db_resolver.resolve_work_db_path", return_value=canonical,
    ) as mock_resolve:
        svc = create_work_service(
            config=sentinel_config, project_key="my-proj",  # type: ignore[arg-type]
        )
        svc.close()
    kwargs = mock_resolve.call_args.kwargs
    assert kwargs["config"] is sentinel_config
    assert kwargs["project"] == "my-proj"


def test_factory_returns_real_sqlite_work_service(tmp_path: Path) -> None:
    """Constructed service must round-trip a basic create/get cycle."""
    from pollypm.work import create_work_service
    from pollypm.work.sqlite_service import SQLiteWorkService

    db_path = tmp_path / "work.db"
    svc = create_work_service(db_path=db_path, project_path=tmp_path)
    try:
        assert isinstance(svc, SQLiteWorkService)
        task = svc.create(
            title="hello",
            description="from factory",
            type="task",
            project="proj",
            flow_template="standard",
            roles={"worker": "agent-1", "reviewer": "agent-2"},
            priority="normal",
            created_by="tester",
        )
        got = svc.get(task.task_id)
        assert got.title == "hello"
    finally:
        svc.close()


def test_factory_accepts_str_or_path_for_db_path(tmp_path: Path) -> None:
    """Both ``str`` and ``Path`` are accepted for ``db_path``."""
    from pollypm.work import create_work_service

    db_path = tmp_path / "work.db"
    svc = create_work_service(db_path=str(db_path))
    try:
        # Reaching into the private attribute is fine for a contract
        # test — we want to confirm the path was normalised to ``Path``.
        assert isinstance(svc._db_path, Path)
    finally:
        svc.close()


def test_factory_supports_context_manager(tmp_path: Path) -> None:
    """``with create_work_service(...) as svc:`` is the canonical pattern."""
    from pollypm.work import create_work_service

    db_path = tmp_path / "work.db"
    with create_work_service(db_path=db_path) as svc:
        # Service is usable inside the ``with`` block.
        tasks = svc.list_tasks()
        assert tasks == []


def test_no_callsites_outside_work_construct_sqlite_work_service_directly() -> None:
    """Boundary check: no module outside ``pollypm.work.*`` should call
    ``SQLiteWorkService(...)`` directly. Test files and the work package
    itself are exempt; ``service_factory=SQLiteWorkService`` (class-as-
    callable injection) is also exempt because no construction happens
    at the callsite.

    This is the regression guard for #1369. Future direct-construction
    callsites added outside the work package will fail this test, which
    is the point: "go through ``create_work_service``" should be the
    only easy path.
    """
    import re

    src_root = Path(__file__).parent.parent / "src" / "pollypm"
    pattern = re.compile(r"\bSQLiteWorkService\s*\(")

    offenders: list[tuple[Path, int, str]] = []
    for py_file in src_root.rglob("*.py"):
        # Skip the work package itself — direct construction is fine
        # there, that's the implementation.
        if "pollypm/work/" in str(py_file).replace("\\", "/"):
            continue
        try:
            text = py_file.read_text(encoding="utf-8")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                # Class-as-callable injection (passing the class
                # reference itself) is allowed: it doesn't construct
                # anything at the callsite.
                if "service_factory=SQLiteWorkService" in line:
                    continue
                offenders.append((py_file, lineno, line.strip()))

    assert not offenders, (
        "Direct SQLiteWorkService(...) construction outside pollypm.work/ — "
        "use pollypm.work.create_work_service instead. Offenders:\n"
        + "\n".join(f"  {p}:{ln}: {src}" for p, ln, src in offenders)
    )


@pytest.mark.parametrize("project_path_input", ["string-path", Path("/tmp/foo")])
def test_factory_normalises_project_path_to_path(
    tmp_path: Path, project_path_input,
) -> None:
    """``project_path`` accepts ``str`` or ``Path`` and normalises to ``Path``."""
    from pollypm.work import create_work_service

    if isinstance(project_path_input, str):
        project_path_input = str(tmp_path / project_path_input)
    db_path = tmp_path / "work.db"
    svc = create_work_service(db_path=db_path, project_path=project_path_input)
    try:
        assert svc._project_path is None or isinstance(svc._project_path, Path)
    finally:
        svc.close()
