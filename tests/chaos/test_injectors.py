from __future__ import annotations

import pytest

from scripts.chaos.injectors import (
    assert_non_ambient_pg_dsn,
    assert_sandbox_name,
    run_failover_injector,
    run_session_kill_injector,
    run_task_stall_injector,
)


def test_safety_rejects_non_sandbox_names() -> None:
    with pytest.raises(ValueError, match="not sandbox-scoped"):
        assert_sandbox_name("worker-production", field="session_name")


def test_safety_rejects_ambient_operator_pg_dsn() -> None:
    with pytest.raises(ValueError, match="ambient local pollypm DB"):
        assert_non_ambient_pg_dsn("postgresql://localhost:5432/pollypm")


@pytest.mark.parametrize("mode", ["auth_broken", "capacity_exhausted"])
def test_failover_injector_trips_heartbeat_recovery_path(mode: str) -> None:
    result = run_failover_injector(mode=mode)

    assert result["passed"] is True, result
    assert result["safety"]["real_account_touched"] is False
    assert result["post_recovery"]["session_status"] == mode
    assert result["post_recovery"]["recoveries"][0]["failure_type"] == mode
    assert result["mapped_rule"]["policy"].endswith(
        "DefaultRecoveryPolicy.classify/select_intervention"
    )


def test_session_kill_injector_maps_to_role_session_missing() -> None:
    result = run_session_kill_injector()

    assert result["passed"] is True, result
    assert result["safety"]["tmux_touched"] is False
    assert result["mapped_rule"]["detector"].endswith(
        "._detect_role_session_missing"
    )
    findings = result["pre_recovery"]["findings"]
    assert findings[0]["rule"] == "role_session_missing"
    assert findings[0]["metadata"]["expected_window"] == "worker-chaos-sandbox"
    assert result["post_recovery"]["findings"] == []


def test_task_stall_injector_ages_pg_fixture_and_releases_stale_claim(
    pg_work_service,
    pg_schema_pool,
) -> None:
    result = run_task_stall_injector(
        work_service=pg_work_service,
        pg_pool=pg_schema_pool,
    )

    assert result["passed"] is True, result
    assert result["mapped_rule"]["detector"].endswith(
        "._detect_task_progress_stale"
    )
    findings = result["pre_recovery"]["findings"]
    assert findings[0]["rule"] == "task_progress_stale"
    assert findings[0]["metadata"]["detected_via"] == "state"
    assert result["pre_recovery"]["task"]["status"] == "in_progress"
    assert result["post_recovery"]["task"]["status"] == "queued"
    assert result["post_recovery"]["findings"] == []
