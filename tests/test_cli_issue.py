from pathlib import Path

from typer.testing import CliRunner

from pollypm.cli import app
from pollypm.config import write_config
from pollypm.models import AccountConfig, KnownProject, PollyPMConfig, PollyPMSettings, ProjectKind, ProjectSettings, ProviderKind


def _config(tmp_path: Path) -> Path:
    project_root = tmp_path / "demo"
    project_root.mkdir()
    config = PollyPMConfig(
        project=ProjectSettings(
            root_dir=tmp_path,
            base_dir=tmp_path / ".pollypm-state",
            logs_dir=tmp_path / ".pollypm-state/logs",
            snapshots_dir=tmp_path / ".pollypm-state/snapshots",
            state_db=tmp_path / ".pollypm-state/state.db",
        ),
        pollypm=PollyPMSettings(controller_account="claude_main"),
        accounts={
            "claude_main": AccountConfig(
                name="claude_main",
                provider=ProviderKind.CLAUDE,
                home=tmp_path / ".pollypm-state" / "homes" / "claude_main",
            )
        },
        sessions={},
        projects={
            "demo": KnownProject(key="demo", path=project_root, name="Demo", kind=ProjectKind.GIT, tracked=True),
        },
    )
    config_path = tmp_path / "pollypm.toml"
    write_config(config, config_path, force=True)
    return config_path


def test_issue_cli_create_list_transition_and_counts(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = _config(tmp_path)

    created = runner.invoke(
        app,
        ["issue", "create", "--config", str(config_path), "--project", "demo", "--title", "Wire backend", "--body", "Implement it"],
    )
    listed = runner.invoke(
        app,
        ["issue", "list", "--config", str(config_path), "--project", "demo"],
    )
    transitioned = runner.invoke(
        app,
        ["issue", "transition", "--config", str(config_path), "--project", "demo", "0001", "02-in-progress"],
    )
    counts = runner.invoke(
        app,
        ["issue", "counts", "--config", str(config_path), "--project", "demo"],
    )

    assert created.exit_code == 0
    assert "Created issue 0001 [01-ready] Wire backend" in created.stdout
    assert listed.exit_code == 0
    assert "0001 [01-ready] Wire backend" in listed.stdout
    assert transitioned.exit_code == 0
    assert "Moved issue 0001 to 02-in-progress" in transitioned.stdout
    assert counts.exit_code == 0
    assert "02-in-progress: 1" in counts.stdout


def test_issue_cli_comment_updates_notes_file(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = _config(tmp_path)

    result = runner.invoke(
        app,
        ["issue", "comment", "--config", str(config_path), "--project", "demo", "notes.md", "--text", "Remember the edge case."],
    )

    assert result.exit_code == 0
    assert "notes.md" in result.stdout
    assert "Remember the edge case." in (tmp_path / "demo" / "issues" / "notes.md").read_text()
