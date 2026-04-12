from pathlib import Path

from pollypm.task_backends import FileTaskBackend, get_task_backend
from pollypm.task_backends.github import GitHubTaskBackend


def test_file_task_backend_creates_tracker_and_tasks(tmp_path: Path) -> None:
    backend = FileTaskBackend(tmp_path)

    issues_dir = backend.ensure_tracker()
    task = backend.create_task(title="Build plugin host", body="Implement the loader.")

    assert issues_dir == tmp_path / "issues"
    assert (issues_dir / "01-ready").exists()
    assert task.task_id == "0001"
    assert task.path.exists()
    assert backend.latest_issue_number() == 1


def test_file_task_backend_moves_tasks_between_states(tmp_path: Path) -> None:
    backend = FileTaskBackend(tmp_path)
    task = backend.create_task(title="Review plugin host")

    moved = backend.move_task(task.task_id, "03-needs-review")

    assert moved.state == "03-needs-review"
    assert moved.path.exists()
    assert not task.path.exists()


def test_file_task_backend_get_task_and_next_available(tmp_path: Path) -> None:
    backend = FileTaskBackend(tmp_path)
    first = backend.create_task(title="First ready")
    backend.create_task(title="Second ready")

    task = backend.get_task(first.task_id)
    next_task = backend.next_available()

    assert task.task_id == first.task_id
    assert next_task is not None
    assert next_task.task_id == first.task_id


def test_file_task_backend_tracks_notes_and_counts(tmp_path: Path) -> None:
    backend = FileTaskBackend(tmp_path)
    backend.ensure_tracker()
    backend.create_task(title="Build plugin host")
    backend.create_task(title="Review plugin host", state="02-in-progress")

    note_path = backend.append_note("notes.md", "Remember to review the service seam.\n")

    assert note_path.exists()
    assert "Remember to review the service seam." in note_path.read_text()
    counts = backend.state_counts()
    assert counts["01-ready"] == 1
    assert counts["02-in-progress"] == 1


def test_get_task_backend_returns_file_backend(tmp_path: Path) -> None:
    backend = get_task_backend(tmp_path)
    assert isinstance(backend, FileTaskBackend)


def test_get_task_backend_reads_github_backend_from_project_config(tmp_path: Path) -> None:
    config_dir = tmp_path / ".pollypm" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "project.toml").write_text(
        """
[plugins]
issue_backend = "github"

[plugins.github_issues]
repo = "acme/widgets"
"""
    )

    backend = get_task_backend(tmp_path)

    assert isinstance(backend, GitHubTaskBackend)
    assert backend.repo == "acme/widgets"


def test_github_task_backend_reads_issue_body(monkeypatch, tmp_path: Path) -> None:
    backend = GitHubTaskBackend(tmp_path, repo="acme/widgets")

    def fake_gh(*args: str, check: bool = True):
        class Result:
            stdout = '{"number": 42, "title": "Wire the backend", "body": "Implement the gh-backed tracker."}'

        return Result()

    monkeypatch.setattr("pollypm.task_backends.github._gh", fake_gh)

    task = backend.list_tasks(states=[]) if False else None
    record = type("Record", (), {"task_id": "42", "title": "Wire the backend", "state": "01-ready", "path": tmp_path / "#42"})()
    body = backend.read_task(record)

    assert body.startswith("# 42 Wire the backend")
    assert "Implement the gh-backed tracker." in body


def test_github_task_backend_get_task_and_next_available(monkeypatch, tmp_path: Path) -> None:
    backend = GitHubTaskBackend(tmp_path, repo="acme/widgets")

    def fake_gh(*args: str, check: bool = True):
        class Result:
            def __init__(self, stdout: str) -> None:
                self.stdout = stdout

        if args[:2] == ("issue", "view"):
            return Result('{"number":42,"title":"Wire the backend","labels":[{"name":"polly:needs-review"}]}')
        if args[:2] == ("issue", "list"):
            return Result('[{"number":41,"title":"First ready","state":"OPEN"}]')
        raise AssertionError(f"Unexpected gh call: {args}")

    monkeypatch.setattr("pollypm.task_backends.github._gh", fake_gh)

    task = backend.get_task("42")
    next_task = backend.next_available()

    assert task.task_id == "42"
    assert task.state == "03-needs-review"
    assert next_task is not None
    assert next_task.task_id == "41"


def test_github_task_backend_moves_state_by_relabeling_issue(monkeypatch, tmp_path: Path) -> None:
    backend = GitHubTaskBackend(tmp_path, repo="acme/widgets")
    calls: list[tuple[tuple[str, ...], bool]] = []

    def fake_gh(*args: str, check: bool = True):
        calls.append((args, check))

        class Result:
            def __init__(self, stdout: str = "") -> None:
                self.stdout = stdout

        if args[:2] == ("issue", "view"):
            return Result('{"title":"Wire the backend","labels":[{"name":"polly:ready"},{"name":"bug"}]}')
        return Result()

    monkeypatch.setattr("pollypm.task_backends.github._gh", fake_gh)

    moved = backend.move_task("42", "03-needs-review")

    assert moved.task_id == "42"
    assert moved.state == "03-needs-review"
    assert (("issue", "edit", "42", "--remove-label", "polly:ready", "--repo", "acme/widgets"), False) in calls
    assert (("issue", "edit", "42", "--add-label", "polly:needs-review", "--repo", "acme/widgets"), True) in calls
    assert (("issue", "reopen", "42", "--repo", "acme/widgets"), False) in calls


def test_github_task_backend_appends_issue_comment(monkeypatch, tmp_path: Path) -> None:
    backend = GitHubTaskBackend(tmp_path, repo="acme/widgets")
    calls: list[tuple[str, ...]] = []

    def fake_gh(*args: str, check: bool = True):
        calls.append(args)

        class Result:
            stdout = ""

        return Result()

    monkeypatch.setattr("pollypm.task_backends.github._gh", fake_gh)

    note_path = backend.append_note("#42", "Implemented and verified.")

    assert note_path == tmp_path / "#42"
    assert ("issue", "comment", "42", "--body", "Implemented and verified.", "--repo", "acme/widgets") in calls


def test_github_task_backend_reports_counts_per_state(monkeypatch, tmp_path: Path) -> None:
    backend = GitHubTaskBackend(tmp_path, repo="acme/widgets")
    counts_by_label = {
        "polly:not-ready": "0",
        "polly:ready": "2",
        "polly:in-progress": "1",
        "polly:needs-review": "3",
        "polly:in-review": "0",
        "polly:completed": "4",
    }

    def fake_gh(*args: str, check: bool = True):
        label = args[args.index("--label") + 1]

        class Result:
            def __init__(self, stdout: str) -> None:
                self.stdout = stdout

        return Result(counts_by_label[label])

    monkeypatch.setattr("pollypm.task_backends.github._gh", fake_gh)

    counts = backend.state_counts()

    assert counts == {
        "00-not-ready": 0,
        "01-ready": 2,
        "02-in-progress": 1,
        "03-needs-review": 3,
        "04-in-review": 0,
        "05-completed": 4,
    }


def test_github_task_backend_ensure_tracker_creates_missing_labels(monkeypatch, tmp_path: Path) -> None:
    backend = GitHubTaskBackend(tmp_path, repo="acme/widgets")
    calls: list[tuple[tuple[str, ...], bool]] = []

    def fake_gh(*args: str, check: bool = True):
        calls.append((args, check))

        class Result:
            def __init__(self, stdout: str = "") -> None:
                self.stdout = stdout

        if args[:2] == ("label", "list"):
            return Result("polly:ready\npolly:completed\n")
        return Result()

    monkeypatch.setattr("pollypm.task_backends.github._gh", fake_gh)

    root = backend.ensure_tracker()

    assert root == tmp_path
    created_labels = {
        args[2]
        for args, _check in calls
        if len(args) >= 3 and args[:2] == ("label", "create")
    }
    assert created_labels == {
        "polly:not-ready",
        "polly:in-progress",
        "polly:needs-review",
        "polly:in-review",
    }


def test_github_task_backend_create_task_parses_issue_number_from_url(monkeypatch, tmp_path: Path) -> None:
    backend = GitHubTaskBackend(tmp_path, repo="acme/widgets")
    calls: list[tuple[str, ...]] = []

    def fake_gh(*args: str, check: bool = True):
        calls.append(args)

        class Result:
            stdout = "https://github.com/acme/widgets/issues/42\n"

        return Result()

    monkeypatch.setattr("pollypm.task_backends.github._gh", fake_gh)

    task = backend.create_task(title="Wire the backend", body="Implement the gh-backed tracker.", state="03-needs-review")

    assert task.task_id == "42"
    assert task.title == "Wire the backend"
    assert task.state == "03-needs-review"
    assert ("issue", "create", "--title", "Wire the backend", "--body", "Implement the gh-backed tracker.", "--label", "polly:needs-review", "--repo", "acme/widgets") in calls


def test_github_task_backend_lists_tasks_for_requested_states(monkeypatch, tmp_path: Path) -> None:
    backend = GitHubTaskBackend(tmp_path, repo="acme/widgets")
    seen_labels: list[str] = []

    def fake_gh(*args: str, check: bool = True):
        label = args[args.index("--label") + 1]
        seen_labels.append(label)

        class Result:
            def __init__(self, stdout: str) -> None:
                self.stdout = stdout

        payloads = {
            "polly:ready": '[{"number":10,"title":"Spec the change","state":"OPEN"}]',
            "polly:needs-review": '[{"number":11,"title":"Review the change","state":"OPEN"}]',
        }
        return Result(payloads[label])

    monkeypatch.setattr("pollypm.task_backends.github._gh", fake_gh)

    tasks = backend.list_tasks(states=["01-ready", "03-needs-review"])

    assert seen_labels == ["polly:ready", "polly:needs-review"]
    assert [(task.task_id, task.title, task.state) for task in tasks] == [
        ("10", "Spec the change", "01-ready"),
        ("11", "Review the change", "03-needs-review"),
    ]
