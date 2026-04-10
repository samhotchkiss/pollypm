import json
from pathlib import Path

from pollypm.config import write_config
from pollypm.models import AccountConfig, ProjectSettings, PollyPMConfig, PollyPMSettings, ProviderKind, SessionConfig
from pollypm.service_api import PollyPMService


def _config(tmp_path: Path) -> Path:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    config = PollyPMConfig(
        project=ProjectSettings(
            name="PollyPM",
            root_dir=project_root,
            base_dir=project_root / ".pollypm-state",
            logs_dir=project_root / ".pollypm-state/logs",
            snapshots_dir=project_root / ".pollypm-state/snapshots",
            state_db=project_root / ".pollypm-state/state.db",
        ),
        pollypm=PollyPMSettings(controller_account="claude_main"),
        accounts={
            "claude_main": AccountConfig(
                name="claude_main",
                provider=ProviderKind.CLAUDE,
                home=project_root / ".pollypm-state" / "homes" / "claude_main",
            )
        },
        sessions={
            "heartbeat": SessionConfig(
                name="heartbeat",
                role="heartbeat-supervisor",
                provider=ProviderKind.CLAUDE,
                account="claude_main",
                cwd=project_root,
            ),
            "operator": SessionConfig(
                name="operator",
                role="operator-pm",
                provider=ProviderKind.CLAUDE,
                account="claude_main",
                cwd=project_root,
            ),
        },
        projects={},
    )
    config_path = tmp_path / "pollypm.toml"
    write_config(config, config_path, force=True)
    return config_path


def test_service_inbox_state_machine_round_trip(tmp_path: Path) -> None:
    config_path = _config(tmp_path)
    service = PollyPMService(config_path)
    service.load_supervisor().ensure_layout()

    inbox_root = tmp_path / ".pollypm" / "inbox"
    assert (inbox_root / "open").exists()
    assert (inbox_root / "threads").exists()
    assert (inbox_root / "closed").exists()

    item_path = service.create_inbox_item(sender="pa", subject="Need review", body="Issue 021 is ready.")
    assert item_path.parent == inbox_root / "open"
    assert item_path.exists()
    assert [item.subject for item in service.list_inbox_items()] == ["Need review"]

    thread = service.triage_inbox_item(item_path.name, actor="pm", owner="pm")
    thread_root = inbox_root / "threads" / thread.thread_id
    assert thread_root.exists()
    assert (thread_root / "state.json").exists()
    assert (thread_root / "handoff.json").exists()

    state_payload = json.loads((thread_root / "state.json").read_text())
    assert state_payload["state"] == "threaded"
    assert state_payload["owner"] == "pm"
    assert state_payload["transitions"][0]["actor"] == "pm"

    service.append_inbox_thread_message(thread.thread_id, sender="pm", subject="Ack", body="Reviewing now.")
    handoff = service.handoff_inbox_thread(thread.thread_id, owner="pa", actor="pm", note="Need fix")
    assert handoff["owner"] == "pa"
    assert handoff["note"] == "Need fix"

    for state in ["waiting-on-pa", "waiting-on-pm", "resolved"]:
        service.transition_inbox_thread(thread.thread_id, state, actor="pm", note=state)
    closed = service.transition_inbox_thread(thread.thread_id, "closed", actor="pm", note="done")

    closed_root = inbox_root / "closed" / thread.thread_id
    assert closed.path == closed_root
    assert closed_root.exists()
    recovered = service.get_inbox_thread(thread.thread_id)
    assert recovered.state == "closed"
    assert len(recovered.message_paths) == 2

    closed_state = json.loads((closed_root / "state.json").read_text())
    assert [item["state"] for item in closed_state["transitions"]] == [
        "threaded",
        "waiting-on-pa",
        "waiting-on-pm",
        "resolved",
        "closed",
    ]
    assert closed_state["closed_at"]
    assert json.loads((closed_root / "handoff.json").read_text())["owner"] == "pa"
