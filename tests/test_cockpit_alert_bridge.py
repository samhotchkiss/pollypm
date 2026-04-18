"""Cockpit alert reader migration (#341) — bridge surfaces legacy alerts.

:class:`AlertNotifier._fetch_alerts` used to open
:class:`pollypm.storage.state.StateStore` and call
:meth:`open_alerts`. Issue #341 migrated it to
:meth:`Store.query_messages_with_legacy_bridge(type='alert',
state='open')` so alerts written via the new ``messages`` table
surface alongside anything still landing in the legacy ``alerts``
table while #349 migrates those writers.

Run with ``HOME=/tmp/pytest-storage-e uv run pytest -x
tests/test_cockpit_alert_bridge.py``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


def _write_minimal_config(project_path: Path, config_path: Path) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "[project]\n"
        f'tmux_session = "pollypm-test"\n'
        f'workspace_root = "{project_path.parent}"\n'
        "\n"
        f'[projects.demo]\n'
        f'key = "demo"\n'
        f'name = "Demo"\n'
        f'path = "{project_path}"\n'
    )


def _ensure_legacy_alerts_table(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_name TEXT NOT NULL,
                alert_type TEXT NOT NULL,
                severity TEXT NOT NULL,
                message TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def cockpit_env(tmp_path: Path, monkeypatch):
    project_path = tmp_path / "demo"
    project_path.mkdir()
    (project_path / ".git").mkdir()
    config_path = tmp_path / "pollypm.toml"
    _write_minimal_config(project_path, config_path)
    # Pin load_config's state_db resolution inside tmp_path so we don't
    # share the HOME-rooted state.db across tests. Point both HOME and
    # XDG_STATE_HOME so every resolver lands in the test sandbox.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))

    # Resolve the actual state_db path the config loader would hand to
    # the Store so our "seed legacy row" step targets the same file.
    from pollypm.config import load_config
    cfg = load_config(config_path)
    state_db = Path(cfg.project.state_db)
    # Clean any stray state left by a prior fixture sharing this HOME
    # (defensive — tmp_path should already be empty).
    if state_db.exists():
        state_db.unlink()
    return {
        "config_path": config_path,
        "project_path": project_path,
        "state_db": state_db,
    }


def _open_notifier(config_path: Path):
    # Build an AlertNotifier bound to a stub App so we can drive
    # _fetch_alerts without spinning up Textual.
    from pollypm.cockpit_ui import AlertNotifier

    class _StubApp:
        def set_interval(self, *args, **kwargs):  # pragma: no cover
            return None

    return AlertNotifier(_StubApp(), config_path=config_path)


class TestFetchAlertsBridge:
    def test_fetch_returns_empty_on_fresh_state_db(self, cockpit_env):
        notifier = _open_notifier(cockpit_env["config_path"])
        assert notifier._fetch_alerts() == []

    def test_legacy_alert_row_surfaces_as_record(self, cockpit_env):
        state_db = cockpit_env["state_db"]
        _ensure_legacy_alerts_table(state_db)
        conn = sqlite3.connect(str(state_db))
        try:
            conn.execute(
                "INSERT INTO alerts (session_name, alert_type, severity, "
                "message, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'open', ?, ?)",
                (
                    "worker-foo",
                    "pane_dead",
                    "warn",
                    "Pane went silent.",
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        notifier = _open_notifier(cockpit_env["config_path"])
        records = notifier._fetch_alerts()
        assert len(records) == 1
        rec = records[0]
        # _AlertLikeRecord mirrors the original AlertRecord surface.
        assert rec.session_name == "worker-foo"
        assert rec.alert_type == "pane_dead"
        assert rec.severity == "warn"
        assert rec.message == "Pane went silent."
        assert rec.status == "open"

    def test_new_messages_alert_surfaces_as_record(self, cockpit_env):
        from pollypm.store import SQLAlchemyStore

        state_db = cockpit_env["state_db"]
        state_db.parent.mkdir(parents=True, exist_ok=True)
        store = SQLAlchemyStore(f"sqlite:///{state_db}")
        try:
            store.upsert_alert(
                session_name="worker-bar",
                alert_type="stuck",
                severity="critical",
                message="Worker hasn't heartbeat in 5m",
            )
        finally:
            store.close()

        notifier = _open_notifier(cockpit_env["config_path"])
        records = notifier._fetch_alerts()
        assert len(records) == 1
        rec = records[0]
        assert rec.session_name == "worker-bar"
        assert rec.alert_type == "stuck"
        assert rec.severity == "critical"
        assert "heartbeat" in rec.message
