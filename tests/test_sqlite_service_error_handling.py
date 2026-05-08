import ast
from pathlib import Path

import pollypm.work.sqlite_service as sqlite_service_module


def test_sqlite_work_service_has_no_silent_broad_exception_passes() -> None:
    source = Path(sqlite_service_module.__file__).read_text()
    tree = ast.parse(source)

    offenders = [
        handler.lineno
        for handler in ast.walk(tree)
        if isinstance(handler, ast.ExceptHandler)
        and isinstance(handler.type, ast.Name)
        and handler.type.id == "Exception"
        and len(handler.body) == 1
        and isinstance(handler.body[0], ast.Pass)
    ]

    assert offenders == []
