import os
from pathlib import Path

from pollypm.storage.state import TokenUsageHourlyRecord
from pollypm.storage.state import StateStore


def test_state_store_alerts_leases_and_heartbeats(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    for table in ("events", "alerts", "task_notifications"):
        assert store.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone() is None
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


def test_state_store_daily_token_usage_aggregates_by_day(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.replace_token_usage_hourly(
        [
            TokenUsageHourlyRecord(
                hour_bucket="2026-04-10T08:00:00+00:00",
                account_name="claude_primary",
                provider="claude",
                model_name="Opus",
                project_key="pollypm",
                tokens_used=100,
                updated_at="2026-04-10T08:05:00+00:00",
            ),
            TokenUsageHourlyRecord(
                hour_bucket="2026-04-10T09:00:00+00:00",
                account_name="codex_primary",
                provider="openai",
                model_name="gpt-5.4",
                project_key="demo",
                tokens_used=250,
                updated_at="2026-04-10T09:05:00+00:00",
            ),
            TokenUsageHourlyRecord(
                hour_bucket="2026-04-11T09:00:00+00:00",
                account_name="claude_primary",
                provider="claude",
                model_name="Opus",
                project_key="pollypm",
                tokens_used=75,
                updated_at="2026-04-11T09:05:00+00:00",
            ),
        ]
    )

    assert store.daily_token_usage(days=30) == [
        ("2026-04-10", 350),
        ("2026-04-11", 75),
    ]


def test_state_store_readonly_mode_reads_existing_data(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    store = StateStore(db_path)
    store.upsert_alert("worker", "idle_output", "warn", "No new output")
    store.set_lease("worker", "human", "manual takeover")
    store.upsert_session_runtime(
        session_name="worker",
        status="idle",
        last_failure_message="Waiting for input",
    )
    store.close()

    os.chmod(tmp_path, 0o555)
    os.chmod(db_path, 0o444)
    try:
        readonly_store = StateStore(db_path, readonly=True)
        alerts = readonly_store.open_alerts()
        lease = readonly_store.get_lease("worker")
        runtime = readonly_store.get_session_runtime("worker")
        readonly_store.close()
    finally:
        os.chmod(db_path, 0o644)
        os.chmod(tmp_path, 0o755)

    assert len(alerts) == 1
    assert alerts[0].alert_type == "idle_output"
    assert lease is not None
    assert lease.owner == "human"
    assert runtime is not None
    assert runtime.status == "idle"


def test_memory_entry_tags_survives_corrupt_non_list_value(tmp_path: Path) -> None:
    """Cycle 109 — ``MemoryEntryRecord.tags`` was decoded with
    ``tuple(json.loads(row[5] or "[]"))``. A row whose tags column
    parsed to a dict would yield ``tuple(dict)`` (the keys) and a
    string would yield the characters — both subtle data-corruption
    bugs. Defend with ``_safe_tags`` so non-list shapes coerce to
    ``()``.
    """
    db_path = tmp_path / "state.db"
    store = StateStore(db_path)
    record = store.record_memory_entry(
        scope="demo",
        kind="note",
        title="seed",
        body="b",
        tags=["a", "b"],
        source="test",
        file_path="x",
        summary_path="y",
    )
    # Hand-corrupt the persisted tags column to a dict.
    store.execute(
        "UPDATE memory_entries SET tags = ? WHERE id = ?",
        ('{"oops": 1}', record.entry_id),
    )
    fetched = store.get_memory_entry(record.entry_id)
    assert fetched is not None
    # Without the fix this would have been ('oops',) — the dict's keys.
    assert fetched.tags == ()
    store.close()


def test_open_alerts_survives_corrupt_non_dict_payload(tmp_path: Path) -> None:
    """Cycle 108 — ``open_alerts`` did ``json.loads(row[3])`` then
    ``payload.get("severity")``. A messages row whose ``payload_json``
    parsed to a non-dict (list/null/string) would AttributeError out
    of the loop and prevent the dashboard from loading any alerts.
    Defend with the new ``_safe_payload`` helper."""
    db_path = tmp_path / "state.db"
    store = StateStore(db_path)
    store.upsert_session(
        name="worker",
        role="worker",
        project="demo",
        provider="codex",
        account="codex_primary",
        cwd=str(tmp_path),
        window_name="worker",
    )
    store.upsert_alert("worker", "idle_output", "warn", "No new output")
    # Hand-corrupt the persisted payload to a JSON list — simulate a
    # legacy or hand-edited DB row.
    store.execute(
        "UPDATE messages SET payload_json = ? WHERE type = 'alert'",
        ('[1, 2, 3]',),
    )
    alerts = store.open_alerts()
    # Without the fix this raised AttributeError. With the fix the row
    # comes back with severity coerced to "" (empty payload).
    assert len(alerts) == 1
    assert alerts[0].alert_type == "idle_output"
    assert alerts[0].severity == ""
    store.close()
