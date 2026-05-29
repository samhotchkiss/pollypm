"""Tests for the web alert drill-down endpoint."""

from __future__ import annotations


def test_list_alerts_filters_operational_and_includes_actions(
    client,
    auth_headers,
    pg_schema_pool,
) -> None:
    from pollypm.storage.pg_alerts import upsert_alert
    from pollypm.storage.pg_migrations import apply_migrations

    apply_migrations(pg_schema_pool)
    upsert_alert(
        "worker_demo",
        "auth_broken",
        "error",
        "Codex auth failed",
        pool=pg_schema_pool,
    )
    upsert_alert(
        "worker_demo",
        "suspected_loop",
        "warn",
        "Heartbeat saw repeated output",
        pool=pg_schema_pool,
    )

    response = client.get("/api/v1/alerts", headers=auth_headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    alert = body["alerts"][0]
    assert alert["session_name"] == "worker_demo"
    assert alert["alert_type"] == "auth_broken"
    assert alert["channel"] == "action_required"
    assert [action["kind"] for action in alert["actions"]] == ["acknowledge"]

    response = client.get(
        "/api/v1/alerts?include_operational=true",
        headers=auth_headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["total"] == 2

    response = client.get(
        "/api/v1/alerts?include_operational=true&limit=1",
        headers=auth_headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 2
    assert len(body["alerts"]) == 1


def test_acknowledge_alert_action_closes_alert(
    client,
    auth_headers,
    pg_schema_pool,
) -> None:
    from pollypm.storage.pg_alerts import open_alerts, upsert_alert
    from pollypm.storage.pg_migrations import apply_migrations

    apply_migrations(pg_schema_pool)
    upsert_alert(
        "worker_demo",
        "auth_broken",
        "error",
        "Codex auth failed",
        pool=pg_schema_pool,
    )
    alert = open_alerts(pool=pg_schema_pool)[0]

    response = client.post(
        f"/api/v1/alerts/{alert.alert_id}/actions/acknowledge",
        headers=auth_headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["alert_id"] == alert.alert_id
    assert body["status"] == "closed"
    assert open_alerts(pool=pg_schema_pool) == []


def test_alert_action_rejects_unsupported_kind(
    client,
    auth_headers,
    pg_schema_pool,
) -> None:
    from pollypm.storage.pg_alerts import open_alerts, upsert_alert
    from pollypm.storage.pg_migrations import apply_migrations

    apply_migrations(pg_schema_pool)
    upsert_alert(
        "worker_demo",
        "auth_broken",
        "error",
        "Codex auth failed",
        pool=pg_schema_pool,
    )
    alert = open_alerts(pool=pg_schema_pool)[0]

    response = client.post(
        f"/api/v1/alerts/{alert.alert_id}/actions/restart_session",
        headers=auth_headers,
    )

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "invalid_request"
