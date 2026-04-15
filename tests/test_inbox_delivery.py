from pathlib import Path

from pollypm.inbox_delivery import _mail_command, deliver_single_message, ensure_inbox_progress
from pollypm.inbox_v2 import create_message, read_message
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


def _config(tmp_path: Path) -> PollyPMConfig:
    (tmp_path / "pollypm.toml").write_text("[project]\nname = 'PollyPM'\n")
    return PollyPMConfig(
        project=ProjectSettings(
            root_dir=tmp_path,
            base_dir=tmp_path / ".pollypm-state",
            logs_dir=tmp_path / ".pollypm-state/logs",
            snapshots_dir=tmp_path / ".pollypm-state/snapshots",
            state_db=tmp_path / ".pollypm-state/state.db",
        ),
        pollypm=PollyPMSettings(controller_account="claude_controller"),
        accounts={
            "claude_controller": AccountConfig(
                name="claude_controller",
                provider=ProviderKind.CLAUDE,
                home=tmp_path / ".pollypm-state/homes/claude_controller",
            )
        },
        sessions={
            "operator": SessionConfig(
                name="operator",
                role="operator-pm",
                provider=ProviderKind.CLAUDE,
                account="claude_controller",
                cwd=tmp_path,
            )
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


def test_mail_command_uses_explicit_config_path(tmp_path: Path) -> None:
    config = _config(tmp_path)

    assert _mail_command(config) == f"pm --config {tmp_path / 'pollypm.toml'} mail"
    assert _mail_command(config, "msg-123") == f"pm --config {tmp_path / 'pollypm.toml'} mail msg-123"


def test_ensure_inbox_progress_poke_includes_explicit_config(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    create_message(
        tmp_path,
        sender="heartbeat",
        subject="Needs attention",
        body="Check inbox",
        owner="polly",
        to="polly",
    )
    sent: list[tuple[str, str, str, bool]] = []

    class FakeSupervisor:
        def __init__(self, _config: PollyPMConfig) -> None:
            self.store = type("Store", (), {"close": lambda self: None})()

        def send_input(self, session_name: str, text: str, owner: str = "human", force: bool = False) -> None:
            sent.append((session_name, text, owner, force))

    class FakeRuntime:
        status = "idle"

    class FakeQuery:
        def fetchall(self) -> list[tuple[str]]:
            return []

    class FakeStateStore:
        def __init__(self, _path: Path) -> None:
            pass

        def get_session_runtime(self, _session_name: str):
            return FakeRuntime()

        def execute(self, _query: str, _params: tuple[str, ...]) -> FakeQuery:
            return FakeQuery()

        def last_event_at(self, _session_name: str, _event_type: str):
            return None

        def record_event(self, _session_name: str, _event_type: str, _details: str) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr("pollypm.supervisor.Supervisor", FakeSupervisor)
    monkeypatch.setattr("pollypm.storage.state.StateStore", FakeStateStore)

    counts = ensure_inbox_progress(config)

    assert counts == {"poked": 1, "active": 0, "skipped": 0}
    assert len(sent) == 1
    assert sent[0][0] == "operator"
    assert sent[0][2:] == ("pollypm", True)
    assert sent[0][1] == f"[Inbox] You have 1 item(s) needing action. Run: pm --config {tmp_path / 'pollypm.toml'} mail"


def test_deliver_single_message_includes_explicit_config(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    msg = create_message(
        tmp_path,
        sender="heartbeat",
        subject="Operator session back online",
        body="FYI",
        owner="polly",
        to="polly",
    )
    sent: list[tuple[str, str, str, bool]] = []

    class FakeSupervisor:
        def __init__(self, _config: PollyPMConfig) -> None:
            self.store = type("Store", (), {"close": lambda self: None})()

        def send_input(self, session_name: str, text: str, owner: str = "human", force: bool = False) -> None:
            sent.append((session_name, text, owner, force))

    monkeypatch.setattr("pollypm.supervisor.Supervisor", FakeSupervisor)

    assert deliver_single_message(config, msg.id) is True
    assert sent == [
        (
            "operator",
            f"[Inbox] New message: Operator session back online — run: pm --config {tmp_path / 'pollypm.toml'} mail {msg.id}",
            "pollypm",
            True,
        )
    ]

    _ctx, _hist, _entries = read_message(tmp_path, msg.id)
