from datetime import UTC, datetime, timedelta
from pathlib import Path

from pollypm.config import write_config
from pollypm.models import AccountConfig, KnownProject, PollyPMConfig, PollyPMSettings, ProjectKind, ProjectSettings, ProviderKind
from pollypm.service_api import PollyPMService, render_json
from pollypm.task_backends.github import GitHubTaskBackendValidation


def test_service_create_and_launch_worker_uses_worker_api(monkeypatch, tmp_path: Path) -> None:
    service = PollyPMService(tmp_path / "pollypm.toml")
    calls: list[tuple[str, object]] = []

    class FakeSession:
        name = "worker_pollypm"

    class FakeTmux:
        def has_session(self, _name: str) -> bool:
            return True

    class FakeConfigProject:
        tmux_session = "pollypm"

    class FakeConfig:
        project = FakeConfigProject()

    class FakeSupervisor:
        tmux = FakeTmux()
        config = FakeConfig()

    monkeypatch.setattr(
        "pollypm.service_api.create_worker_session",
        lambda config_path, project_key, prompt: calls.append(("create", project_key, prompt)) or FakeSession(),
    )
    monkeypatch.setattr(
        "pollypm.service_api.launch_worker_session",
        lambda config_path, session_name, on_status=None, skip_stabilize=False: calls.append(("launch", session_name)),
    )
    monkeypatch.setattr(service, "load_supervisor", lambda: FakeSupervisor())

    session = service.create_and_launch_worker(project_key="pollypm", prompt="Do the next task")

    assert session.name == "worker_pollypm"
    assert calls == [
        ("create", "pollypm", "Do the next task"),
        ("launch", "worker_pollypm"),
    ]


def test_service_suggest_worker_prompt_uses_worker_api(monkeypatch, tmp_path: Path) -> None:
    service = PollyPMService(tmp_path / "pollypm.toml")

    monkeypatch.setattr(
        "pollypm.service_api.suggest_worker_prompt",
        lambda config_path, project_key: f"Kick off {project_key}",
    )

    assert service.suggest_worker_prompt(project_key="pollypm") == "Kick off pollypm"


def test_service_focus_and_send_input_use_supervisor(monkeypatch, tmp_path: Path) -> None:
    service = PollyPMService(tmp_path / "pollypm.toml")
    calls: list[tuple[str, str, str | None]] = []

    class FakeSupervisor:
        def focus_session(self, session_name: str) -> None:
            calls.append(("focus", session_name, None))

        def send_input(self, session_name: str, text: str, owner: str = "human") -> None:
            calls.append(("send", session_name, text))

    monkeypatch.setattr(service, "load_supervisor", lambda: FakeSupervisor())

    service.focus_session("operator")
    service.send_input("operator", "Continue", owner="human")

    assert calls == [
        ("focus", "operator", None),
        ("send", "operator", "Continue"),
    ]


def test_service_schedule_job_uses_supervisor(monkeypatch, tmp_path: Path) -> None:
    service = PollyPMService(tmp_path / "pollypm.toml")
    captured: list[tuple[str, object, object]] = []

    class FakeSupervisor:
        def schedule_job(self, *, kind: str, run_at: datetime, payload=None, interval_seconds=None):
            captured.append((kind, run_at, payload))
            return "job"

        def list_scheduled_jobs(self):
            return ["job"]

        def run_scheduled_jobs(self):
            return ["job"]

    monkeypatch.setattr(service, "load_supervisor", lambda: FakeSupervisor())

    run_at = datetime.now(UTC) + timedelta(minutes=5)
    assert service.schedule_job(kind="heartbeat", run_at=run_at) == "job"
    assert service.list_jobs() == ["job"]
    assert service.run_scheduled_jobs() == ["job"]
    assert captured == [("heartbeat", run_at, None)]


def test_service_ensure_pollypm_ensures_console_and_heartbeat(monkeypatch, tmp_path: Path) -> None:
    service = PollyPMService(tmp_path / "pollypm.toml")
    calls: list[str] = []

    class FakeTmux:
        def has_session(self, _name: str) -> bool:
            return True

    class FakePollyPM:
        controller_account = "claude_controller"

    class FakeConfigProject:
        tmux_session = "pollypm"

    class FakeConfig:
        project = FakeConfigProject()
        pollypm = FakePollyPM()

    class FakeSupervisor:
        tmux = FakeTmux()
        config = FakeConfig()

        def ensure_console_window(self) -> None:
            calls.append("console")

        def ensure_heartbeat_schedule(self) -> None:
            calls.append("heartbeat")

    monkeypatch.setattr(service, "load_supervisor", lambda: FakeSupervisor())

    account = service.ensure_pollypm()

    assert account == "claude_controller"
    assert calls == ["console", "heartbeat"]


def test_render_json_serializes_datetime() -> None:
    payload = {"run_at": datetime(2026, 4, 10, 12, 0, tzinfo=UTC)}

    rendered = render_json(payload)

    assert '"run_at": "2026-04-10T12:00:00+00:00"' in rendered


def test_service_task_operations_use_file_backend(tmp_path: Path) -> None:
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
    service = PollyPMService(config_path)

    task = service.create_task("demo", title="Wire the backend", body="Implement the file-backed task flow.")
    moved = service.move_task("demo", task.task_id, to_state="02-in-progress")
    note_path = service.append_task_note("demo", "notes.md", text="Remember to verify the happy path.\n")
    listed = service.list_tasks("demo", states=["02-in-progress"])
    fetched = service.get_task("demo", "0001")
    next_task = service.next_available_task("demo")
    history = service.task_history("demo", "0001")

    assert task.task_id == "0001"
    assert moved.state == "02-in-progress"
    assert note_path.exists()
    assert listed[0].task_id == "0001"
    assert fetched.task_id == "0001"
    assert next_task is None
    assert "Remember to verify the happy path." in history
    assert service.task_state_counts("demo")["02-in-progress"] == 1


def test_service_task_operations_use_github_backend(monkeypatch, tmp_path: Path) -> None:
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
    service = PollyPMService(config_path)
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
            return Result(f'{{"number":42,"title":"Wire the backend","labels":[{{"name":"{current_label}"}}]}}')
        if args[:2] == ("issue", "list"):
            if "--json" in args and "-q" in args:
                return Result("3")
            return Result('[{"number":42,"title":"Wire the backend","state":"OPEN"}]')
        if args[:2] == ("issue", "edit") and "--add-label" in args:
            current_label = args[args.index("--add-label") + 1]
            return Result()
        if args[:2] == ("issue", "comment"):
            return Result()
        return Result()

    monkeypatch.setattr("pollypm.task_backends.github._gh", fake_gh)

    task = service.create_task("demo", title="Wire the backend", body="Implement the gh-backed task flow.")
    service.move_task("demo", "42", to_state="02-in-progress")
    moved = service.move_task("demo", "42", to_state="03-needs-review")
    note_path = service.append_task_note("demo", "#42", text="Implemented and verified.")
    listed = service.list_tasks("demo", states=["01-ready"])
    fetched = service.get_task("demo", "42")
    next_task = service.next_available_task("demo")
    history = service.task_history("demo", "42")
    counts = service.task_state_counts("demo")

    assert task.task_id == "42"
    assert moved.state == "03-needs-review"
    assert note_path == project_root / "#42"
    assert listed[0].task_id == "42"
    assert fetched.task_id == "42"
    assert next_task is not None
    assert next_task.task_id == "42"
    assert history == ["polly: Ready for review."]
    assert counts["01-ready"] == 3


def test_service_validate_task_backend_uses_backend_validation(monkeypatch, tmp_path: Path) -> None:
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
    service = PollyPMService(config_path)

    monkeypatch.setattr(
        "pollypm.task_backends.github.GitHubTaskBackend.validate",
        lambda self: GitHubTaskBackendValidation(passed=True, checks=["repo_accessible"], errors=[]),
    )

    result = service.validate_task_backend("demo")

    assert result.passed is True
    assert result.checks == ["repo_accessible"]


def test_service_append_task_handoff_writes_structured_note(tmp_path: Path) -> None:
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
    service = PollyPMService(config_path)

    note_path = service.append_task_handoff(
        "demo",
        "notes.md",
        what_done="Implemented the GitHub-backed issue flow.",
        how_to_test="Run the targeted pytest suite.",
        deviations="Skipped live gh auth in unit tests.",
    )

    text = note_path.read_text()
    assert "## Handoff" in text
    assert "### What Was Done" in text
    assert "Implemented the GitHub-backed issue flow." in text
    assert "### How To Test" in text
    assert "Run the targeted pytest suite." in text
    assert "### Deviations" in text
    assert "Skipped live gh auth in unit tests." in text


def test_service_move_task_rejects_skipped_transition(tmp_path: Path) -> None:
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
    service = PollyPMService(config_path)
    task = service.create_task("demo", title="Wire the backend")

    try:
        service.move_task("demo", task.task_id, to_state="03-needs-review")
    except ValueError as exc:
        assert "Invalid transition 01-ready -> 03-needs-review" in str(exc)
    else:
        raise AssertionError("Expected skipped transition to fail")
