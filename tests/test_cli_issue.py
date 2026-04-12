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


def _configure_github_backend(project_root: Path) -> None:
    config_dir = project_root / ".pollypm" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "project.toml").write_text(
        """
[project]
display_name = "Demo"

[plugins]
issue_backend = "github"

[plugins.github_issues]
repo = "acme/widgets"
"""
    )


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
    info = runner.invoke(
        app,
        ["issue", "info", "--config", str(config_path), "--project", "demo", "0001"],
    )
    next_ready = runner.invoke(
        app,
        ["issue", "next", "--config", str(config_path), "--project", "demo"],
    )
    history = runner.invoke(
        app,
        ["issue", "history", "--config", str(config_path), "--project", "demo", "0001"],
    )
    transitioned = runner.invoke(
        app,
        ["issue", "transition", "--config", str(config_path), "--project", "demo", "0001", "02-in-progress"],
    )
    counts = runner.invoke(
        app,
        ["issue", "counts", "--config", str(config_path), "--project", "demo"],
    )
    report = runner.invoke(
        app,
        ["issue", "report", "--config", str(config_path), "--project", "demo"],
    )
    report_status = runner.invoke(
        app,
        ["report", "status", "--config", str(config_path), "--project", "demo"],
    )

    assert created.exit_code == 0
    assert "Created issue 0001 [01-ready] Wire backend" in created.stdout
    assert listed.exit_code == 0
    assert "0001 [01-ready] Wire backend" in listed.stdout
    assert info.exit_code == 0
    assert "0001 [01-ready] Wire backend" in info.stdout
    assert next_ready.exit_code == 0
    assert "0001 [01-ready] Wire backend" in next_ready.stdout
    assert history.exit_code == 0
    assert "state=01-ready" in history.stdout
    assert transitioned.exit_code == 0
    assert "Moved issue 0001 to 02-in-progress" in transitioned.stdout
    assert counts.exit_code == 0
    assert "02-in-progress: 1" in counts.stdout
    assert report.exit_code == 0
    assert report.stdout == counts.stdout
    assert report_status.exit_code == 0
    assert report_status.stdout == counts.stdout


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


def test_issue_cli_handoff_writes_structured_note(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = _config(tmp_path)

    result = runner.invoke(
        app,
        [
            "issue",
            "handoff",
            "--config",
            str(config_path),
            "--project",
            "demo",
            "--done",
            "Implemented the issue commands.",
            "--test",
            "Run pytest on CLI and service tests.",
            "--deviations",
            "No live gh call in unit coverage.",
            "notes.md",
        ],
    )

    text = (tmp_path / "demo" / "issues" / "notes.md").read_text()
    assert result.exit_code == 0
    assert "## Handoff" in text
    assert "Implemented the issue commands." in text
    assert "Run pytest on CLI and service tests." in text
    assert "No live gh call in unit coverage." in text


def test_issue_cli_uses_github_backend_when_project_is_configured(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = _config(tmp_path)
    _configure_github_backend(tmp_path / "demo")
    calls: list[tuple[str, ...]] = []
    current_label = "polly:ready"

    def fake_gh(*args: str, check: bool = True):
        nonlocal current_label
        calls.append(args)

        class Result:
            def __init__(self, stdout: str = "") -> None:
                self.stdout = stdout

        if args[:2] == ("issue", "create"):
            return Result("https://github.com/acme/widgets/issues/42\n")
        if args[:2] == ("issue", "view"):
            if "--json" in args and "comments" in args:
                return Result('{"comments":[{"author":{"login":"polly"},"body":"Ready for review."}]}')
            return Result(f'{{"number":42,"title":"Wire backend","labels":[{{"name":"{current_label}"}}]}}')
        if args[:2] == ("issue", "list"):
            if "--json" in args and "-q" in args:
                return Result("2")
            return Result('[{"number":42,"title":"Wire backend","state":"OPEN"}]')
        if args[:2] == ("issue", "edit") and "--add-label" in args:
            current_label = args[args.index("--add-label") + 1]
            return Result()
        if args[:2] == ("issue", "comment"):
            return Result()
        return Result()

    monkeypatch.setattr("pollypm.task_backends.github._gh", fake_gh)

    created = runner.invoke(
        app,
        ["issue", "create", "--config", str(config_path), "--project", "demo", "--title", "Wire backend", "--body", "Implement it"],
    )
    listed = runner.invoke(
        app,
        ["issue", "list", "--config", str(config_path), "--project", "demo"],
    )
    info = runner.invoke(
        app,
        ["issue", "info", "--config", str(config_path), "--project", "demo", "42"],
    )
    next_ready = runner.invoke(
        app,
        ["issue", "next", "--config", str(config_path), "--project", "demo"],
    )
    history = runner.invoke(
        app,
        ["issue", "history", "--config", str(config_path), "--project", "demo", "42"],
    )
    transitioned = runner.invoke(
        app,
        ["issue", "transition", "--config", str(config_path), "--project", "demo", "42", "02-in-progress"],
    )
    transitioned_review = runner.invoke(
        app,
        ["issue", "transition", "--config", str(config_path), "--project", "demo", "42", "03-needs-review"],
    )
    commented = runner.invoke(
        app,
        ["issue", "comment", "--config", str(config_path), "--project", "demo", "#42", "--text", "Implemented and verified."],
    )
    counts = runner.invoke(
        app,
        ["issue", "counts", "--config", str(config_path), "--project", "demo"],
    )
    report = runner.invoke(
        app,
        ["issue", "report", "--config", str(config_path), "--project", "demo"],
    )
    report_status = runner.invoke(
        app,
        ["report", "status", "--config", str(config_path), "--project", "demo"],
    )

    assert created.exit_code == 0
    assert "Created issue 42 [01-ready] Wire backend" in created.stdout
    assert listed.exit_code == 0
    assert "42 [01-ready] Wire backend" in listed.stdout
    assert info.exit_code == 0
    assert "42 [01-ready] Wire backend" in info.stdout
    assert next_ready.exit_code == 0
    assert "42 [01-ready] Wire backend" in next_ready.stdout
    assert history.exit_code == 0
    assert "polly: Ready for review." in history.stdout
    assert transitioned.exit_code == 0
    assert "Moved issue 42 to 02-in-progress" in transitioned.stdout
    assert transitioned_review.exit_code == 0
    assert "Moved issue 42 to 03-needs-review" in transitioned_review.stdout
    assert commented.exit_code == 0
    assert "#42" in commented.stdout
    assert counts.exit_code == 0
    assert "01-ready: 2" in counts.stdout
    assert report.exit_code == 0
    assert report.stdout == counts.stdout
    assert report_status.exit_code == 0
    assert report_status.stdout == counts.stdout
    assert ("issue", "create", "--title", "Wire backend", "--body", "Implement it", "--label", "polly:ready", "--repo", "acme/widgets") in calls


def test_issue_cli_validate_reports_backend_validation(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = _config(tmp_path)
    _configure_github_backend(tmp_path / "demo")

    monkeypatch.setattr(
        "pollypm.task_backends.github.GitHubTaskBackend.validate",
        lambda self: type("Result", (), {"passed": True, "checks": ["repo_accessible", "labels_ensured"], "errors": []})(),
    )

    result = runner.invoke(
        app,
        ["issue", "validate", "--config", str(config_path), "--project", "demo"],
    )

    assert result.exit_code == 0
    assert "Task backend validation passed." in result.stdout
    assert "check: repo_accessible" in result.stdout
    assert "check: labels_ensured" in result.stdout


def test_issue_cli_rejects_skipped_transition(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = _config(tmp_path)

    runner.invoke(
        app,
        ["issue", "create", "--config", str(config_path), "--project", "demo", "--title", "Wire backend"],
    )
    result = runner.invoke(
        app,
        ["issue", "transition", "--config", str(config_path), "--project", "demo", "0001", "03-needs-review"],
    )

    assert result.exit_code == 1
    assert "Invalid transition 01-ready -> 03-needs-review" in result.stdout
