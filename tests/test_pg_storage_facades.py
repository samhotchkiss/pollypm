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
