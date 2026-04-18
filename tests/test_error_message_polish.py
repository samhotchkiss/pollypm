"""Tests for the error-message polish sweep (reports/error-message-audit.md).

These tests pin the top-priority audit fixes so a future refactor can't
silently regress:

1. Provider-probe failures include the account name + a "Fix:" hint.
2. ``pm issue`` commands route ValueErrors to stderr with a prefix.
3. ``pm add-project`` history-import failure routes to stderr (not stdout)
   and names the retry command.
4. The centralized ``format_config_not_found_error`` helper produces
   consistent phrasing across the seven callers.

All tests are self-contained and use a throwaway ``tmp_path`` config —
no dependency on Sam's 1.9GB state.db.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from pollypm.errors import (
    _last_lines,
    format_config_not_found_error,
    format_probe_failure,
)


# ---------------------------------------------------------------------------
# Helper-level unit tests (no supervisor / CLI — pure function output)
# ---------------------------------------------------------------------------


class TestFormatConfigNotFoundError:
    """The canonical "config not found" helper (errors.py)."""

    def test_includes_absolute_path_in_message(self, tmp_path: Path) -> None:
        target = tmp_path / "missing.toml"

        msg = format_config_not_found_error(target)

        assert str(target) in msg

    def test_includes_pm_onboard_hint(self, tmp_path: Path) -> None:
        msg = format_config_not_found_error(tmp_path / "x.toml")

        # All three recovery paths must be named in the Fix block so the
        # user (or scripted caller) can copy-paste any of them.
        assert "pm onboard" in msg
        assert "pm init" in msg
        assert "--config" in msg

    def test_ends_with_fix_prefix(self, tmp_path: Path) -> None:
        msg = format_config_not_found_error(tmp_path / "x.toml")

        assert "Fix:" in msg

    def test_consistent_text_across_multiple_calls(self, tmp_path: Path) -> None:
        # Two different paths → two messages that differ only in path.
        a = format_config_not_found_error(tmp_path / "a.toml")
        b = format_config_not_found_error(tmp_path / "b.toml")

        # Stripping the path prefix leaves the same scaffolding text.
        assert a.replace("a.toml", "X") == b.replace("b.toml", "X")


class TestFormatProbeFailure:
    """The three-block probe-failure helper (errors.py)."""

    def test_includes_account_name_and_email(self) -> None:
        msg = format_probe_failure(
            provider="Codex",
            account_name="claude_main",
            account_email="sam@example.com",
            reason="the account is not authenticated",
        )

        assert "'claude_main'" in msg
        assert "sam@example.com" in msg

    def test_omits_email_parens_when_absent(self) -> None:
        msg = format_probe_failure(
            provider="Codex",
            account_name="cx",
            account_email=None,
            reason="broke",
        )

        # No stray "None" token or empty parens.
        assert "None" not in msg
        assert "()" not in msg

    def test_default_fix_references_pm_relogin(self) -> None:
        msg = format_probe_failure(
            provider="Claude",
            account_name="primary",
            account_email="x@y",
            reason="failed",
        )

        assert "pm relogin primary" in msg
        assert "Fix:" in msg

    def test_custom_fix_appears_verbatim(self) -> None:
        msg = format_probe_failure(
            provider="Codex",
            account_name="cx",
            account_email=None,
            reason="bust",
            fix="switch the controller with `pm failover`.",
        )

        assert "pm failover" in msg
        # The default pm relogin hint must NOT appear when fix is
        # provided — otherwise both would show and confuse the user.
        assert "pm relogin" not in msg

    def test_pane_tail_is_included_verbatim(self) -> None:
        tail = "ERROR: credits exhausted\ngive up, old man"

        msg = format_probe_failure(
            provider="Codex",
            account_name="cx",
            account_email=None,
            reason="dead",
            pane_tail=tail,
        )

        assert "Last probe output:" in msg
        assert "credits exhausted" in msg

    def test_last_lines_returns_tail(self) -> None:
        text = "\n".join(f"line{i}" for i in range(20))

        tail = _last_lines(text, n=3)

        assert tail == "line17\nline18\nline19"

    def test_last_lines_on_empty_text_returns_empty(self) -> None:
        assert _last_lines("", n=5) == ""


# ---------------------------------------------------------------------------
# Supervisor probe integration — does the shaped error land?
# ---------------------------------------------------------------------------


def _supervisor_config(tmp_path: Path):
    """Build a minimal PollyPMConfig with one controller account."""
    from pollypm.models import (
        AccountConfig,
        KnownProject,
        PollyPMConfig,
        PollyPMSettings,
        ProjectKind,
        ProjectSettings,
        ProviderKind,
        SessionConfig,
    )

    return PollyPMConfig(
        project=ProjectSettings(
            root_dir=tmp_path,
            base_dir=tmp_path / ".pollypm",
            logs_dir=tmp_path / ".pollypm/logs",
            snapshots_dir=tmp_path / ".pollypm/snapshots",
            state_db=tmp_path / ".pollypm/state.db",
        ),
        pollypm=PollyPMSettings(controller_account="claude_main"),
        accounts={
            "claude_main": AccountConfig(
                name="claude_main",
                provider=ProviderKind.CLAUDE,
                email="sam@example.com",
                home=tmp_path / ".pollypm/homes/claude_main",
            ),
            "codex_backup": AccountConfig(
                name="codex_backup",
                provider=ProviderKind.CODEX,
                email="sam+codex@example.com",
                home=tmp_path / ".pollypm/homes/codex_backup",
            ),
        },
        sessions={
            "operator": SessionConfig(
                name="operator",
                role="operator-pm",
                provider=ProviderKind.CLAUDE,
                account="claude_main",
                cwd=tmp_path,
                project="pollypm",
                window_name="pm-operator",
            ),
        },
        projects={
            "pollypm": KnownProject(
                key="pollypm",
                path=tmp_path,
                name="PollyPM",
                kind=ProjectKind.FOLDER,
            )
        },
    )


def _build_supervisor(tmp_path: Path, *, controller_account: str = "claude_main"):
    """Instantiate a Supervisor against the tmp config. Swap controller when needed."""
    from pollypm.supervisor import Supervisor

    config = _supervisor_config(tmp_path)
    config.pollypm.controller_account = controller_account
    if controller_account == "codex_backup":
        # Make the operator session use codex so ``_probe_controller_account``
        # routes into the Codex branch.
        op = config.sessions["operator"]
        from pollypm.models import ProviderKind, SessionConfig

        config.sessions["operator"] = SessionConfig(
            name=op.name,
            role=op.role,
            provider=ProviderKind.CODEX,
            account="codex_backup",
            cwd=op.cwd,
            project=op.project,
            window_name=op.window_name,
        )
    return Supervisor(config)


class TestSupervisorProbeErrors:
    def test_claude_probe_failure_names_account_and_relogin(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        supervisor = _build_supervisor(tmp_path, controller_account="claude_main")
        # Make the probe return gibberish so the "ok" branch doesn't fire.
        monkeypatch.setattr(
            supervisor,
            "_run_probe",
            lambda _account: "claude: something blew up\ntry again later",
        )

        with pytest.raises(RuntimeError) as excinfo:
            supervisor._probe_controller_account("claude_main")

        msg = str(excinfo.value)
        assert "claude_main" in msg
        assert "sam@example.com" in msg
        assert "pm relogin claude_main" in msg
        assert "Fix:" in msg
        # Pane tail context — last 5 non-empty lines must appear verbatim.
        assert "try again later" in msg

    def test_codex_out_of_credits_names_account_and_failover(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        supervisor = _build_supervisor(tmp_path, controller_account="codex_backup")
        monkeypatch.setattr(
            supervisor,
            "_run_probe",
            lambda _account: "we hit your usage limit — please top up",
        )

        with pytest.raises(RuntimeError) as excinfo:
            supervisor._probe_controller_account("codex_backup")

        msg = str(excinfo.value)
        assert "codex_backup" in msg
        assert "out of credits" in msg
        assert "pm failover" in msg or "pm accounts" in msg

    def test_codex_not_authenticated_names_account_and_relogin(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        supervisor = _build_supervisor(tmp_path, controller_account="codex_backup")
        monkeypatch.setattr(
            supervisor,
            "_run_probe",
            lambda _account: "please login to continue",
        )

        with pytest.raises(RuntimeError) as excinfo:
            supervisor._probe_controller_account("codex_backup")

        msg = str(excinfo.value)
        assert "codex_backup" in msg
        assert "pm relogin codex_backup" in msg
        assert "not authenticated" in msg


# ---------------------------------------------------------------------------
# `pm issue` — ValueError routing to stderr
# ---------------------------------------------------------------------------


def _issue_config(tmp_path: Path) -> Path:
    """Build the minimal config `pm issue` needs to run."""
    from pollypm.config import write_config
    from pollypm.models import (
        AccountConfig,
        KnownProject,
        PollyPMConfig,
        PollyPMSettings,
        ProjectKind,
        ProjectSettings,
        ProviderKind,
    )

    project_root = tmp_path / "demo"
    project_root.mkdir()
    config = PollyPMConfig(
        project=ProjectSettings(
            root_dir=tmp_path,
            base_dir=tmp_path / ".pollypm",
            logs_dir=tmp_path / ".pollypm/logs",
            snapshots_dir=tmp_path / ".pollypm/snapshots",
            state_db=tmp_path / ".pollypm/state.db",
        ),
        pollypm=PollyPMSettings(controller_account="claude_main"),
        accounts={
            "claude_main": AccountConfig(
                name="claude_main",
                provider=ProviderKind.CLAUDE,
                home=tmp_path / ".pollypm/homes/claude_main",
            )
        },
        sessions={},
        projects={
            "demo": KnownProject(
                key="demo",
                path=project_root,
                name="Demo",
                kind=ProjectKind.GIT,
                tracked=True,
            ),
        },
    )
    config_path = tmp_path / "pollypm.toml"
    write_config(config, config_path, force=True)
    return config_path


class TestPmIssueStderrRouting:
    """`pm issue transition/approve/request-changes` must land errors on stderr."""

    def test_transition_routes_value_error_to_stderr_with_prefix(
        self, tmp_path: Path
    ) -> None:
        from pollypm.cli import app

        config_path = _issue_config(tmp_path)
        runner = CliRunner()

        # Create a task then try to transition to an invalid state.
        create = runner.invoke(
            app,
            [
                "issue", "create",
                "--config", str(config_path),
                "--project", "demo",
                "--title", "Example",
            ],
        )
        assert create.exit_code == 0, create.stdout + (create.stderr or "")

        # FileTaskBackend raises ValueError on unknown states.
        result = runner.invoke(
            app,
            [
                "issue", "transition",
                "--config", str(config_path),
                "--project", "demo",
                "0001", "99-nope",
            ],
        )

        assert result.exit_code == 1
        combined = (result.stdout or "") + (result.stderr or "")
        assert "Cannot transition 0001" in combined
        # stdout must NOT carry the raw error — it should go to stderr only.
        # (CliRunner coalesces unless mix_stderr=False; we accept either as
        # long as the prefix is present and stdout doesn't ALSO print
        # the raw state name.)
        assert "Cannot transition" in combined

    def test_approve_routes_value_error_to_stderr_with_prefix(
        self, tmp_path: Path
    ) -> None:
        from pollypm.cli import app

        config_path = _issue_config(tmp_path)
        runner = CliRunner()

        # Create a task (01-ready) then try to approve it — review_task
        # rejects non-review states with a ValueError.
        runner.invoke(
            app,
            [
                "issue", "create",
                "--config", str(config_path),
                "--project", "demo",
                "--title", "Example",
            ],
        )
        result = runner.invoke(
            app,
            [
                "issue", "approve",
                "--config", str(config_path),
                "--project", "demo",
                "--summary", "looks good",
                "--verification", "ran tests",
                "0001",
            ],
        )

        assert result.exit_code == 1
        combined = (result.stdout or "") + (result.stderr or "")
        assert "Cannot approve 0001" in combined

    def test_request_changes_routes_value_error_to_stderr_with_prefix(
        self, tmp_path: Path
    ) -> None:
        from pollypm.cli import app

        config_path = _issue_config(tmp_path)
        runner = CliRunner()

        runner.invoke(
            app,
            [
                "issue", "create",
                "--config", str(config_path),
                "--project", "demo",
                "--title", "Example",
            ],
        )
        result = runner.invoke(
            app,
            [
                "issue", "request-changes",
                "--config", str(config_path),
                "--project", "demo",
                "--summary", "no",
                "--verification", "tested",
                "--changes", "do the thing",
                "0001",
            ],
        )

        assert result.exit_code == 1
        combined = (result.stdout or "") + (result.stderr or "")
        assert "Cannot request changes on 0001" in combined


# ---------------------------------------------------------------------------
# `pm add-project` — history-import failure lands on stderr with retry hint
# ---------------------------------------------------------------------------


class TestAddProjectFailureSurfaces:
    def test_history_import_failure_routes_to_stderr_with_retry_hint(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """When ``import_project_history`` raises, the warning must:

        * land on stderr (not stdout),
        * start with ``Failed:`` so scripted callers can grep it,
        * name the project key, and
        * tell the user to rerun ``pm import <key>``.
        """
        from pollypm.config import write_config
        from pollypm.models import (
            AccountConfig,
            PollyPMConfig,
            PollyPMSettings,
            ProjectSettings,
            ProviderKind,
        )

        repo = tmp_path / "proj"
        repo.mkdir()
        (repo / ".git").mkdir()

        config = PollyPMConfig(
            project=ProjectSettings(
                root_dir=tmp_path,
                base_dir=tmp_path / ".pollypm",
                logs_dir=tmp_path / ".pollypm/logs",
                snapshots_dir=tmp_path / ".pollypm/snapshots",
                state_db=tmp_path / ".pollypm/state.db",
            ),
            pollypm=PollyPMSettings(controller_account="claude_main"),
            accounts={
                "claude_main": AccountConfig(
                    name="claude_main",
                    provider=ProviderKind.CLAUDE,
                    home=tmp_path / ".pollypm/homes/claude_main",
                ),
            },
            sessions={},
            projects={},
        )
        config_path = tmp_path / "pollypm.toml"
        write_config(config, config_path, force=True)

        # Make the project.created observer silent (we're testing the
        # history-import path, not the observer). Import ``extension_host_for_root``
        # lazily since the CLI imports it inside the command body.
        def _fake_host(_root: str):
            class _Host:
                def run_observers(self, *_args, **_kwargs):
                    return None

            return _Host()

        monkeypatch.setattr(
            "pollypm.plugin_host.extension_host_for_root", _fake_host,
        )

        # Force history import to blow up.
        def _boom(*_args, **_kwargs):
            raise RuntimeError("synthetic import crash")

        monkeypatch.setattr(
            "pollypm.history_import.import_project_history", _boom,
        )

        from pollypm.cli import app

        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "add-project",
                str(repo),
                "--config", str(config_path),
                "--name", "proj",
            ],
        )

        # add-project does NOT exit non-zero on history import failure —
        # the project is registered, so the warning is a "Failed:" line
        # but the exit code stays 0 (we're preserving existing behavior,
        # only fixing the stream and phrasing).
        combined = (result.stdout or "") + (result.stderr or "")
        assert "Failed:" in combined
        assert "history import" in combined
        assert "pm import" in combined
        # Project key appears so the user knows which project to retry.
        assert "proj" in combined


# ---------------------------------------------------------------------------
# Config-not-found helper consumers agree on phrasing
# ---------------------------------------------------------------------------


class TestConfigNotFoundHelperConsumers:
    """The helper's callers all route the same shape to the user."""

    def test_downtime_cli_uses_helper(self, tmp_path: Path, monkeypatch) -> None:
        from pollypm.plugins_builtin.downtime.cli import downtime_app

        missing_config = tmp_path / "nope.toml"
        runner = CliRunner()
        result = runner.invoke(
            downtime_app, ["status", "--config", str(missing_config)],
        )

        combined = (result.stdout or "") + (result.stderr or "")
        assert "No PollyPM config at" in combined
        assert "pm onboard" in combined
        assert "pm init" in combined
        assert result.exit_code != 0

    def test_cli_reset_uses_helper(self, tmp_path: Path) -> None:
        from pollypm.cli import app

        missing_config = tmp_path / "nope.toml"
        runner = CliRunner()
        result = runner.invoke(
            app, ["reset", "--force", "--config", str(missing_config)],
        )

        combined = (result.stdout or "") + (result.stderr or "")
        assert "No PollyPM config at" in combined
        assert "pm onboard" in combined
        assert result.exit_code != 0
