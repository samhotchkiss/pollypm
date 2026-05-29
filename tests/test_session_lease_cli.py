from __future__ import annotations

from typer.testing import CliRunner

import pollypm.cli as cli


def test_claim_task_id_hints_before_loading_supervisor(monkeypatch) -> None:
    def fail_load_supervisor(_config_path):
        raise AssertionError("task-id hint should run before supervisor loading")

    monkeypatch.setattr(cli, "_load_supervisor", fail_load_supervisor)

    result = CliRunner().invoke(cli.app, ["claim", "shortlink_gen/1"])

    assert result.exit_code == 2
    assert "Did you mean 'pm task claim shortlink_gen/1'?" in result.output
    assert "'pm claim' sets a tmux session lease, not a work task." in result.output


def test_release_task_id_hints_before_loading_supervisor(monkeypatch) -> None:
    def fail_load_supervisor(_config_path):
        raise AssertionError("task-id hint should run before supervisor loading")

    monkeypatch.setattr(cli, "_load_supervisor", fail_load_supervisor)

    result = CliRunner().invoke(cli.app, ["release", "shortlink_gen/1"])

    assert result.exit_code == 2
    assert "Did you mean a work-task command for shortlink_gen/1?" in result.output
    assert "'pm release' clears a tmux session lease, not a work task." in result.output
    assert "pm task --help" in result.output
