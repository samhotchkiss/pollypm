"""Tests for the morning_briefing gather + priority helpers (mb02)."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from pollypm.models import KnownProject, PollyPMConfig, PollyPMSettings, ProjectKind, ProjectSettings
from pollypm.plugins_builtin.morning_briefing.handlers.gather_yesterday import (
    CommitInfo,
    YesterdaySnapshot,
    _gather_commits_for_project,
    _gather_transitions_for_project,
    _gather_downtime_artifacts,
    _gather_advisor_insights,
    gather_yesterday,
    iter_tracked_projects,
    project_state_db_paths,
    yesterday_window,
)
from pollypm.plugins_builtin.morning_briefing.handlers.identify_priorities import (
    PriorityEntry,
    PriorityList,
    _gather_blockers_for_project,
    _gather_top_tasks_for_project,
    identify_priorities,
)
from pollypm.work.sqlite_service import SQLiteWorkService


NY = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# Fixtures — in-process project setup
# ---------------------------------------------------------------------------


def _make_empty_config(
    project_root: Path,
    *,
    workspace_root: Path | None = None,
) -> PollyPMConfig:
    return PollyPMConfig(
        project=ProjectSettings(
            name="Fixture",
            root_dir=project_root,
            workspace_root=workspace_root if workspace_root is not None else project_root,
            base_dir=project_root / ".pollypm",
            logs_dir=project_root / ".pollypm" / "logs",
            snapshots_dir=project_root / ".pollypm" / "snapshots",
            state_db=project_root / ".pollypm" / "state.db",
        ),
        pollypm=PollyPMSettings(controller_account=""),
        accounts={},
        sessions={},
        projects={},
    )


def _make_project(tmp_path: Path, key: str, *, with_git: bool = False) -> Path:
    root = tmp_path / key
    root.mkdir(parents=True, exist_ok=True)
    (root / ".pollypm").mkdir(exist_ok=True)
    if with_git:
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
        subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=root, check=True)
    return root


def _add_commit(
    project_path: Path, subject: str, *, commit_date_utc: datetime, file_contents: str = "x"
) -> None:
    """Make a commit at a chosen committer-date."""
    f = project_path / "README.md"
    f.write_text(file_contents + "\n")
    subprocess.run(["git", "add", "README.md"], cwd=project_path, check=True)
    env_date = commit_date_utc.strftime("%Y-%m-%dT%H:%M:%S+0000")
    subprocess.run(
        ["git", "commit", "-q", "-m", subject],
        cwd=project_path,
        check=True,
        env={
            **_base_git_env(),
            "GIT_AUTHOR_DATE": env_date,
            "GIT_COMMITTER_DATE": env_date,
        },
    )


def _base_git_env() -> dict[str, str]:
    import os
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }


def _init_work_db(project_path: Path) -> Path:
    db_path = project_path / ".pollypm" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    svc = SQLiteWorkService(db_path=db_path, project_path=project_path)
    svc.close()
    return db_path


def _raw_insert_task(
    db_path: Path,
    *,
    project: str,
    task_number: int,
    title: str,
    work_status: str = "queued",
    priority: str = "normal",
    assignee: str = "",
    labels: list[str] | None = None,
    created_at: str | None = None,
    updated_at: str | None = None,
) -> None:
    created = created_at or datetime.now(UTC).isoformat()
    updated = updated_at or created
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO work_tasks "
            "(project, task_number, title, type, labels, work_status, flow_template_id, "
            " priority, assignee, created_at, created_by, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                project, task_number, title, "task",
                json.dumps(labels or []),
                work_status, "standard", priority,
                assignee or None, created, "test", updated,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _raw_insert_transition(
    db_path: Path,
    *,
    project: str,
    task_number: int,
    from_state: str,
    to_state: str,
    actor: str = "test",
    created_at: str,
    reason: str | None = None,
) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO work_transitions "
            "(task_project, task_number, from_state, to_state, actor, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (project, task_number, from_state, to_state, actor, reason, created_at),
        )
        conn.commit()
    finally:
        conn.close()


def _raw_insert_dep(
    db_path: Path,
    *,
    from_project: str,
    from_task_number: int,
    to_project: str,
    to_task_number: int,
    kind: str = "blocks",
) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO work_task_dependencies "
            "(from_project, from_task_number, to_project, to_task_number, kind, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (from_project, from_task_number, to_project, to_task_number,
             kind, datetime.now(UTC).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# yesterday_window
# ---------------------------------------------------------------------------


class TestYesterdayWindow:
    def test_boundary(self) -> None:
        now = datetime(2026, 4, 16, 6, 0, tzinfo=NY)
        start, end = yesterday_window(now)
        assert start == datetime(2026, 4, 15, 0, 0, tzinfo=NY)
        assert end == datetime(2026, 4, 16, 0, 0, tzinfo=NY)

    def test_dst_spring_forward(self) -> None:
        # 2026 DST-start in the US is 2026-03-08 02:00. "Yesterday" from
        # 2026-03-09 06:00 NY should span 2026-03-08 local midnight (which
        # maps to 05:00 UTC, not the usual 04:00 — because DST jumped).
        now = datetime(2026, 3, 9, 6, 0, tzinfo=NY)
        start, end = yesterday_window(now)
        assert start.date().isoformat() == "2026-03-08"
        assert end.date().isoformat() == "2026-03-09"
        # Window in UTC spans ~23 hours (short day).
        duration = (end.astimezone(UTC) - start.astimezone(UTC)).total_seconds()
        assert 82000 < duration < 87000  # 23±1 hours

    def test_requires_tz_aware(self) -> None:
        with pytest.raises(ValueError):
            yesterday_window(datetime(2026, 4, 16, 6, 0))


# ---------------------------------------------------------------------------
# iter_tracked_projects
# ---------------------------------------------------------------------------


class TestIterTrackedProjects:
    def test_includes_ambient_root(self, tmp_path: Path) -> None:
        config = _make_empty_config(tmp_path)
        result = iter_tracked_projects(config)
        # Without any [projects.*] entries, the ambient root is surfaced.
        assert len(result) == 1
        assert result[0].path == tmp_path

    def test_explicit_tracked_projects(self, tmp_path: Path) -> None:
        ambient = tmp_path / "ambient"
        ambient.mkdir()
        p1 = _make_project(tmp_path, "alpha")
        p2 = _make_project(tmp_path, "beta")
        untracked = _make_project(tmp_path, "gamma")

        config = _make_empty_config(ambient)
        config.projects = {
            "alpha": KnownProject(key="alpha", path=p1, tracked=True),
            "beta": KnownProject(key="beta", path=p2, tracked=True),
            "gamma": KnownProject(key="gamma", path=untracked, tracked=False),
        }
        keys = {p.key for p in iter_tracked_projects(config)}
        assert "alpha" in keys
        assert "beta" in keys
        assert "gamma" not in keys

    def test_missing_path_is_skipped(self, tmp_path: Path) -> None:
        ambient = tmp_path / "ambient"
        ambient.mkdir()
        config = _make_empty_config(ambient)
        config.projects = {
            "ghost": KnownProject(
                key="ghost", path=tmp_path / "does-not-exist", tracked=True,
            ),
        }
        keys = {p.key for p in iter_tracked_projects(config)}
        assert "ghost" not in keys


# ---------------------------------------------------------------------------
# Git log gathering
# ---------------------------------------------------------------------------


class TestGatherCommits:
    def test_returns_commits_in_window(self, tmp_path: Path) -> None:
        project = KnownProject(
            key="alpha", path=_make_project(tmp_path, "alpha", with_git=True), tracked=True,
        )
        # Chronological order (required by git-log's since/until traversal):
        # oldest ancestor first, then newer commits on top.
        old_ts = datetime(2026, 4, 13, 14, 0, tzinfo=UTC)
        _add_commit(
            project.path, "old work", commit_date_utc=old_ts, file_contents="y",
        )
        yesterday_ts = datetime(2026, 4, 15, 14, 0, tzinfo=UTC)
        _add_commit(project.path, "yesterday work", commit_date_utc=yesterday_ts)

        start = datetime(2026, 4, 15, 0, 0, tzinfo=UTC)
        end = datetime(2026, 4, 16, 0, 0, tzinfo=UTC)
        commits = _gather_commits_for_project(
            project, since_utc=start, until_utc=end,
        )
        subjects = [c.subject for c in commits]
        assert "yesterday work" in subjects
        assert "old work" not in subjects

    def test_no_git_returns_empty(self, tmp_path: Path) -> None:
        project = KnownProject(
            key="nogit", path=_make_project(tmp_path, "nogit"), tracked=True,
        )
        start = datetime(2026, 4, 15, 0, 0, tzinfo=UTC)
        end = datetime(2026, 4, 16, 0, 0, tzinfo=UTC)
        assert _gather_commits_for_project(project, since_utc=start, until_utc=end) == []


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------


class TestGatherTransitions:
    def test_returns_rows_in_window(self, tmp_path: Path) -> None:
        project_path = _make_project(tmp_path, "alpha")
        db = _init_work_db(project_path)
        _raw_insert_task(db, project="alpha", task_number=1, title="foo", work_status="done")

        in_window = "2026-04-15T14:00:00"
        out_of_window = "2026-04-13T14:00:00"
        _raw_insert_transition(
            db, project="alpha", task_number=1,
            from_state="queued", to_state="done", created_at=in_window,
        )
        _raw_insert_transition(
            db, project="alpha", task_number=1,
            from_state="draft", to_state="queued", created_at=out_of_window,
        )

        project = KnownProject(key="alpha", path=project_path, tracked=True)
        rows = _gather_transitions_for_project(
            project,
            since_iso="2026-04-15T00:00:00",
            until_iso="2026-04-16T00:00:00",
        )
        assert len(rows) == 1
        assert rows[0].task_id == "alpha/1"
        assert rows[0].from_state == "queued"
        assert rows[0].to_state == "done"
        assert rows[0].task_title == "foo"

    def test_missing_db_returns_empty(self, tmp_path: Path) -> None:
        project = KnownProject(
            key="nodb", path=_make_project(tmp_path, "nodb"), tracked=True,
        )
        rows = _gather_transitions_for_project(
            project,
            since_iso="2026-04-15T00:00:00",
            until_iso="2026-04-16T00:00:00",
        )
        assert rows == []


class TestGatherDowntimeArtifacts:
    def test_only_downtime_labelled_awaiting_approval(self, tmp_path: Path) -> None:
        project_path = _make_project(tmp_path, "alpha")
        db = _init_work_db(project_path)
        _raw_insert_task(
            db, project="alpha", task_number=1, title="downtime task",
            labels=["downtime"], work_status="review",
        )
        _raw_insert_task(
            db, project="alpha", task_number=2, title="other task",
            labels=["feature"], work_status="review",
        )
        _raw_insert_transition(
            db, project="alpha", task_number=1,
            from_state="in_progress", to_state="awaiting_approval",
            created_at="2026-04-15T14:00:00",
        )
        _raw_insert_transition(
            db, project="alpha", task_number=2,
            from_state="in_progress", to_state="awaiting_approval",
            created_at="2026-04-15T15:00:00",
        )

        project = KnownProject(key="alpha", path=project_path, tracked=True)
        artifacts = _gather_downtime_artifacts(
            project,
            since_iso="2026-04-15T00:00:00",
            until_iso="2026-04-16T00:00:00",
        )
        assert len(artifacts) == 1
        assert artifacts[0].task_id == "alpha/1"


# ---------------------------------------------------------------------------
# Advisor log
# ---------------------------------------------------------------------------


class TestGatherAdvisorInsights:
    def test_reads_emitted_entries(self, tmp_path: Path) -> None:
        log_path = tmp_path / ".pollypm" / "advisor-log.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            json.dumps({
                "timestamp": "2026-04-15T14:00:00",
                "emit": True,
                "project": "alpha",
                "kind": "perf",
                "title": "slow thing",
                "body": "...",
            }) + "\n"
            + json.dumps({
                "timestamp": "2026-04-15T14:30:00",
                "emit": False,  # dropped
                "project": "alpha",
                "kind": "noise",
            }) + "\n"
            + json.dumps({
                "timestamp": "2026-04-13T14:00:00",  # outside window
                "emit": True,
                "project": "alpha",
                "kind": "old",
            }) + "\n"
            + "not json\n"
        )
        insights = _gather_advisor_insights(
            tmp_path,
            since_iso="2026-04-15T00:00:00",
            until_iso="2026-04-16T00:00:00",
        )
        assert len(insights) == 1
        assert insights[0].kind == "perf"
        assert insights[0].title == "slow thing"

    def test_missing_log_returns_empty(self, tmp_path: Path) -> None:
        assert _gather_advisor_insights(
            tmp_path,
            since_iso="2026-04-15T00:00:00",
            until_iso="2026-04-16T00:00:00",
        ) == []


# ---------------------------------------------------------------------------
# Priorities
# ---------------------------------------------------------------------------


class TestGatherTopTasks:
    def test_order_by_priority_desc_then_stale(self, tmp_path: Path) -> None:
        project_path = _make_project(tmp_path, "alpha")
        db = _init_work_db(project_path)
        now = datetime(2026, 4, 16, 6, 0, tzinfo=UTC)

        def iso(delta_hours: float) -> str:
            return (now - timedelta(hours=delta_hours)).isoformat()

        _raw_insert_task(
            db, project="alpha", task_number=1, title="critical old",
            priority="critical", work_status="queued",
            created_at=iso(48), updated_at=iso(48),
        )
        _raw_insert_task(
            db, project="alpha", task_number=2, title="critical new",
            priority="critical", work_status="queued",
            created_at=iso(1), updated_at=iso(1),
        )
        _raw_insert_task(
            db, project="alpha", task_number=3, title="high",
            priority="high", work_status="queued",
            created_at=iso(2), updated_at=iso(2),
        )
        _raw_insert_task(
            db, project="alpha", task_number=4, title="done-wrong-bucket",
            priority="critical", work_status="done",
            created_at=iso(1), updated_at=iso(1),
        )

        project = KnownProject(key="alpha", path=project_path, tracked=True)
        results = _gather_top_tasks_for_project(project, now_utc=now, limit=5)

        # done tasks are excluded.
        ids = [r.task_id for r in results]
        assert "alpha/4" not in ids
        # All three open tasks surfaced.
        assert set(ids) >= {"alpha/1", "alpha/2", "alpha/3"}


class TestBlockers:
    def test_finds_blocked_tasks_with_deps(self, tmp_path: Path) -> None:
        project_path = _make_project(tmp_path, "alpha")
        db = _init_work_db(project_path)
        _raw_insert_task(db, project="alpha", task_number=1, title="A", work_status="blocked")
        _raw_insert_task(db, project="alpha", task_number=2, title="B", work_status="in_progress")
        _raw_insert_task(db, project="alpha", task_number=3, title="C", work_status="done")
        _raw_insert_dep(db, from_project="alpha", from_task_number=1,
                        to_project="alpha", to_task_number=2)
        _raw_insert_dep(db, from_project="alpha", from_task_number=1,
                        to_project="alpha", to_task_number=3)

        project = KnownProject(key="alpha", path=project_path, tracked=True)
        blockers = _gather_blockers_for_project(project)
        assert len(blockers) == 1
        entry = blockers[0]
        assert entry.task_id == "alpha/1"
        assert set(entry.blocked_by) == {"alpha/2", "alpha/3"}
        # alpha/3 is done, so only alpha/2 is unresolved.
        assert entry.unresolved_blockers == ["alpha/2"]


# ---------------------------------------------------------------------------
# Integration: gather_yesterday + identify_priorities
# ---------------------------------------------------------------------------


class TestGatherYesterdayIntegration:
    def test_two_projects_end_to_end(self, tmp_path: Path) -> None:
        # Build ambient root (where advisor log lives).
        ambient = tmp_path / "ambient"
        ambient.mkdir()

        # Two tracked projects, one with commits, both with task activity.
        alpha = _make_project(tmp_path, "alpha", with_git=True)
        beta = _make_project(tmp_path, "beta")
        db_a = _init_work_db(alpha)
        db_b = _init_work_db(beta)

        # Yesterday window: 2026-04-15 UTC.
        yesterday_ts = "2026-04-15T14:00:00"

        _add_commit(alpha, "alpha commit", commit_date_utc=datetime(2026, 4, 15, 14, 0, tzinfo=UTC))
        _raw_insert_task(db_a, project="alpha", task_number=1, title="alpha task", work_status="done")
        _raw_insert_transition(
            db_a, project="alpha", task_number=1,
            from_state="review", to_state="done", created_at=yesterday_ts,
        )
        _raw_insert_task(db_b, project="beta", task_number=7, title="beta task", work_status="queued")
        _raw_insert_transition(
            db_b, project="beta", task_number=7,
            from_state="draft", to_state="queued", created_at=yesterday_ts,
        )

        advisor_log = ambient / ".pollypm" / "advisor-log.jsonl"
        advisor_log.parent.mkdir(parents=True, exist_ok=True)
        advisor_log.write_text(
            json.dumps({
                "timestamp": yesterday_ts, "emit": True,
                "project": "alpha", "kind": "perf",
                "title": "slow thing", "body": "…",
            }) + "\n"
        )

        config = _make_empty_config(ambient)
        config.projects = {
            "alpha": KnownProject(key="alpha", path=alpha, tracked=True),
            "beta": KnownProject(key="beta", path=beta, tracked=True),
        }

        now_local = datetime(2026, 4, 16, 6, 0, tzinfo=UTC)
        snapshot = gather_yesterday(config, now_local=now_local)
        assert snapshot.date_local == "2026-04-15"
        assert len(snapshot.commits_by_project.get("alpha", [])) == 1
        assert snapshot.commits_by_project.get("beta", []) == []
        ids = {r.task_id for r in snapshot.task_transitions}
        assert ids == {"alpha/1", "beta/7"}
        assert len(snapshot.advisor_insights) == 1
        assert snapshot.advisor_insights[0].kind == "perf"

        priorities = identify_priorities(config, now_local=now_local, priorities_count=5)
        top_ids = {t.task_id for t in priorities.top_tasks}
        assert "beta/7" in top_ids   # queued → surfaced
        assert "alpha/1" not in top_ids   # done → excluded

    def test_empty_projects(self, tmp_path: Path) -> None:
        ambient = tmp_path / "ambient"
        ambient.mkdir()
        config = _make_empty_config(ambient)
        now_local = datetime(2026, 4, 16, 6, 0, tzinfo=NY)
        snapshot = gather_yesterday(config, now_local=now_local)
        assert snapshot.is_empty
        priorities = identify_priorities(config, now_local=now_local)
        assert priorities.is_empty


# ---------------------------------------------------------------------------
# Awaiting-approval (inbox v2 state.json scan)
# ---------------------------------------------------------------------------


class TestAwaitingApproval:
    def _write_inbox_message(
        self,
        project_root: Path,
        *,
        msg_id: str,
        subject: str,
        sender: str,
        created_at: str,
        status: str = "open",
        kind: str | None = None,
    ) -> None:
        msg_dir = project_root / ".pollypm" / "inbox" / "messages" / msg_id
        msg_dir.mkdir(parents=True, exist_ok=True)
        state = {
            "id": msg_id,
            "subject": subject,
            "sender": sender,
            "owner": "user",
            "status": status,
            "created_at": created_at,
            "updated_at": created_at,
            "message_count": 1,
        }
        if kind:
            state["kind"] = kind
        (msg_dir / "state.json").write_text(json.dumps(state))

    def test_advisor_insight_older_than_24h(self, tmp_path: Path) -> None:
        now = datetime(2026, 4, 16, 6, 0, tzinfo=UTC)
        self._write_inbox_message(
            tmp_path,
            msg_id="old-advisor", subject="Advisor: slow thing",
            sender="advisor",
            created_at=(now - timedelta(hours=36)).isoformat(),
        )
        self._write_inbox_message(
            tmp_path,
            msg_id="fresh-advisor", subject="Advisor: new thing",
            sender="advisor",
            created_at=(now - timedelta(hours=2)).isoformat(),
        )
        # Build minimal config for identify_priorities.
        config = _make_empty_config(tmp_path)
        priorities = identify_priorities(config, now_local=now, priorities_count=5)
        ids = {i.id for i in priorities.awaiting_approval}
        assert "old-advisor" in ids
        assert "fresh-advisor" not in ids


# ---------------------------------------------------------------------------
# Performance budget
# ---------------------------------------------------------------------------


class TestPerfBudget:
    def test_gather_ten_projects_under_2s(self, tmp_path: Path) -> None:
        ambient = tmp_path / "ambient"
        ambient.mkdir()
        config = _make_empty_config(ambient)
        projects: dict[str, KnownProject] = {}
        now = datetime(2026, 4, 16, 6, 0, tzinfo=UTC)
        yesterday_ts = "2026-04-15T14:00:00"

        for i in range(10):
            key = f"proj{i}"
            path = _make_project(tmp_path, key)
            db = _init_work_db(path)
            for t in range(5):
                _raw_insert_task(
                    db, project=key, task_number=t + 1,
                    title=f"task {t}",
                    priority="normal" if t % 2 == 0 else "high",
                    work_status="queued",
                )
                _raw_insert_transition(
                    db, project=key, task_number=t + 1,
                    from_state="draft", to_state="queued", created_at=yesterday_ts,
                )
            projects[key] = KnownProject(key=key, path=path, tracked=True)
        config.projects = projects

        t0 = time.monotonic()
        snapshot = gather_yesterday(config, now_local=now)
        priorities = identify_priorities(config, now_local=now, priorities_count=5)
        elapsed = time.monotonic() - t0

        assert elapsed < 2.0, f"gather+priorities took {elapsed:.2f}s"
        assert len(snapshot.task_transitions) == 50
        assert len(priorities.top_tasks) == 5


# ---------------------------------------------------------------------------
# Post-#339 dual-path resolution (#1036)
#
# After #339, briefing reads must fall back to the workspace-root
# state.db when the per-project ``<project>/.pollypm/state.db`` is
# absent. These tests pin that behavior so a regression to a
# single-path probe is caught loudly.
# ---------------------------------------------------------------------------


class TestProjectStateDbPaths:
    def test_per_project_only(self, tmp_path: Path) -> None:
        project_path = _make_project(tmp_path, "alpha")
        _init_work_db(project_path)
        project = KnownProject(key="alpha", path=project_path, tracked=True)
        config = _make_empty_config(tmp_path / "ws-no-db", workspace_root=tmp_path / "ws-no-db")
        (tmp_path / "ws-no-db").mkdir()

        paths = project_state_db_paths(project, config)
        assert len(paths) == 1
        assert paths[0] == project_path / ".pollypm" / "state.db"

    def test_workspace_only_post_339(self, tmp_path: Path) -> None:
        # Project directory exists but has no per-project state.db.
        project_path = tmp_path / "alpha"
        project_path.mkdir()
        # Workspace root holds the canonical post-#339 DB.
        ws = tmp_path / "ws"
        ws.mkdir()
        ws_db = _init_work_db(ws)
        project = KnownProject(key="alpha", path=project_path, tracked=True)
        config = _make_empty_config(ws, workspace_root=ws)

        paths = project_state_db_paths(project, config)
        assert paths == [ws_db]

    def test_both_present_returns_both_deduped(self, tmp_path: Path) -> None:
        project_path = _make_project(tmp_path, "alpha")
        per_db = _init_work_db(project_path)
        ws = tmp_path / "ws"
        ws.mkdir()
        ws_db = _init_work_db(ws)
        project = KnownProject(key="alpha", path=project_path, tracked=True)
        config = _make_empty_config(ws, workspace_root=ws)

        paths = project_state_db_paths(project, config)
        assert per_db in paths
        assert ws_db in paths
        # Dedupe: per-project first, workspace second.
        assert paths.index(per_db) < paths.index(ws_db)
        # Resolved-path dedupe: no duplicates.
        resolved = [p.resolve() for p in paths]
        assert len(resolved) == len(set(resolved))

    def test_project_path_equals_workspace_dedupes(self, tmp_path: Path) -> None:
        # Single-project install where project path == workspace root.
        # Should scan once.
        ws = _make_project(tmp_path, "solo")
        _init_work_db(ws)
        project = KnownProject(key="solo", path=ws, tracked=True)
        config = _make_empty_config(ws, workspace_root=ws)

        paths = project_state_db_paths(project, config)
        assert len(paths) == 1


class TestPost339WorkspaceLayoutGather:
    """gather_yesterday must surface activity living in the workspace DB."""

    def test_transitions_resolved_from_workspace_db(self, tmp_path: Path) -> None:
        # Per-project state.db is ABSENT. The workspace state.db holds
        # the rows. This is exactly the install layout that triggered #1036.
        project_path = tmp_path / "alpha"
        project_path.mkdir()
        # NOTE: no .pollypm/state.db inside project_path.

        ws = tmp_path / "ws"
        ws.mkdir()
        ws_db = _init_work_db(ws)
        _raw_insert_task(
            ws_db, project="alpha", task_number=1, title="alpha task",
            work_status="done",
        )
        yesterday_ts = "2026-04-15T14:00:00"
        _raw_insert_transition(
            ws_db, project="alpha", task_number=1,
            from_state="review", to_state="done", created_at=yesterday_ts,
        )

        project = KnownProject(key="alpha", path=project_path, tracked=True)
        config = _make_empty_config(ws, workspace_root=ws)

        rows = _gather_transitions_for_project(
            project,
            since_iso="2026-04-15T00:00:00",
            until_iso="2026-04-16T00:00:00",
            config=config,
        )
        assert len(rows) == 1
        assert rows[0].task_id == "alpha/1"
        assert rows[0].task_title == "alpha task"

    def test_workspace_db_filtered_by_project_key(self, tmp_path: Path) -> None:
        # A shared workspace DB holds rows for many projects.
        # Each project's gather must only see its own rows.
        ws = tmp_path / "ws"
        ws.mkdir()
        ws_db = _init_work_db(ws)
        _raw_insert_task(ws_db, project="alpha", task_number=1, title="A", work_status="done")
        _raw_insert_task(ws_db, project="beta", task_number=2, title="B", work_status="done")
        _raw_insert_transition(
            ws_db, project="alpha", task_number=1,
            from_state="review", to_state="done", created_at="2026-04-15T14:00:00",
        )
        _raw_insert_transition(
            ws_db, project="beta", task_number=2,
            from_state="review", to_state="done", created_at="2026-04-15T15:00:00",
        )

        alpha_path = tmp_path / "alpha"
        alpha_path.mkdir()
        project_alpha = KnownProject(key="alpha", path=alpha_path, tracked=True)
        config = _make_empty_config(ws, workspace_root=ws)

        rows = _gather_transitions_for_project(
            project_alpha,
            since_iso="2026-04-15T00:00:00",
            until_iso="2026-04-16T00:00:00",
            config=config,
        )
        ids = {r.task_id for r in rows}
        assert ids == {"alpha/1"}

    def test_downtime_artifacts_resolved_from_workspace_db(self, tmp_path: Path) -> None:
        project_path = tmp_path / "alpha"
        project_path.mkdir()
        ws = tmp_path / "ws"
        ws.mkdir()
        ws_db = _init_work_db(ws)
        _raw_insert_task(
            ws_db, project="alpha", task_number=1, title="downtime task",
            labels=["downtime"], work_status="review",
        )
        _raw_insert_transition(
            ws_db, project="alpha", task_number=1,
            from_state="in_progress", to_state="awaiting_approval",
            created_at="2026-04-15T14:00:00",
        )

        project = KnownProject(key="alpha", path=project_path, tracked=True)
        config = _make_empty_config(ws, workspace_root=ws)

        artifacts = _gather_downtime_artifacts(
            project,
            since_iso="2026-04-15T00:00:00",
            until_iso="2026-04-16T00:00:00",
            config=config,
        )
        assert len(artifacts) == 1
        assert artifacts[0].task_id == "alpha/1"

    def test_gather_yesterday_end_to_end_workspace_db(self, tmp_path: Path) -> None:
        # End-to-end repro of #1036: tracked project with no per-project
        # state.db, all activity in the workspace DB. Snapshot must be
        # non-empty (failure mode in #1036 was: snapshot.is_empty == True).
        ws = tmp_path / "ws"
        ws.mkdir()
        ws_db = _init_work_db(ws)
        _raw_insert_task(
            ws_db, project="alpha", task_number=1, title="alpha task",
            work_status="done",
        )
        _raw_insert_transition(
            ws_db, project="alpha", task_number=1,
            from_state="review", to_state="done",
            created_at="2026-04-15T14:00:00",
        )

        alpha_path = tmp_path / "alpha"
        alpha_path.mkdir()
        config = _make_empty_config(ws, workspace_root=ws)
        config.projects = {
            "alpha": KnownProject(key="alpha", path=alpha_path, tracked=True),
        }

        now_local = datetime(2026, 4, 16, 6, 0, tzinfo=UTC)
        snapshot = gather_yesterday(config, now_local=now_local)
        # Pre-fix this would have been is_empty == True.
        assert not snapshot.is_empty
        ids = {r.task_id for r in snapshot.task_transitions}
        assert "alpha/1" in ids


class TestPost339WorkspaceLayoutPriorities:
    """identify_priorities must surface tasks living in the workspace DB."""

    def test_top_tasks_resolved_from_workspace_db(self, tmp_path: Path) -> None:
        project_path = tmp_path / "alpha"
        project_path.mkdir()
        ws = tmp_path / "ws"
        ws.mkdir()
        ws_db = _init_work_db(ws)
        _raw_insert_task(
            ws_db, project="alpha", task_number=1, title="open task",
            priority="high", work_status="queued",
        )

        project = KnownProject(key="alpha", path=project_path, tracked=True)
        config = _make_empty_config(ws, workspace_root=ws)
        now = datetime(2026, 4, 16, 6, 0, tzinfo=UTC)

        from pollypm.plugins_builtin.morning_briefing.handlers.identify_priorities import (
            _gather_top_tasks_for_project,
        )
        rows = _gather_top_tasks_for_project(
            project, now_utc=now, limit=5, config=config,
        )
        ids = {r.task_id for r in rows}
        assert ids == {"alpha/1"}

    def test_top_tasks_filtered_by_project_key(self, tmp_path: Path) -> None:
        # Shared workspace DB with multi-project rows.
        ws = tmp_path / "ws"
        ws.mkdir()
        ws_db = _init_work_db(ws)
        _raw_insert_task(ws_db, project="alpha", task_number=1, title="A", work_status="queued")
        _raw_insert_task(ws_db, project="beta", task_number=2, title="B", work_status="queued")

        alpha_path = tmp_path / "alpha"
        alpha_path.mkdir()
        project_alpha = KnownProject(key="alpha", path=alpha_path, tracked=True)
        config = _make_empty_config(ws, workspace_root=ws)
        now = datetime(2026, 4, 16, 6, 0, tzinfo=UTC)

        from pollypm.plugins_builtin.morning_briefing.handlers.identify_priorities import (
            _gather_top_tasks_for_project,
        )
        rows = _gather_top_tasks_for_project(
            project_alpha, now_utc=now, limit=5, config=config,
        )
        ids = {r.task_id for r in rows}
        assert ids == {"alpha/1"}

    def test_blockers_resolved_from_workspace_db(self, tmp_path: Path) -> None:
        project_path = tmp_path / "alpha"
        project_path.mkdir()
        ws = tmp_path / "ws"
        ws.mkdir()
        ws_db = _init_work_db(ws)
        _raw_insert_task(ws_db, project="alpha", task_number=1, title="A", work_status="blocked")
        _raw_insert_task(ws_db, project="alpha", task_number=2, title="B", work_status="in_progress")
        _raw_insert_dep(
            ws_db, from_project="alpha", from_task_number=1,
            to_project="alpha", to_task_number=2,
        )

        project = KnownProject(key="alpha", path=project_path, tracked=True)
        config = _make_empty_config(ws, workspace_root=ws)
        blockers = _gather_blockers_for_project(project, config=config)
        assert len(blockers) == 1
        assert blockers[0].task_id == "alpha/1"
        assert blockers[0].blocked_by == ["alpha/2"]

    def test_identify_priorities_end_to_end_workspace_db(self, tmp_path: Path) -> None:
        # End-to-end: priorities must be non-empty for an active workspace
        # whose only DB lives at the workspace root.
        ws = tmp_path / "ws"
        ws.mkdir()
        ws_db = _init_work_db(ws)
        _raw_insert_task(
            ws_db, project="alpha", task_number=1, title="open task",
            priority="critical", work_status="queued",
        )

        alpha_path = tmp_path / "alpha"
        alpha_path.mkdir()
        config = _make_empty_config(ws, workspace_root=ws)
        config.projects = {
            "alpha": KnownProject(key="alpha", path=alpha_path, tracked=True),
        }

        now_local = datetime(2026, 4, 16, 6, 0, tzinfo=UTC)
        priorities = identify_priorities(config, now_local=now_local, priorities_count=5)
        ids = {t.task_id for t in priorities.top_tasks}
        assert "alpha/1" in ids
