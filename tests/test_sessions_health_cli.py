"""Tests for ``pm sessions`` — session-health summary CLI (refs #2012).

Covers status-classification thresholds (healthy / stale / missing /
unknown), the Lever-2 ``TOKEN ok|missing`` column, the ``--json`` line-
oriented output, and the ``--health`` diagnostic columns. The CLI
command is exercised end-to-end via Typer's ``CliRunner`` with the
tmux + heartbeat probes monkey-patched so the test does not depend on
a live tmux server or backend.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer
from typer.testing import CliRunner

from pollypm.cli_features import sessions_health as mod

runner = CliRunner()


def _make_session(
    name: str,
    *,
    role: str = "worker",
    project: str = "demo",
    window: str | None = None,
    auth_token: str = "",
    enabled: bool = True,
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        role=role,
        project=project,
        window_name=window or name,
        auth_token=auth_token,
        enabled=enabled,
    )


def _make_config(
    *, sessions: dict[str, SimpleNamespace], tmux_session: str = "pollypm"
) -> SimpleNamespace:
    return SimpleNamespace(
        project=SimpleNamespace(
            tmux_session=tmux_session,
            logs_dir=Path("/tmp/does-not-exist-test-logs"),
        ),
        sessions=sessions,
    )


def _heartbeat(*, seconds_ago: int) -> SimpleNamespace:
    return SimpleNamespace(
        created_at=(datetime.now(UTC) - timedelta(seconds=seconds_ago)).isoformat()
    )


# ---------------------------------------------------------------------------
# classifier
# ---------------------------------------------------------------------------


class TestClassifier:
    def test_missing_window_dominates_stale_heartbeat(self) -> None:
        # Even when the heartbeat is fresh, a missing window is the
        # urgent finding — report ``missing`` not ``healthy``.
        assert (
            mod._classify_status(window_present=False, age_seconds=5)
            == "missing"
        )

    def test_no_heartbeat_with_live_window_is_unknown(self) -> None:
        assert (
            mod._classify_status(window_present=True, age_seconds=None)
            == "unknown"
        )

    def test_fresh_heartbeat_with_window_is_healthy(self) -> None:
        assert (
            mod._classify_status(window_present=True, age_seconds=30)
            == "healthy"
        )

    def test_threshold_boundary_is_inclusive_healthy(self) -> None:
        # The 5-minute threshold is the boundary; samples AT the
        # threshold count as healthy, only "older than" tips to stale.
        assert (
            mod._classify_status(
                window_present=True, age_seconds=mod._STALE_HEARTBEAT_SECONDS
            )
            == "healthy"
        )

    def test_just_past_threshold_is_stale(self) -> None:
        assert (
            mod._classify_status(
                window_present=True,
                age_seconds=mod._STALE_HEARTBEAT_SECONDS + 1,
            )
            == "stale"
        )

    def test_runtime_auth_failure_overrides_fresh_heartbeat(self) -> None:
        assert (
            mod._classify_status(
                window_present=True,
                age_seconds=5,
                runtime_status="auth_broken",
            )
            == "auth_broken"
        )

    def test_runtime_capacity_failure_overrides_fresh_heartbeat(self) -> None:
        assert (
            mod._classify_status(
                window_present=True,
                age_seconds=5,
                runtime_status="recovering",
                last_failure_type="capacity_exhausted",
            )
            == "capacity_exhausted"
        )

    def test_missing_window_still_wins_over_runtime_failure(self) -> None:
        assert (
            mod._classify_status(
                window_present=False,
                age_seconds=5,
                runtime_status="auth_broken",
            )
            == "missing"
        )

    def test_recovered_runtime_does_not_pin_old_failure(self) -> None:
        assert (
            mod._classify_status(
                window_present=True,
                age_seconds=5,
                runtime_status="healthy",
                last_failure_type="capacity_exhausted",
            )
            == "healthy"
        )


# ---------------------------------------------------------------------------
# humanize_age
# ---------------------------------------------------------------------------


class TestHumanize:
    def test_none_renders_none(self) -> None:
        assert mod._humanize_age(None) == "none"

    def test_unparseable_renders_none(self) -> None:
        assert mod._humanize_age("not-a-timestamp") == "none"

    def test_seconds_format(self) -> None:
        ts = (datetime.now(UTC) - timedelta(seconds=22)).isoformat()
        result = mod._humanize_age(ts)
        assert result.endswith("s ago")

    def test_minutes_format(self) -> None:
        ts = (datetime.now(UTC) - timedelta(minutes=12)).isoformat()
        result = mod._humanize_age(ts)
        assert result == "12m ago"


# ---------------------------------------------------------------------------
# build_row / token classification
# ---------------------------------------------------------------------------


class TestBuildRow:
    def test_token_ok_when_session_has_auth_token(self, monkeypatch) -> None:
        monkeypatch.setattr(mod, "_latest_heartbeat", lambda *_, **__: None)
        monkeypatch.setattr(mod, "_latest_session_runtime", lambda *_, **__: None)
        session = _make_session("worker_demo", auth_token="cafebabe" * 8)
        row = mod._build_row(
            config=_make_config(sessions={"worker_demo": session}),
            session=session,
            storage_session="pollypm-storage-closet",
            windows={},
            health=False,
        )
        assert row["token"] == "ok"

    def test_token_missing_for_legacy_session(self, monkeypatch) -> None:
        monkeypatch.setattr(mod, "_latest_heartbeat", lambda *_, **__: None)
        monkeypatch.setattr(mod, "_latest_session_runtime", lambda *_, **__: None)
        session = _make_session("worker_legacy", auth_token="")
        row = mod._build_row(
            config=_make_config(sessions={"worker_legacy": session}),
            session=session,
            storage_session="pollypm-storage-closet",
            windows={},
            health=False,
        )
        assert row["token"] == "missing"

    def test_window_target_includes_storage_session(self, monkeypatch) -> None:
        monkeypatch.setattr(mod, "_latest_heartbeat", lambda *_, **__: None)
        monkeypatch.setattr(mod, "_latest_session_runtime", lambda *_, **__: None)
        session = _make_session("worker_demo", window="worker-demo")
        row = mod._build_row(
            config=_make_config(sessions={"worker_demo": session}),
            session=session,
            storage_session="pollypm-storage-closet",
            windows={},
            health=False,
        )
        assert row["window"] == "pollypm-storage-closet:worker-demo"


# ---------------------------------------------------------------------------
# latest_heartbeat — pg-only, degrades to None on failure
# ---------------------------------------------------------------------------


class TestLatestHeartbeatPgOnly:
    def test_pg_lookup_failure_degrades_to_unknown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # When the pg heartbeat facade raises (pool unavailable, schema
        # missing, etc.) the CLI must NOT crash — _latest_heartbeat
        # returns None and the row downstream classifies as "unknown".
        def _boom(*_args, **_kwargs):
            raise RuntimeError("pg pool unavailable")

        monkeypatch.setattr(
            "pollypm.storage.pg_heartbeats.latest_heartbeat", _boom
        )

        config = _make_config(sessions={})
        assert mod._latest_heartbeat(config, "worker_demo") is None

        # End-to-end: a failing pg lookup surfaces as ``unknown`` in the
        # CLI output without raising.
        sessions = {
            "worker_demo": _make_session(
                "worker_demo", window="worker-demo", auth_token="t" * 16
            )
        }
        config_with_session = _make_config(sessions=sessions)
        monkeypatch.setattr(
            "pollypm.config.load_config", lambda _path=None: config_with_session
        )
        monkeypatch.setattr(
            mod, "_list_windows", lambda _name: {"worker-demo": _fake_window()}
        )
        monkeypatch.setattr(mod, "_latest_session_runtime", lambda *_, **__: None)

        result = runner.invoke(_build_cli_app(), ["sessions", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output.strip().splitlines()[0])
        assert payload["status"] == "unknown"
        assert payload["last_heartbeat_iso"] is None


# ---------------------------------------------------------------------------
# end-to-end CLI
# ---------------------------------------------------------------------------


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    sessions: dict[str, SimpleNamespace],
    windows: dict[str, SimpleNamespace],
    heartbeats: dict[str, SimpleNamespace | None],
    runtimes: dict[str, SimpleNamespace | None] | None = None,
) -> None:
    config = _make_config(sessions=sessions)
    monkeypatch.setattr(
        "pollypm.config.load_config", lambda _path=None: config
    )
    monkeypatch.setattr(mod, "_list_windows", lambda _name: windows)
    monkeypatch.setattr(
        mod,
        "_latest_heartbeat",
        lambda _config, session_name: heartbeats.get(session_name),
    )
    runtimes = runtimes or {}
    monkeypatch.setattr(
        mod,
        "_latest_session_runtime",
        lambda _config, session_name: runtimes.get(session_name),
    )


def _build_cli_app() -> typer.Typer:
    # Typer collapses a one-command Typer app into a no-subcommand
    # surface, which would make ``runner.invoke(app, ["sessions", ...])``
    # fail with "unexpected extra argument". Register a no-op
    # ``_marker`` command alongside ours so Typer keeps the subcommand
    # dispatch enabled — the behaviour we want for the production
    # ``pollypm.cli.app`` (which mounts dozens of commands).
    app = typer.Typer()
    mod.register_sessions_health_command(app)

    @app.command("_marker")
    def _marker() -> None:  # pragma: no cover - presence-only
        return None

    return app


def _fake_window(
    *, pane_current_command: str = "claude", pane_pid: int | None = 4242
) -> SimpleNamespace:
    return SimpleNamespace(
        pane_current_command=pane_current_command, pane_pid=pane_pid
    )


class TestSessionsHealthCLI:
    def test_text_output_shows_all_status_buckets(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sessions = {
            "architect_samblog": _make_session(
                "architect_samblog",
                role="architect",
                project="samblog",
                window="architect-samblog",
                auth_token="x" * 16,
            ),
            "worker_legacy": _make_session(
                "worker_legacy",
                role="worker",
                project="demo",
                window="worker-legacy",
                auth_token="",
            ),
            "advisor_missing": _make_session(
                "advisor_missing",
                role="advisor",
                project="savethenovel",
                window="advisor-missing",
                auth_token="y" * 16,
            ),
            "operator_unknown": _make_session(
                "operator_unknown",
                role="operator",
                project="pollypm",
                window="operator-unknown",
                auth_token="z" * 16,
            ),
        }
        windows = {
            "architect-samblog": _fake_window(),
            "worker-legacy": _fake_window(),
            # advisor-missing: not in windows -> missing
            "operator-unknown": _fake_window(),
        }
        heartbeats = {
            "architect_samblog": _heartbeat(seconds_ago=22),
            "worker_legacy": _heartbeat(seconds_ago=20 * 60),  # stale
            "advisor_missing": _heartbeat(seconds_ago=10),  # ignored — window missing
            # operator_unknown: no heartbeat
        }
        _install_fakes(
            monkeypatch,
            sessions=sessions,
            windows=windows,
            heartbeats=heartbeats,
        )

        result = runner.invoke(_build_cli_app(), ["sessions"])
        assert result.exit_code == 0, result.output

        # Header + one row per session.
        assert "NAME" in result.output and "STATUS" in result.output
        assert "architect_samblog" in result.output
        assert "worker_legacy" in result.output
        assert "advisor_missing" in result.output
        assert "operator_unknown" in result.output

        # Status classification.
        lines = {
            line.split()[0]: line
            for line in result.output.splitlines()
            if line and not line.startswith("NAME")
        }
        assert "healthy" in lines["architect_samblog"]
        assert "stale" in lines["worker_legacy"]
        assert "missing" in lines["advisor_missing"]
        assert "unknown" in lines["operator_unknown"]

        # Token classification — only worker_legacy is missing the token.
        assert "missing" in lines["worker_legacy"].split()[-1] or lines[
            "worker_legacy"
        ].endswith("missing")
        assert lines["architect_samblog"].rstrip().endswith("ok")

    def test_json_output_emits_one_object_per_line(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sessions = {
            "worker_demo": _make_session(
                "worker_demo", window="worker-demo", auth_token="t" * 16
            ),
            "worker_other": _make_session(
                "worker_other", window="worker-other", auth_token=""
            ),
        }
        _install_fakes(
            monkeypatch,
            sessions=sessions,
            windows={"worker-demo": _fake_window()},
            heartbeats={"worker_demo": _heartbeat(seconds_ago=5)},
        )

        result = runner.invoke(_build_cli_app(), ["sessions", "--json"])
        assert result.exit_code == 0, result.output
        lines = [line for line in result.output.splitlines() if line.strip()]
        payloads = [json.loads(line) for line in lines]
        assert {p["name"] for p in payloads} == {"worker_demo", "worker_other"}
        # No table header in JSON mode.
        assert "NAME" not in result.output
        # Each payload carries the required keys.
        for payload in payloads:
            assert {
                "name",
                "role",
                "project",
                "window",
                "status",
                "last_heartbeat_age",
                "token",
            } <= payload.keys()

    def test_json_output_surfaces_runtime_failure_status(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _make_session(
            "operator",
            role="operator-pm",
            window="pm-operator",
            auth_token="t" * 16,
        )
        _install_fakes(
            monkeypatch,
            sessions={"operator": session},
            windows={"pm-operator": _fake_window()},
            heartbeats={"operator": _heartbeat(seconds_ago=5)},
            runtimes={
                "operator": SimpleNamespace(
                    status="auth_broken",
                    last_failure_type="auth_broken",
                )
            },
        )

        result = runner.invoke(_build_cli_app(), ["sessions", "--json"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output.strip().splitlines()[0])
        assert payload["status"] == "auth_broken"

    def test_health_flag_adds_diagnostic_columns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sessions = {
            "worker_demo": _make_session(
                "worker_demo", window="worker-demo", auth_token="t" * 16
            )
        }
        _install_fakes(
            monkeypatch,
            sessions=sessions,
            windows={
                "worker-demo": _fake_window(
                    pane_current_command="node", pane_pid=9999
                )
            },
            heartbeats={"worker_demo": _heartbeat(seconds_ago=3)},
        )

        result = runner.invoke(
            _build_cli_app(), ["sessions", "--health", "--json"]
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output.strip().splitlines()[0])
        assert payload["pane_current_command"] == "node"
        assert payload["pid"] == 9999
        # log mtime missing on disk → None, not crash.
        assert payload["log_mtime"] is None

    def test_no_sessions_configured_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_fakes(monkeypatch, sessions={}, windows={}, heartbeats={})
        result = runner.invoke(_build_cli_app(), ["sessions"])
        assert result.exit_code == 0, result.output
        assert "No sessions configured." in result.output

    def test_disabled_sessions_are_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sessions = {
            "worker_demo": _make_session("worker_demo"),
            "worker_off": _make_session("worker_off", enabled=False),
        }
        _install_fakes(monkeypatch, sessions=sessions, windows={}, heartbeats={})
        result = runner.invoke(_build_cli_app(), ["sessions", "--json"])
        assert result.exit_code == 0, result.output
        names = [
            json.loads(line)["name"]
            for line in result.output.splitlines()
            if line.strip()
        ]
        assert "worker_demo" in names
        assert "worker_off" not in names
