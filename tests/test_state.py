from pathlib import Path

from pollypm.storage.state import StateStore


def test_state_store_alerts_leases_and_heartbeats(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.upsert_session(
        name="worker",
        role="worker",
        project="demo-project",
        provider="codex",
        account="codex_primary",
        cwd=str(tmp_path),
        window_name="worker",
    )

    store.record_heartbeat(
        session_name="worker",
        tmux_window="worker",
        pane_id="%1",
        pane_command="codex",
        pane_dead=False,
        log_bytes=123,
        snapshot_path=str(tmp_path / "snapshot.txt"),
        snapshot_hash="abc123",
    )
    heartbeat = store.latest_heartbeat("worker")
    assert heartbeat is not None
    assert heartbeat.log_bytes == 123
    assert heartbeat.snapshot_hash == "abc123"

    store.upsert_alert("worker", "idle_output", "warn", "No new output")
    alerts = store.open_alerts()
    assert len(alerts) == 1
    assert alerts[0].alert_type == "idle_output"

    store.clear_alert("worker", "idle_output")
    assert store.open_alerts() == []

    store.set_lease("worker", "human", "manual takeover")
    lease = store.get_lease("worker")
    assert lease is not None
    assert lease.owner == "human"

    store.clear_lease("worker")
    assert store.get_lease("worker") is None


def test_state_store_records_hourly_token_usage_deltas(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")

    first = store.record_token_sample(
        session_name="operator",
        account_name="claude_primary",
        provider="claude",
        model_name="Opus 4.6 (1M context)",
        project_key="pollypm",
        cumulative_tokens=2000,
    )
    second = store.record_token_sample(
        session_name="operator",
        account_name="claude_primary",
        provider="claude",
        model_name="Opus 4.6 (1M context)",
        project_key="pollypm",
        cumulative_tokens=2600,
    )

    assert first == 0
    assert second == 600
    usage = store.recent_token_usage(limit=5)
    assert len(usage) == 1
    assert usage[0].account_name == "claude_primary"
    assert usage[0].model_name == "Opus 4.6 (1M context)"
    assert usage[0].tokens_used == 600
