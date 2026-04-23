from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import subprocess

from pollypm.config import write_config
from pollypm.models import (
    AccountConfig,
    KnownProject,
    ModelAssignment,
    PollyPMConfig,
    PollyPMSettings,
    ProjectKind,
    ProjectSettings,
    ProviderKind,
)
from pollypm.service_api import PollyPMService, render_json
from pollypm.storage.state import StateStore
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
        "pollypm.service_api.v1.create_worker_session",
        lambda config_path, project_key, prompt: calls.append(("create", project_key, prompt)) or FakeSession(),
    )
    monkeypatch.setattr(
        "pollypm.service_api.v1.launch_worker_session",
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
        "pollypm.service_api.v1.suggest_worker_prompt",
        lambda config_path, project_key: f"Kick off {project_key}",
    )

    assert service.suggest_worker_prompt(project_key="pollypm") == "Kick off pollypm"


def test_service_role_routing_resolves_against_bound_config(tmp_path: Path) -> None:
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
        pollypm=PollyPMSettings(
            controller_account="claude_main",
            role_assignments={"worker": ModelAssignment(alias="codex-gpt-5.4")},
        ),
        accounts={
            "claude_main": AccountConfig(
                name="claude_main",
                provider=ProviderKind.CLAUDE,
                home=tmp_path / ".pollypm" / "homes" / "claude_main",
            )
        },
        sessions={},
        projects={"demo": KnownProject(key="demo", path=project_root, kind=ProjectKind.FOLDER)},
    )
    config_path = tmp_path / "pollypm.toml"
    write_config(config, config_path, force=True)

    resolved = PollyPMService(config_path).role_routing.resolve("worker", "demo")

    assert resolved.alias == "codex-gpt-5.4"
    assert resolved.provider == "codex"
    assert resolved.source == "global"


def test_service_focus_and_send_input_use_supervisor(monkeypatch, tmp_path: Path) -> None:
    service = PollyPMService(tmp_path / "pollypm.toml")
    calls: list[tuple[str, str, str | None]] = []

    class FakeSupervisor:
        def focus_session(self, session_name: str) -> None:
            calls.append(("focus", session_name, None))

        def send_input(
            self,
            session_name: str,
            text: str,
            *,
            owner: str = "human",
            force: bool = False,
            press_enter: bool = True,
        ) -> None:
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
                home=tmp_path / ".pollypm" / "homes" / "claude_main",
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
                home=tmp_path / ".pollypm" / "homes" / "claude_main",
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
                home=tmp_path / ".pollypm" / "homes" / "claude_main",
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
                home=tmp_path / ".pollypm" / "homes" / "claude_main",
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
        branch_or_pr="https://github.com/acme/widgets/pull/42",
        deviations="Skipped live gh auth in unit tests.",
    )

    text = note_path.read_text()
    assert "## Handoff" in text
    assert "### What Was Done" in text
    assert "Implemented the GitHub-backed issue flow." in text
    assert "### How To Test" in text
    assert "Run the targeted pytest suite." in text
    assert "### Branch / PR" in text
    assert "https://github.com/acme/widgets/pull/42" in text
    assert "### Deviations" in text
    assert "Skipped live gh auth in unit tests." in text


def test_service_review_task_approves_and_records_verification(tmp_path: Path) -> None:
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
                home=tmp_path / ".pollypm" / "homes" / "claude_main",
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
    service.move_task("demo", task.task_id, to_state="02-in-progress")
    service.move_task("demo", task.task_id, to_state="03-needs-review")

    moved = service.review_task(
        "demo",
        task.task_id,
        approved=True,
        summary="Looks correct.",
        verification="Ran pytest and exercised the CLI flow independently.",
    )

    assert moved.state == "05-completed"
    history = service.task_history("demo", task.task_id)
    assert "### Independent Verification" in history
    assert "Ran pytest and exercised the CLI flow independently." in history


def test_service_review_task_requests_changes_and_returns_to_in_progress(tmp_path: Path) -> None:
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
                home=tmp_path / ".pollypm" / "homes" / "claude_main",
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
    service.move_task("demo", task.task_id, to_state="02-in-progress")
    service.move_task("demo", task.task_id, to_state="03-needs-review")

    moved = service.review_task(
        "demo",
        task.task_id,
        approved=False,
        summary="The flow is close but incomplete.",
        verification="Ran the review CLI and inspected history output.",
        changes_requested="Add regression coverage for the reject loop.",
    )

    assert moved.state == "02-in-progress"
    history = service.task_history("demo", task.task_id)
    assert "### Change Requests" in history
    assert "Add regression coverage for the reject loop." in history


def test_service_review_task_uses_github_backend(monkeypatch, tmp_path: Path) -> None:
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
                home=tmp_path / ".pollypm" / "homes" / "claude_main",
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
    current_label = "polly:needs-review"
    comments: list[str] = []

    def fake_gh(*args: str, check: bool = True):
        nonlocal current_label
        calls.append(args)

        class Result:
            def __init__(self, stdout: str = "") -> None:
                self.stdout = stdout

        if args[:2] == ("issue", "view"):
            if "--json" in args and "comments" in args:
                payload = ",".join(f'{{"author":{{"login":"polly"}},"body":{comment!r}}}' for comment in comments)
                return Result(f'{{"comments":[{payload}]}}'.replace("'", '"'))
            return Result(f'{{"number":42,"title":"Wire the backend","labels":[{{"name":"{current_label}"}}]}}')
        if args[:2] == ("issue", "edit") and "--add-label" in args:
            current_label = args[args.index("--add-label") + 1]
            return Result()
        if args[:2] == ("issue", "comment"):
            comments.append(args[args.index("--body") + 1])
            return Result()
        if args[:2] == ("issue", "close"):
            return Result()
        return Result()

    monkeypatch.setattr("pollypm.task_backends.github._gh", fake_gh)

    approved = service.review_task(
        "demo",
        "42",
        approved=True,
        summary="Looks correct.",
        verification="Ran pytest independently.",
    )

    assert approved.state == "05-completed"
    assert any("## Review" in comment and "### Independent Verification" in comment for comment in comments)
    assert ("issue", "close", "42", "--repo", "acme/widgets") in calls

    current_label = "polly:needs-review"
    comments.clear()
    returned = service.review_task(
        "demo",
        "42",
        approved=False,
        summary="Needs another pass.",
        verification="Reviewed history and replayed the flow.",
        changes_requested="Add reject-loop coverage.",
    )

    assert returned.state == "02-in-progress"
    assert any("### Change Requests" in comment and "Add reject-loop coverage." in comment for comment in comments)


def test_service_review_task_records_level1_checkpoint_on_file_completion(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    project_root.mkdir()
    subprocess.run(["git", "init"], cwd=project_root, check=True, capture_output=True, text=True)
    (project_root / "src").mkdir()
    (project_root / "tests").mkdir()
    (project_root / "src" / "backend.py").write_text("print('ok')\n")
    (project_root / "tests" / "test_backend.py").write_text("def test_ok():\n    assert True\n")
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
                home=tmp_path / ".pollypm" / "homes" / "claude_main",
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

    task = service.create_task("demo", title="Wire the backend", body="Implement it.")
    service.move_task("demo", task.task_id, to_state="02-in-progress")
    service.move_task("demo", task.task_id, to_state="03-needs-review")
    approved = service.review_task(
        "demo",
        task.task_id,
        approved=True,
        summary="Looks correct.",
        verification="Added source and test coverage.",
    )

    assert approved.state == "05-completed"
    store = StateStore(config.project.state_db)
    checkpoint = store.latest_checkpoint("worker_demo")
    assert checkpoint is not None
    assert checkpoint.level == "level1"
    assert checkpoint.project_key == "demo"
    payload = json.loads(Path(checkpoint.json_path).read_text())
    assert payload["level"] == 1
    assert payload["trigger"] == "issue_completed"
    assert payload["objective"] == "Wire the backend"
    assert "src/backend.py" in payload["files_changed"]
    assert "tests/test_backend.py" in payload["files_changed"]
    assert any("Tests added or updated" in item for item in payload["work_completed"])


def test_service_move_task_to_completed_records_level1_checkpoint_for_file_backend(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    project_root.mkdir()
    subprocess.run(["git", "init"], cwd=project_root, check=True, capture_output=True, text=True)
    (project_root / "pkg").mkdir()
    (project_root / "pkg" / "feature.py").write_text("FEATURE = True\n")
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
                home=tmp_path / ".pollypm" / "homes" / "claude_main",
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

    task = service.create_task("demo", title="Ship it", body="Done.")
    service.move_task("demo", task.task_id, to_state="02-in-progress")
    service.move_task("demo", task.task_id, to_state="03-needs-review")
    service.move_task("demo", task.task_id, to_state="04-in-review")
    moved = service.move_task("demo", task.task_id, to_state="05-completed")

    assert moved.state == "05-completed"
    store = StateStore(config.project.state_db)
    checkpoint = store.latest_checkpoint("worker_demo")
    assert checkpoint is not None
    payload = json.loads(Path(checkpoint.json_path).read_text())
    assert payload["objective"] == "Ship it"
    assert "pkg/feature.py" in payload["files_changed"]


def test_service_move_task_rejects_skipped_transition(tmp_path: Path) -> None:
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
                home=tmp_path / ".pollypm" / "homes" / "claude_main",
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


def test_service_move_task_rejects_direct_completion_without_review(tmp_path: Path) -> None:
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
                home=tmp_path / ".pollypm" / "homes" / "claude_main",
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
    service.move_task("demo", task.task_id, to_state="02-in-progress")

    try:
        service.move_task("demo", task.task_id, to_state="05-completed")
    except ValueError as exc:
        assert "must pass through 04-in-review before completion" in str(exc)
    else:
        raise AssertionError("Expected direct completion to fail")
