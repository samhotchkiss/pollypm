import sys
from types import SimpleNamespace

from pollypm.doctor.install_state import check_pg_connection_for_config


class _FakeCursor:
    def __init__(self, *, vector_installed: bool = True) -> None:
        self.query = ""
        self.vector_installed = vector_installed

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def execute(self, query: str) -> None:
        self.query = query

    def fetchone(self):
        if "server_version_num" in self.query:
            return (160000,)
        if "pg_extension" in self.query:
            return (1,) if self.vector_installed else None
        return (1,)


class _FakeConnection:
    def __init__(self, *, vector_installed: bool = True) -> None:
        self.vector_installed = vector_installed

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(vector_installed=self.vector_installed)


def test_pg_connection_for_config_probes_first_run_default_with_timeout(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def connect(dsn: str, *, connect_timeout: int):
        captured["dsn"] = dsn
        captured["connect_timeout"] = connect_timeout
        return _FakeConnection()

    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=connect))
    monkeypatch.setenv("POLLYPM_PG_DSN", "postgresql://localhost:5432/pollypm_test")

    result = check_pg_connection_for_config(None, connect_timeout=2)

    assert result.passed is True
    assert result.data["dsn"] == "postgresql://localhost:5432/pollypm_test"
    assert captured == {
        "dsn": "postgresql://localhost:5432/pollypm_test",
        "connect_timeout": 2,
    }


def test_pg_connection_for_config_skips_non_postgres_backend() -> None:
    config = SimpleNamespace(storage=SimpleNamespace(backend="sqlite"))

    result = check_pg_connection_for_config(config, connect_timeout=2)

    assert result.passed is True
    assert result.skipped is True
