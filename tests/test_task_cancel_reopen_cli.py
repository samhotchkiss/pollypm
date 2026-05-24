from __future__ import annotations

import os
from dataclasses import dataclass

from typer.testing import CliRunner

from pollypm.work import cli as work_cli
from pollypm.work.models import WorkStatus


@dataclass
class _Task:
    task_id: str
    work_status: WorkStatus
    assignee: str | None = None
    current_node_id: str | None = None

    @property
    def project(self) -> str:
        return self.task_id.split("/", 1)[0]

    @property
    def task_number(self) -> int:
        return int(self.task_id.split("/", 1)[1])


class _Service:
    def __init__(self, task: _Task) -> None:
        self.task = task
        self.cancel_calls: list[tuple[str, str, str]] = []
        self.reopen_calls: list[tuple[str, str, str | None]] = []

    def get(self, task_id: str) -> _Task:
        assert task_id == self.task.task_id
        return self.task

    def cancel(self, task_id: str, actor: str, reason: str) -> _Task:
        self.cancel_calls.append((task_id, actor, reason))
        self.task.work_status = WorkStatus.CANCELLED
        return self.task

    def reopen(
        self, task_id: str, actor: str, reason: str | None = None
    ) -> _Task:
        self.reopen_calls.append((task_id, actor, reason))
        self.task.work_status = WorkStatus.QUEUED
        self.task.assignee = None
        self.task.current_node_id = None
        return self.task


def test_cancel_in_progress_decline_does_not_cancel(monkeypatch) -> None:
    task = _Task("demo/1", WorkStatus.IN_PROGRESS, assignee="worker")
    service = _Service(task)
    events: list[dict] = []
    monkeypatch.setattr(work_cli, "_svc", lambda **_kw: service)
    monkeypatch.setattr("pollypm.audit.emit", lambda **kw: events.append(kw))

    result = CliRunner().invoke(
        work_cli.task_app,
        ["cancel", "demo/1", "--reason", "oops"],
        input="n\n",
    )

    assert result.exit_code == 1
    assert service.cancel_calls == []
    assert task.work_status is WorkStatus.IN_PROGRESS
    assert "Worker worker currently working this task" in result.output
    assert [event["event"] for event in events] == ["task.cancel.warned"]


def test_cancel_in_progress_noninteractive_without_input_exits_2(
    monkeypatch,
) -> None:
    task = _Task("demo/4", WorkStatus.IN_PROGRESS, assignee="worker")
    service = _Service(task)
    events: list[dict] = []
    monkeypatch.setattr(work_cli, "_svc", lambda **_kw: service)
    monkeypatch.setattr("pollypm.audit.emit", lambda **kw: events.append(kw))
    monkeypatch.setattr(work_cli, "_confirm_active_cancel", lambda _prompt: None)

    result = CliRunner().invoke(
        work_cli.task_app,
        ["cancel", "demo/4", "--reason", "oops"],
    )

    assert result.exit_code == 2
    assert service.cancel_calls == []
    assert task.work_status is WorkStatus.IN_PROGRESS
    assert "Refusing to cancel in_progress task non-interactively" in result.output
    assert "--force" in result.output
    assert [event["event"] for event in events] == ["task.cancel.warned"]


def test_cancel_confirmation_reads_piped_yes(monkeypatch) -> None:
    read_fd, write_fd = os.pipe()
    with os.fdopen(write_fd, "w", encoding="utf-8") as writer:
        writer.write("y\n")
    reader = os.fdopen(read_fd, "r", encoding="utf-8")
    monkeypatch.setattr(work_cli.sys, "stdin", reader)
    try:
        assert work_cli._confirm_active_cancel("Cancel?") is True
    finally:
        reader.close()


def test_cancel_confirmation_empty_pipe_returns_no_decision(monkeypatch) -> None:
    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    reader = os.fdopen(read_fd, "r", encoding="utf-8")
    monkeypatch.setattr(work_cli.sys, "stdin", reader)
    try:
        assert work_cli._confirm_active_cancel("Cancel?") is None
    finally:
        reader.close()


def test_cancel_in_progress_force_skips_prompt(monkeypatch) -> None:
    task = _Task("demo/2", WorkStatus.IN_PROGRESS, assignee="worker")
    service = _Service(task)
    events: list[dict] = []
    monkeypatch.setattr(work_cli, "_svc", lambda **_kw: service)
    monkeypatch.setattr("pollypm.audit.emit", lambda **kw: events.append(kw))

    result = CliRunner().invoke(
        work_cli.task_app,
        ["cancel", "demo/2", "--reason", "oops", "--force"],
    )

    assert result.exit_code == 0
    assert service.cancel_calls == [("demo/2", "cli", "oops")]
    assert "Cancel anyway" not in result.output
    assert [event["event"] for event in events] == ["task.cancel.confirmed"]
    assert events[0]["metadata"]["force"] is True


def test_reopen_command_calls_service(monkeypatch) -> None:
    task = _Task("demo/3", WorkStatus.CANCELLED, assignee="worker")
    service = _Service(task)
    monkeypatch.setattr(work_cli, "_svc", lambda **_kw: service)

    result = CliRunner().invoke(
        work_cli.task_app,
        ["reopen", "demo/3", "--reason", "undo"],
    )

    assert result.exit_code == 0
    assert service.reopen_calls == [("demo/3", "cli", "undo")]
    assert task.work_status is WorkStatus.QUEUED
    assert task.assignee is None
    assert "Reopened demo/3" in result.output
