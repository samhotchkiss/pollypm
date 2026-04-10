import json
from pathlib import Path

from pollypm.config import write_config
from pollypm.models import AccountConfig, PollyPMConfig, PollyPMSettings, ProjectSettings, ProviderKind, SessionConfig
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


def test_inbox_routing_and_handoff_flow(tmp_path: Path) -> None:
    config_path = _config(tmp_path)
    service = PollyPMService(config_path)
    service.load_supervisor().ensure_layout()
    inbox_root = tmp_path / ".pollypm" / "inbox"

    execution_item = service.create_inbox_item(
        sender="operator",
        subject="Deploy this to staging",
        body="Roll the current branch to staging and report back.",
    )
    launches = {launch.session.name: launch for launch in service.load_supervisor().plan_launches()}
    operator_prompt = launches["operator"].session.prompt or ""
    assert "Monitor `.pollypm/inbox/open/` continuously." in operator_prompt
    assert "Deploy this to staging [operator]" in operator_prompt

    execution_thread = service.triage_inbox_item(execution_item.name, actor="pm", owner="pm")
    decision = service.route_inbox_thread(execution_thread.thread_id, actor="pm")
    assert decision.owner == "pa"
    assert decision.state == "waiting-on-pa"
    assert decision.reason == "execution"

    handoff_path = inbox_root / "threads" / execution_thread.thread_id / "handoff.json"
    assert json.loads(handoff_path.read_text())["owner"] == "pa"

    returned = service.record_worker_reply_via_pa(
        execution_thread.thread_id,
        worker_name="worker_repo",
        subject="Staging deploy complete",
        body="The deploy finished and smoke checks passed.",
        actor="pa",
    )
    assert returned.state == "waiting-on-pm"
    assert returned.owner == "pm"
    assert json.loads(handoff_path.read_text())["owner"] == "pm"
    assert "Staging deploy complete" in returned.message_paths[-1].read_text()

    policy_item = service.create_inbox_item(
        sender="operator",
        subject="Should we change priority?",
        body="Should this jump ahead of the current plan?",
    )
    policy_thread = service.triage_inbox_item(policy_item.name, actor="pm", owner="pm")
    policy_decision = service.route_inbox_thread(policy_thread.thread_id, actor="pm")
    assert policy_decision.owner == "pm"
    assert policy_decision.state == "threaded"
    assert policy_decision.reason == "policy"
    policy_handoff = inbox_root / "threads" / policy_thread.thread_id / "handoff.json"
    assert json.loads(policy_handoff.read_text())["owner"] == "pm"
