"""End-to-end lifecycle test.

Tests the full lifecycle flow without requiring tmux or live providers:
config → project setup → transcript ingestion → knowledge extraction →
history import → checkpoint creation → recovery prompt → heartbeat cycle.

All operations use isolated temp directories with no production interference.
"""

import json
from pathlib import Path

from pollypm.capacity import (
    CapacityState,
    probe_all_accounts,
    select_failover_account,
)
from pollypm.checkpoints import (
    CheckpointData,
    create_level0_checkpoint,
    create_level1_checkpoint,
    create_level2_checkpoint,
    has_meaningful_work,
    load_canonical_checkpoint,
)
from pollypm.config import write_config
from pollypm.doc_backends import get_doc_backend
from pollypm.history_import import import_project_history
from pollypm.knowledge_extract import extract_knowledge_once
from pollypm.plugin_validate import validate_all_plugins
from pollypm.plugin_host import ExtensionHost
from pollypm.recovery.base import (
    SessionHealth,
    SessionSignals,
)
from pollypm.recovery.default import DefaultRecoveryPolicy
from pollypm.recovery_prompt import build_recovery_prompt
from pollypm.storage.state import StateStore

from tests.fixtures import (
    sample_config,
    sample_launch,
    sample_transcript_events,
    write_project_overview,
    write_transcript_events,
)


def test_full_project_lifecycle(tmp_path: Path) -> None:
    """End-to-end test: config → ingest → extract → checkpoint → recover → heartbeat."""
    config = sample_config(tmp_path, project_name="LifecycleProject")
    project_root = config.project.root_dir

    # 1. Write config
    config_path = tmp_path / "pollypm.toml"
    write_config(config, config_path, force=True)

    # 2. Set up project structure
    (project_root / "pyproject.toml").write_text("[project]\nname = 'lifecycle'\n")
    (project_root / "README.md").write_text("# LifecycleProject\n\nA test project.\n")

    # 3. Write transcript events
    events = sample_transcript_events()
    write_transcript_events(project_root, "session-a", events)

    # 4. Knowledge extraction (heuristic — no LLM needed)
    extract_result = extract_knowledge_once(config)
    assert extract_result["processed_events"] == 3
    assert extract_result["updated_docs"] >= 1

    # Verify docs were created
    assert (project_root / "docs" / "project-overview.md").exists()
    assert (project_root / "docs" / "decisions.md").exists()

    # 5. History import
    import_result = import_project_history(
        project_root,
        "LifecycleProject",
        skip_interview=True,
        timestamp="2026-04-10T00:00:00Z",
    )
    assert import_result.sources_found >= 2  # README + pyproject.toml + transcripts
    assert import_result.docs_generated == 5

    # 6. Doc backend reads
    doc_backend = get_doc_backend(project_root)
    docs = doc_backend.list_documents()
    assert len(docs) >= 5

    injection_context = doc_backend.get_injection_context()
    assert "LifecycleProject" in injection_context

    # 7. Checkpoint lifecycle: Level 0 → Level 1 → Level 2
    launch = sample_launch(config)

    l0_data, l0_artifact = create_level0_checkpoint(
        config, launch,
        snapshot_content="$ pytest -q\n10 passed in 1.5s\n$ git status\nM main.py\n",
    )
    assert l0_data.level == 0
    assert l0_data.test_results.get("passed") == 10

    # Meaningful work detected
    assert has_meaningful_work(l0_data, None) is True

    l1_data, l1_artifact = create_level1_checkpoint(
        config, launch,
        level0=l0_data,
        transcript_since_last="Implemented the core module and wrote tests.",
    )
    assert l1_data.level == 1
    assert l1_data.parent_checkpoint_id == l0_data.checkpoint_id

    l2_data, l2_artifact = create_level2_checkpoint(
        config, launch,
        level1=l1_data,
    )
    assert l2_data.level == 2
    assert l2_data.parent_checkpoint_id == l1_data.checkpoint_id

    # Canonical checkpoint is L2
    canonical = load_canonical_checkpoint(config, "worker", "test")
    assert canonical is not None
    assert canonical.checkpoint_id == l2_data.checkpoint_id

    # 8. Recovery prompt
    recovery = build_recovery_prompt(
        config, "worker", "test",
        task_prompt="Continue implementing the project",
    )
    rendered = recovery.render()
    assert "resuming" in rendered.lower()
    assert "LifecycleProject" in rendered

    # 9. Heartbeat classification (sealed recovery policy)
    store = StateStore(config.project.state_db)
    policy = DefaultRecoveryPolicy()
    signals = [
        SessionSignals(session_name="heartbeat", has_transcript_delta=True),
        SessionSignals(session_name="operator", has_transcript_delta=True),
        SessionSignals(session_name="worker", has_transcript_delta=True),
    ]
    classifications = {s.session_name: policy.classify(s) for s in signals}
    interventions = [
        policy.select_intervention(classifications[s.session_name], s, [])
        for s in signals
    ]
    assert len(classifications) == 3
    assert all(intervention is None for intervention in interventions)
    assert all(h == SessionHealth.ACTIVE for h in classifications.values())

    # 10. Capacity probes
    probes = probe_all_accounts(config, store)
    assert len(probes) == 2  # claude_main + claude_backup

    # 11. Plugin validation
    host = ExtensionHost(project_root)
    report = validate_all_plugins(host)
    assert report.all_passed


def test_failure_and_recovery_lifecycle(tmp_path: Path) -> None:
    """Test the failure → failover → recovery → heartbeat cycle."""
    config = sample_config(tmp_path)
    project_root = config.project.root_dir
    store = StateStore(config.project.state_db)

    # Set up a healthy state
    write_project_overview(project_root)

    # Create a checkpoint before failure
    launch = sample_launch(config)
    l0_data, _ = create_level0_checkpoint(
        config, launch,
        snapshot_content="$ pytest -q\n5 passed\n",
    )
    l1_data, _ = create_level1_checkpoint(
        config, launch,
        level0=l0_data,
    )

    # Simulate account exhaustion
    store.upsert_account_runtime(
        account_name="claude_main",
        provider="claude",
        status="capacity-exhausted",
        reason="rate limited",
    )
    store.upsert_account_usage(
        account_name="claude_backup",
        provider="claude",
        plan="max",
        health="healthy",
        usage_summary="80% left",
        raw_text="",
    )

    # Failover decision
    decision = select_failover_account(config, store, "claude_main")
    assert decision.should_failover
    assert decision.selected_account == "claude_backup"

    # Recovery prompt still works
    recovery = build_recovery_prompt(
        config, "worker", "test",
        task_prompt="Continue the work",
    )
    assert not recovery.is_fallback
    rendered = recovery.render()
    assert "resuming" in rendered.lower()

    # Heartbeat classifies the capacity issue and selects failover
    policy = DefaultRecoveryPolicy()
    signals = [
        SessionSignals(
            session_name="worker",
            capacity_state=CapacityState.EXHAUSTED,
        ),
    ]
    classifications = {s.session_name: policy.classify(s) for s in signals}
    interventions = [
        policy.select_intervention(classifications[s.session_name], s, [])
        for s in signals
    ]
    assert classifications["worker"] == SessionHealth.BLOCKED_NO_CAPACITY
    assert any(i is not None and i.action == "failover" for i in interventions)


def test_isolated_state_no_production_interference(tmp_path: Path) -> None:
    """Verify all test operations use isolated paths."""
    config = sample_config(tmp_path)
    project_root = config.project.root_dir

    # All paths should be under tmp_path
    assert str(project_root).startswith(str(tmp_path))
    assert str(config.project.state_db).startswith(str(tmp_path))
    assert str(config.project.base_dir).startswith(str(tmp_path))

    for account in config.accounts.values():
        if account.home:
            assert str(account.home).startswith(str(tmp_path))

    # Operations should not touch production state
    store = StateStore(config.project.state_db)
    store.upsert_session_runtime(session_name="test", status="healthy")
    runtime = store.get_session_runtime("test")
    assert runtime is not None
    assert runtime.status == "healthy"
