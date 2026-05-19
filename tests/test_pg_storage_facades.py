"""pg-parity tests for the read-side ``storage/*`` facades (#1737, Slice C).

For each facade we seed the pg test schema with the same shape its
sqlite counterpart would carry, then call the facade with
``config.storage.backend = "postgres"`` and assert the same return
shape as the sqlite branch. The ``pg_schema_pool`` fixture (from
``tests/conftest_pg.py``) creates a fresh schema namespace per test.

Skipped when neither Docker nor a local pg with the ``vector``
extension is available — see the conftest_pg docstring.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest


# --------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------- #


@dataclass(slots=True)
class _StubStorage:
    backend: str = "postgres"


@dataclass(slots=True)
class _StubConfig:
    """Minimal config stub the facades treat as a real PollyPMConfig."""

    storage: _StubStorage = field(default_factory=_StubStorage)


@pytest.fixture()
def pg_config(pg_schema_pool):  # noqa: ARG001 — fixture just needs to run first
    """Return a stub config that signals the pg backend is active."""
    return _StubConfig()


def _seed_work_tasks(pg_schema_pool, rows: list[dict]) -> None:
    """Insert minimal ``work_tasks`` rows for a facade test."""
    defaults = {
        "title": "",
        "type": "task",
        "labels": "[]",
        "work_status": "draft",
        "flow_template_id": "default",
        "flow_template_version": 1,
        "current_node_id": None,
        "assignee": None,
        "priority": "normal",
        "requires_human_review": False,
        "description": "",
        "acceptance_criteria": None,
        "constraints": None,
        "relevant_files": "[]",
        "parent_project": None,
        "parent_task_number": None,
        "supersedes_project": None,
        "supersedes_task_number": None,
        "plan_version": 1,
        "predecessor_task_id": None,
        "kind": "legacy",
        "roles": "{}",
        "external_refs": "{}",
        "created_by": "test",
    }
    with pg_schema_pool.connection() as conn, conn.cursor() as cur:
        for row in rows:
            payload = {**defaults, **row}
            payload.setdefault("project_key", payload["project"])
            cur.execute(
                "INSERT INTO work_tasks ("
                "project, task_number, project_key, title, type, labels, "
                "work_status, flow_template_id, flow_template_version, "
                "current_node_id, assignee, priority, requires_human_review, "
                "description, acceptance_criteria, constraints, relevant_files, "
                "parent_project, parent_task_number, "
                "supersedes_project, supersedes_task_number, "
                "plan_version, predecessor_task_id, kind, "
                "roles, external_refs, created_at, created_by, updated_at"
                ") VALUES ("
                " %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, "
                " %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, "
                " %s::jsonb, %s::jsonb, now(), %s, now()"
                ")",
                (
                    payload["project"],
                    payload["task_number"],
                    payload["project_key"],
                    payload["title"],
                    payload["type"],
                    payload["labels"],
                    payload["work_status"],
                    payload["flow_template_id"],
                    payload["flow_template_version"],
                    payload["current_node_id"],
                    payload["assignee"],
                    payload["priority"],
                    payload["requires_human_review"],
                    payload["description"],
                    payload["acceptance_criteria"],
                    payload["constraints"],
                    payload["relevant_files"],
                    payload["parent_project"],
                    payload["parent_task_number"],
                    payload["supersedes_project"],
                    payload["supersedes_task_number"],
                    payload["plan_version"],
                    payload["predecessor_task_id"],
                    payload["kind"],
                    payload["roles"],
                    payload["external_refs"],
                    payload["created_by"],
                ),
            )
        conn.commit()


def _apply_initial_migrations(pg_schema_pool) -> None:
    """Apply the canonical migration so the schema port lands on the pool."""
    from pollypm.storage.pg_migrations import apply_migrations

    apply_migrations(pg_schema_pool)


# --------------------------------------------------------------------- #
# work_session_queries — aggregate Tokens line for the project dashboard
# --------------------------------------------------------------------- #


def test_pg_aggregate_project_session_tokens(pg_schema_pool, pg_config, tmp_path):
    """The pg branch must return the same (sum_in, sum_out) tuple shape."""
    _apply_initial_migrations(pg_schema_pool)
    _seed_work_tasks(pg_schema_pool, [
        {"project": "alpha", "task_number": 1},
        {"project": "alpha", "task_number": 2},
        {"project": "beta", "task_number": 1},
    ])
    with pg_schema_pool.connection() as conn, conn.cursor() as cur:
        for project, num, tin, tout in [
            ("alpha", 1, 100, 50),
            ("alpha", 2, 200, 25),
            ("beta", 1, 999, 999),
        ]:
            cur.execute(
                "INSERT INTO work_sessions ("
                "task_project, task_number, agent_name, started_at, "
                "total_input_tokens, total_output_tokens"
                ") VALUES (%s, %s, %s, now(), %s, %s)",
                (project, num, "worker", tin, tout),
            )
        conn.commit()

    from pollypm.storage.work_session_queries import (
        aggregate_project_session_tokens,
    )

    # db_path is ignored on the pg branch — pass a bogus path to prove it.
    result = aggregate_project_session_tokens(
        tmp_path / "ignored.db",
        project_key="alpha",
        config=pg_config,
    )
    assert result == (300, 75)


def test_pg_aggregate_empty_returns_zero_tuple(pg_schema_pool, pg_config, tmp_path):
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.work_session_queries import (
        aggregate_project_session_tokens,
    )

    result = aggregate_project_session_tokens(
        tmp_path / "missing.db",
        project_key="alpha",
        config=pg_config,
    )
    # Empty table → (0, 0), not None (matches sqlite empty-table parity).
    assert result == (0, 0)


# --------------------------------------------------------------------- #
# doctor_state_probes
# --------------------------------------------------------------------- #


def test_pg_applied_schema_version_returns_max(pg_schema_pool, pg_config, tmp_path):
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.doctor_state_probes import applied_schema_version_ro

    # Both the sqlite ``schema_version`` and ``work_schema_version`` map
    # to the unified ``schema_migrations`` table on pg. The probe returns
    # ``max(version)``, so this tracks whatever the head migration is.
    from pollypm.storage.pg_schema import MIGRATIONS

    expected_head = max(v for v, _, _ in MIGRATIONS)
    assert applied_schema_version_ro(
        tmp_path / "x.db", "schema_version", config=pg_config,
    ) == expected_head
    assert applied_schema_version_ro(
        tmp_path / "x.db", "work_schema_version", config=pg_config,
    ) == expected_head


def test_pg_count_work_tasks(pg_schema_pool, pg_config, tmp_path):
    _apply_initial_migrations(pg_schema_pool)
    _seed_work_tasks(pg_schema_pool, [
        {"project": "alpha", "task_number": 1},
        {"project": "alpha", "task_number": 2},
    ])
    from pollypm.storage.doctor_state_probes import count_work_tasks_ro

    assert count_work_tasks_ro(tmp_path / "x.db", config=pg_config) == 2


def test_pg_has_messages_table(pg_schema_pool, pg_config, tmp_path):
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.doctor_state_probes import has_messages_table_ro

    assert has_messages_table_ro(tmp_path / "x.db", config=pg_config) is True


def test_pg_sessions_row_count(pg_schema_pool, pg_config, tmp_path):
    _apply_initial_migrations(pg_schema_pool)
    with pg_schema_pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sessions ("
            "name, role, project, provider, account, cwd, window_name"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s)",
            ("sess-1", "worker", "alpha", "claude", "default", "/tmp", "win"),
        )
        conn.commit()
    from pollypm.storage.doctor_state_probes import sessions_row_count_ro

    assert sessions_row_count_ro(tmp_path / "x.db", config=pg_config) == 1


def test_pg_session_window_names(pg_schema_pool, pg_config, tmp_path):
    _apply_initial_migrations(pg_schema_pool)
    with pg_schema_pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sessions ("
            "name, role, project, provider, account, cwd, window_name"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s)",
            ("sess-1", "worker", "alpha", "claude", "default", "/tmp", "win-a"),
        )
        cur.execute(
            "INSERT INTO sessions ("
            "name, role, project, provider, account, cwd, window_name"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s)",
            ("sess-2", "worker", "alpha", "claude", "default", "/tmp", "win-b"),
        )
        conn.commit()
    from pollypm.storage.doctor_state_probes import session_window_names_ro

    windows = session_window_names_ro(tmp_path / "x.db", config=pg_config)
    assert windows == {"win-a", "win-b"}


# --------------------------------------------------------------------- #
# work_task_state
# --------------------------------------------------------------------- #


def test_pg_task_status_probe_found(pg_schema_pool, pg_config, tmp_path):
    _apply_initial_migrations(pg_schema_pool)
    _seed_work_tasks(pg_schema_pool, [
        {"project": "alpha", "task_number": 7, "work_status": "in_progress"},
    ])
    from pollypm.storage.work_task_state import task_status_probe

    found, status = task_status_probe(
        project_key="alpha",
        task_number=7,
        project_path=tmp_path,
        config=pg_config,
    )
    assert found is True
    assert status == "in_progress"


def test_pg_task_status_probe_missing(pg_schema_pool, pg_config, tmp_path):
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.work_task_state import task_status_probe

    found, status = task_status_probe(
        project_key="alpha",
        task_number=999,
        project_path=tmp_path,
        config=pg_config,
    )
    # The pg branch reports the DB is reachable even when the row is
    # missing — same shape as the sqlite branch when it found a candidate
    # state.db but no matching row.
    assert found is True
    assert status is None


def test_pg_task_numbers_with_statuses(pg_schema_pool, pg_config, tmp_path):
    _apply_initial_migrations(pg_schema_pool)
    _seed_work_tasks(pg_schema_pool, [
        {"project": "alpha", "task_number": 1, "work_status": "queued"},
        {"project": "alpha", "task_number": 2, "work_status": "in_progress"},
        {"project": "alpha", "task_number": 3, "work_status": "done"},
        {"project": "beta", "task_number": 1, "work_status": "queued"},
    ])
    from pollypm.storage.work_task_state import task_numbers_with_statuses

    result = task_numbers_with_statuses(
        project_key="alpha",
        project_path=tmp_path,
        statuses=("queued", "in_progress"),
        config=pg_config,
    )
    assert result == [1, 2]


def test_pg_project_task_total_fast(pg_schema_pool, pg_config, tmp_path):
    _apply_initial_migrations(pg_schema_pool)
    _seed_work_tasks(pg_schema_pool, [
        {"project": "alpha", "task_number": 1},
        {"project": "alpha", "task_number": 2},
        {"project": "beta", "task_number": 1},
    ])
    from pollypm.storage.work_task_state import project_task_total_fast

    assert project_task_total_fast(
        tmp_path / "x.db",
        project_key="alpha",
        config=pg_config,
    ) == 2


def test_pg_has_work_task_rows(pg_schema_pool, pg_config, tmp_path):
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.work_task_state import has_work_task_rows

    assert has_work_task_rows(
        tmp_path / "x.db", project_key="alpha", config=pg_config,
    ) is False
    _seed_work_tasks(pg_schema_pool, [
        {"project": "alpha", "task_number": 1},
    ])
    assert has_work_task_rows(
        tmp_path / "x.db", project_key="alpha", config=pg_config,
    ) is True
    assert has_work_task_rows(
        tmp_path / "x.db", project_key="beta", config=pg_config,
    ) is False


def test_pg_project_activity_probe(pg_schema_pool, pg_config, tmp_path):
    _apply_initial_migrations(pg_schema_pool)
    _seed_work_tasks(pg_schema_pool, [
        {"project": "alpha", "task_number": 1, "work_status": "in_progress"},
    ])
    from pollypm.storage.work_task_state import project_activity_probe

    is_active, has_working = project_activity_probe(
        project_key="alpha",
        project_path=tmp_path,
        cutoff_iso="2000-01-01T00:00:00",
        config=pg_config,
    )
    assert has_working is True
    assert is_active is True


# --------------------------------------------------------------------- #
# work_transition_queries
# --------------------------------------------------------------------- #


def _insert_transition(
    pool,
    *,
    project: str,
    task_number: int,
    from_state: str,
    to_state: str,
    actor: str = "test",
    created_at: str | None = None,
) -> None:
    with pool.connection() as conn, conn.cursor() as cur:
        if created_at is None:
            cur.execute(
                "INSERT INTO work_transitions ("
                "task_project, task_number, from_state, to_state, "
                "actor, reason, created_at"
                ") VALUES (%s, %s, %s, %s, %s, %s, now())",
                (project, task_number, from_state, to_state, actor, None),
            )
        else:
            cur.execute(
                "INSERT INTO work_transitions ("
                "task_project, task_number, from_state, to_state, "
                "actor, reason, created_at"
                ") VALUES (%s, %s, %s, %s, %s, %s, %s::timestamptz)",
                (project, task_number, from_state, to_state, actor, None, created_at),
            )
        conn.commit()


def test_pg_advisor_transition_rows(pg_schema_pool, pg_config, tmp_path):
    _apply_initial_migrations(pg_schema_pool)
    _seed_work_tasks(pg_schema_pool, [
        {"project": "alpha", "task_number": 1, "title": "T1"},
    ])
    _insert_transition(
        pg_schema_pool,
        project="alpha", task_number=1,
        from_state="draft", to_state="queued",
        created_at="2025-01-01T12:00:00+00:00",
    )
    from pollypm.storage.work_transition_queries import advisor_transition_rows

    rows = advisor_transition_rows(
        tmp_path / "x.db",
        project_key="alpha",
        since_iso="2024-01-01",
        config=pg_config,
    )
    assert len(rows) == 1
    assert rows[0]["project"] == "alpha"
    assert rows[0]["task_number"] == 1
    assert rows[0]["from_state"] == "draft"
    assert rows[0]["to_state"] == "queued"
    assert rows[0]["title"] == "T1"


def test_pg_activity_feed_transition_rows(pg_schema_pool, pg_config, tmp_path):
    _apply_initial_migrations(pg_schema_pool)
    _seed_work_tasks(pg_schema_pool, [
        {"project": "alpha", "task_number": 1},
    ])
    _insert_transition(
        pg_schema_pool,
        project="alpha", task_number=1,
        from_state="draft", to_state="queued",
    )
    from pollypm.storage.work_transition_queries import (
        activity_feed_transition_rows,
    )

    rows = activity_feed_transition_rows(
        tmp_path / "x.db",
        since_ts=None,
        limit=10,
        config=pg_config,
    )
    assert len(rows) == 1
    assert rows[0]["task_project"] == "alpha"
    assert rows[0]["task_number"] == 1


# --------------------------------------------------------------------- #
# morning_briefing_queries
# --------------------------------------------------------------------- #


def test_pg_briefing_transition_rows(pg_schema_pool, pg_config):
    _apply_initial_migrations(pg_schema_pool)
    _seed_work_tasks(pg_schema_pool, [
        {"project": "alpha", "task_number": 1, "title": "T1"},
    ])
    _insert_transition(
        pg_schema_pool,
        project="alpha", task_number=1,
        from_state="draft", to_state="queued",
        created_at="2025-01-02T12:00:00+00:00",
    )
    _insert_transition(
        pg_schema_pool,
        project="alpha", task_number=1,
        from_state="queued", to_state="done",
        created_at="2025-01-05T12:00:00+00:00",
    )
    from pollypm.storage.morning_briefing_queries import transition_rows

    rows = transition_rows(
        [],
        project_key="alpha",
        since_iso="2025-01-01",
        until_iso="2025-01-03",
        config=pg_config,
    )
    # Window is half-open [since, until) — only the Jan 2 transition.
    assert len(rows) == 1
    assert rows[0]["to_state"] == "queued"


def test_pg_briefing_priority_task_rows(pg_schema_pool, pg_config):
    _apply_initial_migrations(pg_schema_pool)
    _seed_work_tasks(pg_schema_pool, [
        {"project": "alpha", "task_number": 1, "title": "low", "priority": "low",
         "work_status": "queued"},
        {"project": "alpha", "task_number": 2, "title": "crit",
         "priority": "critical", "work_status": "queued"},
        {"project": "alpha", "task_number": 3, "title": "done",
         "priority": "high", "work_status": "done"},
    ])
    from pollypm.storage.morning_briefing_queries import priority_task_rows

    rows = priority_task_rows(
        [],
        project_key="alpha",
        open_statuses=("queued", "in_progress", "blocked", "awaiting_approval"),
        limit=10,
        config=pg_config,
    )
    titles = [r["title"] for r in rows]
    assert "crit" in titles
    assert "low" in titles
    assert "done" not in titles
    # Critical sorts before low.
    assert titles.index("crit") < titles.index("low")


def test_pg_briefing_blocker_rows(pg_schema_pool, pg_config):
    _apply_initial_migrations(pg_schema_pool)
    _seed_work_tasks(pg_schema_pool, [
        {"project": "alpha", "task_number": 1, "title": "blocker",
         "work_status": "queued"},
        {"project": "alpha", "task_number": 2, "title": "blocked",
         "work_status": "blocked"},
    ])
    with pg_schema_pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO work_task_dependencies ("
            "from_project, from_task_number, to_project, to_task_number, "
            "kind, created_at"
            ") VALUES (%s, %s, %s, %s, %s, now())",
            ("alpha", 2, "alpha", 1, "blocks"),
        )
        conn.commit()
    from pollypm.storage.morning_briefing_queries import blocker_rows

    rows = blocker_rows([], project_key="alpha", config=pg_config)
    assert len(rows) == 1
    assert rows[0]["task_number"] == 2
    assert rows[0]["blocked_by"] == ["alpha/1"]
    assert rows[0]["unresolved_blockers"] == ["alpha/1"]


# --------------------------------------------------------------------- #
# inbox_action_preview
# --------------------------------------------------------------------- #


def test_pg_inbox_action_preview(pg_schema_pool, pg_config, tmp_path):
    _apply_initial_migrations(pg_schema_pool)
    with pg_schema_pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO messages ("
            "scope, project_key, type, tier, recipient, sender, state, "
            "subject, body, payload_json, labels, kind"
            ") VALUES ("
            " %s, %s, %s, %s, %s, %s, %s, "
            " %s, %s, %s::jsonb, %s::jsonb, %s)",
            (
                "alpha", "alpha", "notify", "immediate", "user", "system",
                "open", "Action required: review my plan",
                "Plan needs your approval",
                json.dumps({"project": "alpha"}),
                json.dumps(["plan_review"]),
                "legacy",
            ),
        )
        conn.commit()
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        '[projects.alpha]\npath = "."\n',
        encoding="utf-8",
    )
    from pollypm.storage.inbox_action_preview import (
        load_fast_inbox_action_preview,
    )

    result = load_fast_inbox_action_preview(
        config_path, project=None, limit=5, config=pg_config,
    )
    assert result is not None
    preview, ids, total = result
    assert total == 1
    assert preview[0].title.startswith("Action required")
    assert preview[0].triage_label == "plan review"


# --------------------------------------------------------------------- #
# project_state_purge
# --------------------------------------------------------------------- #


def test_pg_count_and_purge_project_state_rows(pg_schema_pool, pg_config, tmp_path):
    _apply_initial_migrations(pg_schema_pool)
    _seed_work_tasks(pg_schema_pool, [
        {"project": "alpha", "task_number": 1},
        {"project": "alpha", "task_number": 2},
        {"project": "beta", "task_number": 1},
    ])
    with pg_schema_pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO worktrees ("
            "project_key, lane_kind, lane_key, path, branch, status, "
            "created_at, updated_at"
            ") VALUES (%s, %s, %s, %s, %s, %s, now(), now())",
            ("alpha", "main", "alpha", "/tmp/a", "main", "active"),
        )
        conn.commit()
    from pollypm.storage.project_state_purge import (
        count_project_state_rows,
        purge_project_state_rows,
    )

    counts = count_project_state_rows(
        tmp_path / "x.db", "alpha", config=pg_config,
    )
    assert counts["work_tasks"] == 2
    assert counts["worktrees"] == 1

    purged = purge_project_state_rows(
        tmp_path / "x.db", "alpha", config=pg_config,
    )
    assert purged["work_tasks"] == 2
    assert purged["worktrees"] == 1

    # The other project is untouched.
    other = count_project_state_rows(
        tmp_path / "x.db", "beta", config=pg_config,
    )
    assert other["work_tasks"] == 1


def test_pg_purge_dry_run_does_not_delete(pg_schema_pool, pg_config, tmp_path):
    _apply_initial_migrations(pg_schema_pool)
    _seed_work_tasks(pg_schema_pool, [
        {"project": "alpha", "task_number": 1},
    ])
    from pollypm.storage.project_state_purge import (
        count_project_state_rows,
        purge_project_state_rows,
    )

    counts = purge_project_state_rows(
        tmp_path / "x.db", "alpha", dry_run=True, config=pg_config,
    )
    assert counts["work_tasks"] == 1
    # No mutation happened.
    after = count_project_state_rows(
        tmp_path / "x.db", "alpha", config=pg_config,
    )
    assert after["work_tasks"] == 1


# --------------------------------------------------------------------- #
# legacy_per_project_db — pg branch is a no-op
# --------------------------------------------------------------------- #


def test_pg_legacy_per_project_db_migration_skipped(pg_config):
    from pollypm.storage.legacy_per_project_db import (
        migrate_legacy_per_project_dbs,
    )

    reports = migrate_legacy_per_project_dbs(config=pg_config)
    assert reports == []


# --------------------------------------------------------------------- #
# pg_workspace_state — workspace_state KV (Slice K-state-port phase 2)
# --------------------------------------------------------------------- #


def test_pg_workspace_state_set_get_clear_roundtrip(pg_schema_pool, pg_config):
    """The pg facade must mirror StateStore's set / get / clear contract."""
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_workspace_state import (
        clear_workspace_state,
        get_workspace_state,
        set_workspace_state,
    )

    assert get_workspace_state("missing", pool=pg_schema_pool) is None

    set_workspace_state(
        "product_state",
        {"state": "broken", "reason": "test", "extra": {"k": 1}},
        actor="unit-test",
        pool=pg_schema_pool,
    )
    payload = get_workspace_state("product_state", pool=pg_schema_pool)
    assert payload == {"state": "broken", "reason": "test", "extra": {"k": 1}}

    # Idempotent re-set replaces the row.
    set_workspace_state(
        "product_state",
        {"state": "broken", "reason": "updated"},
        actor="unit-test-2",
        pool=pg_schema_pool,
    )
    payload2 = get_workspace_state("product_state", pool=pg_schema_pool)
    assert payload2 == {"state": "broken", "reason": "updated"}

    # Clear returns True on hit, False on miss.
    assert clear_workspace_state("product_state", pool=pg_schema_pool) is True
    assert clear_workspace_state("product_state", pool=pg_schema_pool) is False
    assert get_workspace_state("product_state", pool=pg_schema_pool) is None


def test_pg_workspace_state_non_dict_payload_returns_none(pg_schema_pool, pg_config):
    """A non-dict jsonb payload should read back as None (sqlite parity)."""
    _apply_initial_migrations(pg_schema_pool)
    # Write a list payload directly so the facade has to gracefully
    # reject it on read. The StateStore equivalent returns None for any
    # non-dict; the pg facade must do the same.
    with pg_schema_pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO workspace_state (key, value_json, set_at, set_by) "
            "VALUES (%s, %s::jsonb, now(), 'test')",
            ("weird", json.dumps(["not", "a", "dict"])),
        )
        conn.commit()

    from pollypm.storage.pg_workspace_state import get_workspace_state

    assert get_workspace_state("weird", pool=pg_schema_pool) is None


# --------------------------------------------------------------------- #
# pg_notifications — task-assignment dedupe (Slice K-state-port phase 2)
# --------------------------------------------------------------------- #


def test_pg_notifications_claim_then_dedupe(pg_schema_pool, pg_config):
    """A second claim inside the window returns None (TOCTOU-safe dedupe)."""
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_notifications import claim_notification_slot

    first = claim_notification_slot(
        session_name="worker-alpha",
        task_id="alpha:7",
        window_seconds=1800,
        execution_version=1,
        project="alpha",
        message="kickoff",
        pool=pg_schema_pool,
    )
    assert isinstance(first, int) and first > 0

    # Same session/task/version inside the window dedupes.
    second = claim_notification_slot(
        session_name="worker-alpha",
        task_id="alpha:7",
        window_seconds=1800,
        execution_version=1,
        project="alpha",
        message="kickoff",
        pool=pg_schema_pool,
    )
    assert second is None

    # A bumped execution_version (e.g. reject-bounce) is a fresh ping.
    third = claim_notification_slot(
        session_name="worker-alpha",
        task_id="alpha:7",
        window_seconds=1800,
        execution_version=2,
        project="alpha",
        message="kickoff-after-bounce",
        pool=pg_schema_pool,
    )
    assert isinstance(third, int) and third > 0 and third != first


def test_pg_notifications_update_status_and_was_notified(pg_schema_pool, pg_config):
    """update_notification_status + was_notified_within must round-trip."""
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_notifications import (
        claim_notification_slot,
        update_notification_status,
        was_notified_within,
    )

    claim_id = claim_notification_slot(
        session_name="worker-beta",
        task_id="beta:1",
        window_seconds=1800,
        execution_version=0,
        project="beta",
        message="kickoff",
        pool=pg_schema_pool,
    )
    assert isinstance(claim_id, int)

    update_notification_status(
        claim_id,
        delivery_status="sent",
        message="canonical body",
        pool=pg_schema_pool,
    )

    # The read-side probe should see the row inside the window.
    assert (
        was_notified_within(
            "worker-beta", "beta:1", 1800, 0, pool=pg_schema_pool,
        )
        is True
    )
    # A different version doesn't match.
    assert (
        was_notified_within(
            "worker-beta", "beta:1", 1800, 99, pool=pg_schema_pool,
        )
        is False
    )


def test_pg_notifications_recent_notifications_shape(pg_schema_pool, pg_config):
    """recent_notifications returns the documented dict shape."""
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_notifications import (
        record_notification,
        recent_notifications,
    )

    record_notification(
        session_name="worker-gamma",
        task_id="gamma:5",
        project="gamma",
        message="kickoff",
        delivery_status="sent",
        execution_version=1,
        pool=pg_schema_pool,
    )
    record_notification(
        session_name="worker-delta",
        task_id="delta:1",
        project="delta",
        message="kickoff",
        delivery_status="failed: timeout",
        execution_version=0,
        pool=pg_schema_pool,
    )

    rows = recent_notifications(limit=10, pool=pg_schema_pool)
    assert len(rows) == 2
    keys = {
        "session_name",
        "task_id",
        "project",
        "notified_at",
        "delivery_status",
        "message",
        "execution_version",
    }
    assert all(keys <= set(row.keys()) for row in rows)
    by_task = {row["task_id"]: row for row in rows}
    assert by_task["gamma:5"]["delivery_status"] == "sent"
    assert by_task["delta:1"]["delivery_status"].startswith("failed")

    # Filter by project.
    only_gamma = recent_notifications(
        project="gamma", limit=10, pool=pg_schema_pool,
    )
    assert [r["task_id"] for r in only_gamma] == ["gamma:5"]


# --------------------------------------------------------------------- #
# pg_architect_resume — resume tokens (Slice K-state-port phase 2b)
# --------------------------------------------------------------------- #


def test_pg_architect_resume_upsert_get_clear_roundtrip(pg_schema_pool, pg_config):
    """The pg facade must mirror StateStore's upsert / get / clear contract."""
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_architect_resume import (
        clear_architect_resume_token,
        get_architect_resume_token,
        list_architect_resume_tokens,
        upsert_architect_resume_token,
    )

    assert get_architect_resume_token("alpha", pool=pg_schema_pool) is None

    upsert_architect_resume_token(
        project_key="alpha",
        provider="claude",
        session_id="sess-uuid-1",
        last_active_at="2025-01-01T12:00:00+00:00",
        pool=pg_schema_pool,
    )
    record = get_architect_resume_token("alpha", pool=pg_schema_pool)
    assert record is not None
    assert record.project_key == "alpha"
    assert record.provider == "claude"
    assert record.session_id == "sess-uuid-1"
    # captured_at is stamped server-side; just confirm it's a non-empty string.
    assert isinstance(record.captured_at, str) and record.captured_at
    # last_active_at round-trips as the iso-8601 datetime we passed in.
    assert record.last_active_at.startswith("2025-01-01")

    # Re-upsert replaces the row (same project_key — primary key).
    upsert_architect_resume_token(
        project_key="alpha",
        provider="codex",
        session_id="sess-uuid-2",
        last_active_at="2025-01-02T12:00:00+00:00",
        pool=pg_schema_pool,
    )
    record2 = get_architect_resume_token("alpha", pool=pg_schema_pool)
    assert record2 is not None
    assert record2.provider == "codex"
    assert record2.session_id == "sess-uuid-2"

    # list returns every row.
    upsert_architect_resume_token(
        project_key="beta",
        provider="claude",
        session_id="sess-uuid-3",
        last_active_at="2025-01-03T12:00:00+00:00",
        pool=pg_schema_pool,
    )
    rows = list_architect_resume_tokens(pool=pg_schema_pool)
    keys = {r.project_key for r in rows}
    assert keys == {"alpha", "beta"}

    # clear is a no-op on miss, removes on hit.
    clear_architect_resume_token("alpha", pool=pg_schema_pool)
    assert get_architect_resume_token("alpha", pool=pg_schema_pool) is None
    clear_architect_resume_token("missing", pool=pg_schema_pool)  # no raise


# --------------------------------------------------------------------- #
# pg_checkpoints — checkpoint ring (Slice K-state-port phase 2b)
# --------------------------------------------------------------------- #


def test_pg_checkpoints_record_and_latest(pg_schema_pool, pg_config):
    """Each record_checkpoint inserts a new row; latest_checkpoint returns newest."""
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_checkpoints import (
        latest_checkpoint,
        record_checkpoint,
    )

    assert latest_checkpoint("worker-alpha", pool=pg_schema_pool) is None

    record_checkpoint(
        session_name="worker-alpha",
        project_key="alpha",
        level="L0",
        json_path="/tmp/cp1.json",
        summary_path="/tmp/cp1.md",
        snapshot_path="/tmp/cp1.snap",
        summary_text="first checkpoint",
        pool=pg_schema_pool,
    )
    record_checkpoint(
        session_name="worker-alpha",
        project_key="alpha",
        level="L1",
        json_path="/tmp/cp2.json",
        summary_path="/tmp/cp2.md",
        snapshot_path="/tmp/cp2.snap",
        summary_text="second checkpoint",
        pool=pg_schema_pool,
    )
    # A different session must not bleed into the lookup.
    record_checkpoint(
        session_name="worker-beta",
        project_key="beta",
        level="L0",
        json_path="/tmp/other.json",
        summary_path="/tmp/other.md",
        snapshot_path="/tmp/other.snap",
        summary_text="unrelated",
        pool=pg_schema_pool,
    )

    latest = latest_checkpoint("worker-alpha", pool=pg_schema_pool)
    assert latest is not None
    assert latest.level == "L1"
    assert latest.summary_text == "second checkpoint"
    assert latest.session_name == "worker-alpha"
    assert isinstance(latest.created_at, str) and latest.created_at


# --------------------------------------------------------------------- #
# pg_worktrees — worktree inventory (Slice K-state-port phase 2b)
# --------------------------------------------------------------------- #


def test_pg_worktrees_upsert_status_list(pg_schema_pool, pg_config):
    """upsert dedupes on (project, lane, status); list returns newest first."""
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_worktrees import (
        list_worktrees,
        update_worktree_status,
        upsert_worktree,
    )

    assert list_worktrees(pool=pg_schema_pool) == []

    upsert_worktree(
        project_key="alpha",
        lane_kind="main",
        lane_key="alpha",
        session_name="sess-1",
        issue_key=None,
        path="/tmp/wt-alpha",
        branch="main",
        status="active",
        pool=pg_schema_pool,
    )
    # Re-upserting the same (project, lane, status) updates in place.
    upsert_worktree(
        project_key="alpha",
        lane_kind="main",
        lane_key="alpha",
        session_name="sess-1b",
        issue_key="alpha#42",
        path="/tmp/wt-alpha",
        branch="main",
        status="active",
        pool=pg_schema_pool,
    )
    rows = list_worktrees("alpha", pool=pg_schema_pool)
    assert len(rows) == 1
    assert rows[0].session_name == "sess-1b"
    assert rows[0].issue_key == "alpha#42"

    # Promoting active → closed leaves a single row in the closed state.
    update_worktree_status("alpha", "main", "alpha", "closed", pool=pg_schema_pool)
    rows = list_worktrees("alpha", pool=pg_schema_pool)
    assert len(rows) == 1
    assert rows[0].status == "closed"

    # A fresh active row coexists with the historical closed row.
    upsert_worktree(
        project_key="alpha",
        lane_kind="main",
        lane_key="alpha",
        session_name="sess-2",
        issue_key=None,
        path="/tmp/wt-alpha-2",
        branch="main",
        status="active",
        pool=pg_schema_pool,
    )
    rows = list_worktrees("alpha", pool=pg_schema_pool)
    assert len(rows) == 2
    statuses = {r.status for r in rows}
    assert statuses == {"active", "closed"}

    # Scoped list filters by project.
    upsert_worktree(
        project_key="beta",
        lane_kind="issue",
        lane_key="beta#1",
        session_name=None,
        issue_key="beta#1",
        path="/tmp/wt-beta",
        branch="issue/beta-1",
        status="active",
        pool=pg_schema_pool,
    )
    all_rows = list_worktrees(pool=pg_schema_pool)
    alpha_rows = list_worktrees("alpha", pool=pg_schema_pool)
    assert len(all_rows) == 3
    assert len(alpha_rows) == 2


# --------------------------------------------------------------------- #
# pg_token_usage — token samples + hourly aggregate (Slice K-state-port phase 2b)
# --------------------------------------------------------------------- #


def test_pg_token_usage_record_sample_rolls_hourly(pg_schema_pool, pg_config):
    """record_token_sample computes delta + rolls forward the hourly bucket."""
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_token_usage import (
        get_token_sample,
        record_token_sample,
        recent_token_usage,
    )

    assert get_token_sample("worker-alpha", pool=pg_schema_pool) is None

    # First sample: no previous row, so delta is zero and the hourly
    # aggregate stays empty.
    delta1 = record_token_sample(
        session_name="worker-alpha",
        account_name="acct-1",
        provider="claude",
        model_name="sonnet",
        project_key="alpha",
        cumulative_tokens=100,
        observed_at="2025-01-01T12:30:00+00:00",
        pool=pg_schema_pool,
    )
    assert delta1 == 0
    assert recent_token_usage(pool=pg_schema_pool) == []

    sample = get_token_sample("worker-alpha", pool=pg_schema_pool)
    assert sample is not None
    assert sample.cumulative_tokens == 100

    # Second sample (same tuple, higher cumulative): delta = 150,
    # lands in the 12:00 hour bucket.
    delta2 = record_token_sample(
        session_name="worker-alpha",
        account_name="acct-1",
        provider="claude",
        model_name="sonnet",
        project_key="alpha",
        cumulative_tokens=250,
        observed_at="2025-01-01T12:45:00+00:00",
        pool=pg_schema_pool,
    )
    assert delta2 == 150
    rows = recent_token_usage(pool=pg_schema_pool)
    assert len(rows) == 1
    assert rows[0].tokens_used == 150
    assert rows[0].hour_bucket.startswith("2025-01-01T12")

    # Third sample (switched accounts): delta resets to zero, no
    # hourly write.
    delta3 = record_token_sample(
        session_name="worker-alpha",
        account_name="acct-2",
        provider="claude",
        model_name="sonnet",
        project_key="alpha",
        cumulative_tokens=400,
        observed_at="2025-01-01T13:15:00+00:00",
        pool=pg_schema_pool,
    )
    assert delta3 == 0
    rows = recent_token_usage(pool=pg_schema_pool)
    # Still one row — the acct-1 12:00 bucket; acct-2 never wrote.
    assert len(rows) == 1


def test_pg_token_usage_replace_hourly_and_daily(pg_schema_pool, pg_config):
    """replace_token_usage_hourly + daily_token_usage parity shape."""
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_token_usage import (
        daily_token_usage,
        recent_token_usage,
        replace_token_usage_hourly,
    )
    from pollypm.storage.records import TokenUsageHourlyRecord

    seed = [
        TokenUsageHourlyRecord(
            hour_bucket="2025-01-01T12:00:00+00:00",
            account_name="acct-1",
            provider="claude",
            model_name="sonnet",
            project_key="alpha",
            tokens_used=100,
            updated_at="2025-01-01T12:45:00+00:00",
        ),
        TokenUsageHourlyRecord(
            hour_bucket="2025-01-02T08:00:00+00:00",
            account_name="acct-1",
            provider="claude",
            model_name="sonnet",
            project_key="alpha",
            tokens_used=250,
            updated_at="2025-01-02T08:30:00+00:00",
        ),
        TokenUsageHourlyRecord(
            hour_bucket="2025-01-02T09:00:00+00:00",
            account_name="acct-2",
            provider="codex",
            model_name="gpt-5",
            project_key="beta",
            tokens_used=50,
            updated_at="2025-01-02T09:15:00+00:00",
        ),
    ]
    replace_token_usage_hourly(seed, pool=pg_schema_pool)

    rows = recent_token_usage(limit=10, pool=pg_schema_pool)
    assert len(rows) == 3
    # ORDER BY hour_bucket DESC, tokens_used DESC.
    assert rows[0].hour_bucket.startswith("2025-01-02T09")
    assert rows[1].hour_bucket.startswith("2025-01-02T08")
    assert rows[2].hour_bucket.startswith("2025-01-01T12")

    days = daily_token_usage(days=10, pool=pg_schema_pool)
    # oldest-first per the StateStore contract.
    by_day = dict(days)
    assert by_day["2025-01-01"] == 100
    assert by_day["2025-01-02"] == 300
    assert days[0][0] == "2025-01-01"
    assert days[-1][0] == "2025-01-02"

    # Scoped replace only clears the named accounts.
    replace_token_usage_hourly(
        [
            TokenUsageHourlyRecord(
                hour_bucket="2025-01-03T10:00:00+00:00",
                account_name="acct-1",
                provider="claude",
                model_name="sonnet",
                project_key="alpha",
                tokens_used=999,
                updated_at="2025-01-03T10:15:00+00:00",
            ),
        ],
        account_names=["acct-1"],
        pool=pg_schema_pool,
    )
    rows = recent_token_usage(limit=10, pool=pg_schema_pool)
    # acct-1 rows are gone except the new one; acct-2 row is preserved.
    by_acct = sorted({(r.account_name, r.hour_bucket[:10]) for r in rows})
    assert by_acct == [
        ("acct-1", "2025-01-03"),
        ("acct-2", "2025-01-02"),
    ]
