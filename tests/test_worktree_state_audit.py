"""Tests for the 10-minute ``worktree.state_audit`` recurring handler (#251).

Run with:

    HOME=/tmp/pytest-agent-worktree-audit \
        uv run pytest tests/test_worktree_state_audit.py -x

Two test surfaces:

1. ``classify_worktree_state(path)`` — pure, subprocess-only classifier.
   Seed real tmp git repos in each state, assert the enum + metadata.
2. ``worktree_state_audit_handler(payload)`` — handler path. Uses a
   fake work-service + in-memory state store to verify alerts fire on
   bad states, inbox tasks are created for merge conflicts, and
   open alerts auto-clear when the state returns to ``clean``.
"""

from __future__ import annotations

from contextlib import contextmanager

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from pollypm.worktree_audit import (
    WorktreeState,
    classify_worktree_state,
)


def _fake_load_cm(config, store):
    """Context-manager mock matching the real @contextmanager _load_config_and_store."""
    @contextmanager
    def _cm(payload):
        yield config, store
    return _cm


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _run(*args: str, cwd: Path | None = None, check: bool = True):
    return subprocess.run(
        list(args),
        cwd=str(cwd) if cwd else None,
        check=check,
        capture_output=True,
        text=True,
    )


def _make_repo(root: Path) -> Path:
    """Initialize a real git repo with one commit on ``main``."""
    root.mkdir(parents=True, exist_ok=True)
    _run("git", "init", "-b", "main", str(root))
    _run("git", "-C", str(root), "config", "user.email", "audit@test")
    _run("git", "-C", str(root), "config", "user.name", "Audit Test")
    (root / "README.md").write_text("seed\n")
    _run("git", "-C", str(root), "add", "README.md")
    _run("git", "-C", str(root), "commit", "-m", "init")
    return root


def _add_worktree(repo: Path, slug: str) -> Path:
    """Add a git worktree under ``<repo>/.claude/worktrees/agent-<slug>``."""
    worktrees_dir = repo / ".claude" / "worktrees"
    worktrees_dir.mkdir(parents=True, exist_ok=True)
    wt_path = worktrees_dir / f"agent-{slug}"
    branch = f"audit-{slug}"
    _run("git", "-C", str(repo), "worktree", "add", "-b", branch, str(wt_path))
    return wt_path


# ---------------------------------------------------------------------------
# Classifier tests
# ---------------------------------------------------------------------------


class TestClassifier:
    def test_clean_worktree(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path / "repo")
        wt = _add_worktree(repo, "clean")
        # Make sure the branch has an upstream to avoid orphan classification.
        # We fake one with a bare "remote" clone.
        remote = tmp_path / "remote.git"
        _run("git", "init", "--bare", "-b", "main", str(remote))
        _run("git", "-C", str(wt), "remote", "add", "origin", str(remote))
        _run("git", "-C", str(wt), "push", "-u", "origin", "audit-clean")

        result = classify_worktree_state(wt)
        assert result.state is WorktreeState.CLEAN
        assert result.branch == "audit-clean"

    def test_dirty_expected(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path / "repo")
        wt = _add_worktree(repo, "dirty")
        (wt / "scratch.txt").write_text("in progress\n")

        result = classify_worktree_state(wt)
        assert result.state is WorktreeState.DIRTY_EXPECTED
        assert result.metadata["dirty_line_count"] >= 1

    def test_merge_conflict(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path / "repo")
        # Create two branches that edit the same line, then try to merge.
        wt = _add_worktree(repo, "conflict")
        (wt / "file.txt").write_text("left\n")
        _run("git", "-C", str(wt), "add", "file.txt")
        _run("git", "-C", str(wt), "commit", "-m", "left side")
        # Meanwhile on main:
        (repo / "file.txt").write_text("right\n")
        _run("git", "-C", str(repo), "add", "file.txt")
        _run("git", "-C", str(repo), "commit", "-m", "right side")
        # Merge main into the worktree branch — expect conflict.
        merge = _run(
            "git", "-C", str(wt), "merge", "main", check=False,
        )
        assert merge.returncode != 0, "merge should have conflicted"

        result = classify_worktree_state(wt)
        assert result.state is WorktreeState.MERGE_CONFLICT
        assert "file.txt" in result.metadata.get("conflict_files", [])

    def test_detached_head(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path / "repo")
        wt = _add_worktree(repo, "detached")
        # Add a second commit so we have a sha to detach to.
        (wt / "f.txt").write_text("x\n")
        _run("git", "-C", str(wt), "add", "f.txt")
        _run("git", "-C", str(wt), "commit", "-m", "c2")
        sha = _run("git", "-C", str(wt), "rev-parse", "HEAD").stdout.strip()
        _run("git", "-C", str(wt), "checkout", "--detach", sha)

        result = classify_worktree_state(wt)
        assert result.state is WorktreeState.DETACHED_HEAD
        assert result.branch is None
        assert result.metadata.get("head_sha")

    def test_lock_file(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path / "repo")
        wt = _add_worktree(repo, "locked")
        # Resolve the real gitdir for the worktree (``.git`` is a file).
        git_entry = wt / ".git"
        gitdir_text = git_entry.read_text().strip()
        assert gitdir_text.startswith("gitdir:")
        raw = gitdir_text.split(":", 1)[1].strip()
        gitdir = Path(raw)
        if not gitdir.is_absolute():
            gitdir = (git_entry.parent / gitdir).resolve()
        lock = gitdir / "index.lock"
        lock.write_text("")
        # Back-date the lock so we can assert a non-zero age.
        target = time.time() - 120
        os.utime(lock, (target, target))

        result = classify_worktree_state(wt)
        assert result.state is WorktreeState.LOCK_FILE
        assert result.metadata.get("lock_age_seconds", 0) >= 60

    def test_orphan_branch(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path / "repo")
        wt = _add_worktree(repo, "orphan")
        # No upstream, no remote. We need a commit older than 7 days —
        # use ``commit --date`` + ``GIT_COMMITTER_DATE`` to back-date.
        (wt / "old.txt").write_text("old\n")
        _run("git", "-C", str(wt), "add", "old.txt")
        env = os.environ.copy()
        old_date = "2020-01-01T00:00:00"
        env["GIT_AUTHOR_DATE"] = old_date
        env["GIT_COMMITTER_DATE"] = old_date
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", "old", "--date", old_date],
            check=True, env=env, capture_output=True, text=True,
        )

        result = classify_worktree_state(wt)
        assert result.state is WorktreeState.ORPHAN_BRANCH
        assert result.metadata.get("age_days", 0) > 7
        assert result.metadata.get("has_upstream") is False

    def test_missing_path(self, tmp_path: Path) -> None:
        result = classify_worktree_state(tmp_path / "does-not-exist")
        assert result.state is WorktreeState.MISSING

    def test_not_a_git_worktree(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        (plain / "a.txt").write_text("hi")
        result = classify_worktree_state(plain)
        assert result.state is WorktreeState.MISSING


# ---------------------------------------------------------------------------
# Handler tests — use a fake work-service + in-memory state store so we
# can assert alert / inbox side effects without standing up the full rail.
# ---------------------------------------------------------------------------


@dataclass
class _FakeSession:
    task_project: str
    task_number: int
    agent_name: str
    worktree_path: str | None
    pane_id: str | None = None
    branch_name: str | None = None
    started_at: str = "2026-01-01T00:00:00Z"


class _FakeStore:
    """In-memory stand-in for ``StateStore`` — captures alert calls."""

    def __init__(self) -> None:
        # dict keyed by (session_name, alert_type) → (severity, message, status)
        self.alerts: dict[tuple[str, str], dict[str, Any]] = {}

    # StateStore API surface the handler touches
    def upsert_alert(
        self, session_name: str, alert_type: str, severity: str, message: str,
    ) -> None:
        self.alerts[(session_name, alert_type)] = {
            "severity": severity,
            "message": message,
            "status": "open",
        }

    def clear_alert(self, session_name: str, alert_type: str) -> None:
        key = (session_name, alert_type)
        if key in self.alerts:
            self.alerts[key]["status"] = "cleared"

    def execute(self, query: str, params: tuple = ()):  # noqa: ARG002
        """Minimal SELECT-open-alert emulation used by the handler's
        clear/stale-probe branches. Returns a cursor-like whose
        fetchone() yields (id,) iff we have an open alert for the
        queried session_name/alert_type.
        """
        session_name, alert_type = params
        entry = self.alerts.get((session_name, alert_type))
        is_open = bool(entry and entry.get("status") == "open")

        class _Cur:
            def fetchone(self_inner):
                return (1,) if is_open else None

        return _Cur()

    # Post-#342 alert-exists probe: the handler queries the unified
    # ``messages`` table rather than raw SQL on the legacy ``alerts``
    # table. Return a messages-shaped dict when we hold an open alert.
    def query_messages(self, **filters: Any) -> list[dict[str, Any]]:
        if filters.get("type") != "alert":
            return []
        if filters.get("state") != "open":
            return []
        scope = filters.get("scope")
        sender = filters.get("sender")
        if scope is None or sender is None:
            return []
        entry = self.alerts.get((scope, sender))
        if not entry or entry.get("status") != "open":
            return []
        return [
            {
                "id": 1,
                "scope": scope,
                "sender": sender,
                "type": "alert",
                "state": "open",
                "subject": entry.get("message", ""),
                "body": "",
                "payload": {"severity": entry.get("severity", "")},
            }
        ]


class _FakeWork:
    """Minimal SQLiteWorkService stand-in — only the two methods the
    handler uses (``list_worker_sessions`` + ``create`` + ``list_tasks``)."""

    def __init__(self, sessions: list[_FakeSession]) -> None:
        self._sessions = list(sessions)
        self.created: list[dict[str, Any]] = []

    def list_worker_sessions(self, *, active_only: bool = True):  # noqa: ARG002
        return list(self._sessions)

    def list_tasks(self, **_kwargs: Any):
        return []

    def create(self, **kwargs: Any):
        self.created.append(kwargs)
        # Mimic Task just enough — the handler doesn't read the result.
        return object()

    def close(self) -> None:
        pass


def _invoke_handler_with_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    sessions: list[_FakeSession],
    project_root: Path,
) -> tuple[dict[str, Any], _FakeStore, _FakeWork]:
    """Run the handler with the SQLiteWorkService + _load_config_and_store
    swapped for fakes. Returns (result, store, work) so assertions can
    inspect side effects."""
    from pollypm.plugins_builtin.core_recurring import plugin as plugin_module

    fake_store = _FakeStore()
    fake_work = _FakeWork(sessions)

    @dataclass
    class _FakeProject:
        root_dir: Path

    @dataclass
    class _FakeConfig:
        project: _FakeProject

    cfg = _FakeConfig(project=_FakeProject(root_dir=project_root))

    monkeypatch.setattr(
        plugin_module, "_load_config_and_store",
        _fake_load_cm(cfg, fake_store),
    )
    # Patch SQLiteWorkService to return our fake regardless of args.
    import pollypm.work.sqlite_service as sqlite_service_mod

    monkeypatch.setattr(
        sqlite_service_mod, "SQLiteWorkService",
        lambda *a, **kw: fake_work,
    )

    result = plugin_module.worktree_state_audit_handler({})
    return result, fake_store, fake_work


class TestHandler:
    def test_clean_worktree_raises_no_alerts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = _make_repo(tmp_path / "repo")
        wt = _add_worktree(repo, "clean")
        remote = tmp_path / "remote.git"
        _run("git", "init", "--bare", "-b", "main", str(remote))
        _run("git", "-C", str(wt), "remote", "add", "origin", str(remote))
        _run("git", "-C", str(wt), "push", "-u", "origin", "audit-clean")

        sessions = [
            _FakeSession(
                task_project="demo", task_number=1,
                agent_name="worker-demo-1", worktree_path=str(wt),
            ),
        ]
        result, store, work = _invoke_handler_with_fakes(
            monkeypatch, sessions=sessions, project_root=repo,
        )
        assert result["outcome"] == "swept"
        assert result["considered"] == 1
        assert result["classified"].get("clean") == 1
        # No alerts raised; no inbox.
        assert result["alerts_raised"] == 0
        assert work.created == []
        # Store has no open alerts.
        assert all(
            entry["status"] != "open" for entry in store.alerts.values()
        )

    def test_merge_conflict_raises_alert_and_inbox(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = _make_repo(tmp_path / "repo")
        wt = _add_worktree(repo, "conflict")
        (wt / "file.txt").write_text("left\n")
        _run("git", "-C", str(wt), "add", "file.txt")
        _run("git", "-C", str(wt), "commit", "-m", "L")
        (repo / "file.txt").write_text("right\n")
        _run("git", "-C", str(repo), "add", "file.txt")
        _run("git", "-C", str(repo), "commit", "-m", "R")
        merge = _run("git", "-C", str(wt), "merge", "main", check=False)
        assert merge.returncode != 0

        sessions = [
            _FakeSession(
                task_project="demo", task_number=7,
                agent_name="worker-demo-7", worktree_path=str(wt),
            ),
        ]
        result, store, work = _invoke_handler_with_fakes(
            monkeypatch, sessions=sessions, project_root=repo,
        )
        assert result["classified"].get("merge_conflict") == 1
        assert result["alerts_raised"] >= 1
        assert result["inbox_emitted"] == 1
        # Alert keyed by (agent, worktree_state:demo/7:merge_conflict).
        key = ("worker-demo-7", "worktree_state:demo/7:merge_conflict")
        assert key in store.alerts
        assert store.alerts[key]["severity"] == "error"
        # Inbox task created with the audit label.
        assert len(work.created) == 1
        created = work.created[0]
        assert created["project"] == "demo"
        assert "audit:worktree_state" in created["labels"]
        assert any(
            lbl.startswith("worktree_audit:demo/7:merge_conflict")
            for lbl in created["labels"]
        )

    def test_lock_file_escalates_after_5min(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = _make_repo(tmp_path / "repo")
        wt = _add_worktree(repo, "locked")
        git_entry = wt / ".git"
        raw = git_entry.read_text().strip().split(":", 1)[1].strip()
        gitdir = Path(raw)
        if not gitdir.is_absolute():
            gitdir = (git_entry.parent / gitdir).resolve()
        lock = gitdir / "index.lock"
        lock.write_text("")
        # 10min old — should escalate to error.
        target = time.time() - 10 * 60
        os.utime(lock, (target, target))

        sessions = [
            _FakeSession(
                task_project="demo", task_number=3,
                agent_name="worker-demo-3", worktree_path=str(wt),
            ),
        ]
        result, store, _work = _invoke_handler_with_fakes(
            monkeypatch, sessions=sessions, project_root=repo,
        )
        assert result["classified"].get("lock_file") == 1
        key = ("worker-demo-3", "worktree_state:demo/3:lock_file")
        assert store.alerts[key]["severity"] == "error"

    def test_clean_after_dirty_clears_existing_alert(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Alert raised on a prior dirty_stale sweep auto-clears when
        the worktree returns to clean."""
        repo = _make_repo(tmp_path / "repo")
        wt = _add_worktree(repo, "recover")
        remote = tmp_path / "remote.git"
        _run("git", "init", "--bare", "-b", "main", str(remote))
        _run("git", "-C", str(wt), "remote", "add", "origin", str(remote))
        _run("git", "-C", str(wt), "push", "-u", "origin", "audit-recover")

        sessions = [
            _FakeSession(
                task_project="demo", task_number=9,
                agent_name="worker-demo-9", worktree_path=str(wt),
            ),
        ]
        # Prime the store with a stale open alert simulating a prior sweep.
        from pollypm.plugins_builtin.core_recurring import plugin as plugin_module

        fake_store = _FakeStore()
        fake_work = _FakeWork(sessions)
        fake_store.upsert_alert(
            "worker-demo-9",
            "worktree_state:demo/9:dirty_stale",
            "warn",
            "legacy alert",
        )

        @dataclass
        class _FakeProject:
            root_dir: Path

        @dataclass
        class _FakeConfig:
            project: _FakeProject

        cfg = _FakeConfig(project=_FakeProject(root_dir=repo))
        monkeypatch.setattr(
            plugin_module, "_load_config_and_store",
            _fake_load_cm(cfg, fake_store),
        )
        # #342: handler reads/writes alerts through the unified Store;
        # point ``_open_msg_store`` at the fake so existence probes hit
        # the same in-memory dict the upsert/clear writes land on.
        monkeypatch.setattr(
            plugin_module, "_open_msg_store", lambda _config: fake_store,
        )
        monkeypatch.setattr(
            plugin_module, "_close_msg_store", lambda _store: None,
        )
        import pollypm.work.sqlite_service as sqlite_service_mod

        monkeypatch.setattr(
            sqlite_service_mod, "SQLiteWorkService",
            lambda *a, **kw: fake_work,
        )

        result = plugin_module.worktree_state_audit_handler({})
        assert result["classified"].get("clean") == 1
        # Prior alert cleared.
        key = ("worker-demo-9", "worktree_state:demo/9:dirty_stale")
        assert fake_store.alerts[key]["status"] == "cleared"
        assert result["alerts_cleared"] >= 1
