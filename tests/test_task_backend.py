from pathlib import Path

from promptmaster.task_backends import FileTaskBackend, get_task_backend


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
