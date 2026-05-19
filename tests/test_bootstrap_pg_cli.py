"""Tests for ``pm bootstrap-pg`` (issue #1747).

The bootstrap command is a side-effectful orchestrator over brew, psql,
createdb, and apply_migrations. These tests exercise the safe surfaces:

* ``--help`` renders (CLI is registered, no import errors).
* ``--dry-run`` is the default — invoking the command without ``--yes``
  prints the plan and exits 0 without invoking any subprocess.
* The plan respects platform detection (non-macOS falls through to a
  hint-only step instead of trying to ``brew install``).
* ``--yes`` without ``--dry-run`` still requires the explicit flag —
  the contract is "destructive only when the operator opted in".
* The ``[storage]`` block writer is idempotent.

The actual destructive path (`brew install`, `createdb`, etc.) is
covered by manual smoke + the operator runbook; mocking subprocess at
that depth would be brittle and provides no real coverage of the
contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from pollypm.cli import app
from pollypm.cli_features import storage as storage_cli


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_bootstrap_pg_help_renders(runner: CliRunner) -> None:
    """``pm bootstrap-pg --help`` exits 0 and mentions the dry-run default."""
    result = runner.invoke(app, ["bootstrap-pg", "--help"])
    assert result.exit_code == 0, result.output
    assert "Guided one-shot Postgres install" in result.output
    assert "DEFAULT IS DRY-RUN" in result.output
    assert "--yes" in result.output
    assert "--db" in result.output
    # Examples block per the help-examples contract.
    assert "Examples:" in result.output


def test_bootstrap_pg_dry_run_prints_plan_and_runs_no_commands(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default invocation is a dry-run — no subprocess calls allowed."""
    called: list[list[str]] = []

    def _boom(*args, **kwargs):  # noqa: ARG001 — any call is a bug
        called.append(list(args[0]) if args else [])
        raise AssertionError(
            f"bootstrap-pg dry-run invoked subprocess: args={args!r}"
        )

    monkeypatch.setattr(storage_cli.subprocess, "run", _boom)
    # Force the introspection probes to no-op so we don't shell out to
    # real brew / psql while planning.
    monkeypatch.setattr(storage_cli, "_brew_pkg_installed", lambda pkg: False)
    monkeypatch.setattr(storage_cli, "_brew_service_running", lambda svc: False)
    monkeypatch.setattr(storage_cli, "_pg_db_exists", lambda db: False)

    result = runner.invoke(app, ["bootstrap-pg"])
    assert result.exit_code == 0, result.output
    assert "Bootstrap plan:" in result.output
    assert "Dry-run mode" in result.output
    assert called == []


def test_bootstrap_pg_dry_run_flag_overrides_yes(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--yes --dry-run`` still runs in dry-run mode (kill-switch precedence)."""
    monkeypatch.setattr(
        storage_cli.subprocess,
        "run",
        lambda *a, **k: pytest.fail("--dry-run should suppress execution"),
    )
    monkeypatch.setattr(storage_cli, "_brew_pkg_installed", lambda pkg: True)
    monkeypatch.setattr(storage_cli, "_brew_service_running", lambda svc: True)
    monkeypatch.setattr(storage_cli, "_pg_db_exists", lambda db: True)

    result = runner.invoke(app, ["bootstrap-pg", "--yes", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "Dry-run mode" in result.output


def test_bootstrap_pg_plan_marks_existing_packages_as_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Already-installed brew packages should show up as skipped steps."""
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr(storage_cli, "_which", lambda b: f"/usr/local/bin/{b}")
    monkeypatch.setattr(storage_cli, "_brew_pkg_installed", lambda pkg: True)
    monkeypatch.setattr(storage_cli, "_brew_service_running", lambda svc: True)
    monkeypatch.setattr(storage_cli, "_pg_db_exists", lambda db: True)

    steps = storage_cli._plan_steps(pg_version="postgresql@17", db_name="pollypm")
    skipped_labels = [s.label for s in steps if s.skipped]
    # The three brew steps + createdb should all be marked skipped.
    assert any("postgresql@17" in lbl for lbl in skipped_labels)
    assert any("pgvector" in lbl for lbl in skipped_labels)
    assert any("services start" in lbl for lbl in skipped_labels)
    assert any("createdb" in lbl for lbl in skipped_labels)


def test_bootstrap_pg_plan_non_macos_falls_back_to_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Linux / other platforms get a single informational step + a hint."""
    monkeypatch.setattr("platform.system", lambda: "Linux")
    monkeypatch.setattr(storage_cli, "_which", lambda b: None)
    monkeypatch.setattr(storage_cli, "_pg_db_exists", lambda db: False)

    steps = storage_cli._plan_steps(pg_version="postgresql@17", db_name="pollypm")
    install_step = steps[0]
    assert install_step.skipped
    assert "Non-Homebrew platform" in install_step.skip_reason
    # The brew steps must NOT appear individually on non-mac.
    brew_step_labels = [s.label for s in steps if "brew" in s.label.lower()]
    assert brew_step_labels == []


def test_write_storage_block_is_idempotent(tmp_path: Path) -> None:
    """Re-running bootstrap on an already-configured pollypm.toml is a no-op."""
    cfg = tmp_path / "pollypm.toml"
    cfg.write_text("[project]\nname = 'demo'\n", encoding="utf-8")

    wrote_first = storage_cli._write_storage_block(cfg, dsn="postgresql://x/y")
    assert wrote_first is True
    text_after_first = cfg.read_text(encoding="utf-8")
    assert "[storage]" in text_after_first
    assert 'url = "postgresql://x/y"' in text_after_first

    wrote_second = storage_cli._write_storage_block(cfg, dsn="postgresql://x/y")
    assert wrote_second is False
    # No duplicate [storage] sections.
    assert text_after_first == cfg.read_text(encoding="utf-8")


def test_write_storage_block_creates_missing_config(tmp_path: Path) -> None:
    """First run on a fresh install creates ``pollypm.toml`` with [storage] (#1888).

    The earlier behavior bailed and returned False when the file did
    not exist — leaving a brand-new ``pm bootstrap-pg`` operator with
    no DSN written. The fix scaffolds a minimal config so the runtime
    has a backend on next launch.
    """
    cfg = tmp_path / "subdir" / "pollypm.toml"
    assert not cfg.exists()

    wrote = storage_cli._write_storage_block(cfg, dsn="postgresql://x/y")
    assert wrote is True
    assert cfg.exists()
    text = cfg.read_text(encoding="utf-8")
    assert "[storage]" in text
    assert 'url = "postgresql://x/y"' in text

    # Re-running on the now-existing file is idempotent.
    wrote_again = storage_cli._write_storage_block(cfg, dsn="postgresql://x/y")
    assert wrote_again is False
    # No duplicate sections.
    assert text == cfg.read_text(encoding="utf-8")


def test_pg_db_exists_uses_parameter_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_pg_db_exists` must not f-string the db name into SQL (#1890).

    Regression guard: a hostile ``--db`` value containing a single
    quote / SQL fragment should not be reflected back as part of the
    query text. The probe goes through psycopg with a ``%s`` placeholder.
    """
    captured: dict[str, object] = {}

    class _FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):  # noqa: D401
            return False

        def execute(self, sql: str, params: tuple[object, ...]) -> None:
            captured["sql"] = sql
            captured["params"] = params

        def fetchone(self):
            return (1,)

    class _FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):  # noqa: D401
            return False

        def cursor(self):
            return _FakeCursor()

    class _FakePsycopg:
        @staticmethod
        def connect(dsn: str, **kwargs):  # noqa: ARG004
            captured["dsn"] = dsn
            return _FakeConn()

    monkeypatch.setitem(__import__("sys").modules, "psycopg", _FakePsycopg)

    hostile = "pollypm'; DROP TABLE foo; --"
    assert storage_cli._pg_db_exists(hostile) is True

    # The SQL text must be the parameterized form — the db name must
    # NOT appear interpolated in the SQL.
    sql = captured["sql"]
    assert "%s" in sql
    assert hostile not in sql
    # The hostile value must travel as a bound parameter, not SQL.
    assert captured["params"] == (hostile,)


def test_bootstrap_pg_vector_step_is_python_not_psql_subprocess() -> None:
    """The pgvector pre-step is a Python step routed through the friendly wrapper (#1889).

    Regression guard: the previous implementation shelled out to ``psql
    -c "CREATE EXTENSION ..."`` which bypassed
    ``PgVectorExtensionMissing``. After the fix, the planner emits a
    pure-Python step (``cmd is None``) so the executor can wrap the
    DDL with the same friendly error.
    """
    steps = storage_cli._plan_steps(pg_version="postgresql@17", db_name="pollypm")
    vector_steps = [s for s in steps if s.label.startswith("CREATE EXTENSION")]
    assert len(vector_steps) == 1
    assert vector_steps[0].cmd is None, (
        "pgvector step must be a Python step so the friendly "
        "PgVectorExtensionMissing wrapper fires."
    )


def test_create_vector_extension_wraps_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_create_vector_extension` re-raises connection errors with the install hint (#1889)."""
    from pollypm.storage.pg_migrations import PgVectorExtensionMissing

    class _ExplodingPsycopg:
        @staticmethod
        def connect(dsn: str, **kwargs):  # noqa: ARG004
            raise RuntimeError("pgvector .so not on disk")

    monkeypatch.setitem(__import__("sys").modules, "psycopg", _ExplodingPsycopg)

    with pytest.raises(PgVectorExtensionMissing) as excinfo:
        storage_cli._create_vector_extension("pollypm")
    msg = str(excinfo.value)
    assert "brew install pgvector" in msg
    assert "vector" in msg
