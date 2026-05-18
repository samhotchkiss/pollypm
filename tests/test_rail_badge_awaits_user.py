"""Rail badge / CLI / predicate alignment (#1571).

Pins the invariant that motivated the epic (#1564): the rail badge,
``pm inbox --awaits-user``, and the canonical
:func:`pollypm.inbox.awaits_user` predicate all return the same count on
the same DB. Before #1571 the rail badge ran a local
``triage_bucket == "action"`` regex heuristic while the CLI ran nothing
of the kind, so the three surfaces disagreed (97 / 44 / 133).

The test setup populates a workspace + a project with a mix of
:class:`InboxItemKind` values (some user-facing, some informational,
some legacy). The body of every assertion is the predicate, so any
regression that re-introduces a parallel "is actionable" definition
on the rail side fails here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from pollypm.cockpit_inbox import (
    _count_inbox_tasks_for_label,
    pm_inbox_awaits_user_list,
)
from pollypm.inbox import awaits_user
from pollypm.inbox.kind import InboxItemKind
from pollypm.work.inbox_cli import inbox_app
from pollypm.work.sqlite_service import SQLiteWorkService


runner = CliRunner()


def _write_config(
    workspace_root: Path, project_path: Path, config_path: Path,
) -> None:
    config_path.write_text(
        "[project]\n"
        'tmux_session = "pollypm-test"\n'
        f'workspace_root = "{workspace_root}"\n'
        "\n"
        "[projects.demo]\n"
        'key = "demo"\n'
        'name = "Demo"\n'
        f'path = "{project_path}"\n'
    )


def _seed_task(
    db_path: Path,
    project_path: Path,
    *,
    project: str,
    title: str,
    kind: str,
) -> str:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    svc = SQLiteWorkService(db_path=db_path, project_path=project_path)
    try:
        task = svc.create(
            title=title,
            description=f"body for {title}",
            type="task",
            project=project,
            flow_template="chat",
            roles={"requester": "user", "operator": "polly"},
            priority="normal",
            created_by="polly",
            kind=kind,
        )
        return task.task_id
    finally:
        svc.close()


def _seed_message(
    db_path: Path,
    *,
    scope: str,
    subject: str,
    kind: str,
) -> int:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    from pollypm.store import SQLAlchemyStore
    store = SQLAlchemyStore(f"sqlite:///{db_path}")
    try:
        return store.enqueue_message(
            type="notify",
            tier="immediate",
            scope=scope,
            sender="polly",
            recipient="user",
            subject=subject,
            body="body",
            kind=kind,
        )
    finally:
        store.close()


def _load_cfg(config_path: Path):
    from pollypm.config import load_config
    return load_config(config_path)


@pytest.fixture
def env(tmp_path: Path):
    workspace_root = tmp_path / "dev"
    workspace_root.mkdir()
    project_path = workspace_root / "demo"
    project_path.mkdir()
    (project_path / ".git").mkdir()
    config_path = tmp_path / "pollypm.toml"
    _write_config(workspace_root, project_path, config_path)
    return {
        "workspace_root": workspace_root,
        "project_path": project_path,
        "config_path": config_path,
        "project_db": project_path / ".pollypm" / "state.db",
        "workspace_db": workspace_root / ".pollypm" / "state.db",
    }


def test_empty_db_badge_and_predicate_both_zero(env) -> None:
    """A fresh DB with no rows trivially agrees on zero."""
    config = _load_cfg(env["config_path"])
    badge = _count_inbox_tasks_for_label(config)
    listed = pm_inbox_awaits_user_list(config)
    assert badge == 0
    assert listed == []


def test_mixed_kinds_badge_equals_predicate_count(env) -> None:
    """Pin the invariant: rail count == predicate-filtered list length.

    Seeds a deliberately mixed bag of inbox rows so the predicate has
    to do real work: three user-facing tasks (approval_request,
    pm_question_unanswered, manual_decision), two informational tasks
    that must NOT count (completion_fyi, info), one legacy task
    (counts under the migration safety default), and matching message
    rows on the workspace-root DB.
    """
    # Tasks on the per-project DB.
    _seed_task(
        env["project_db"], env["project_path"],
        project="demo", title="approve plan",
        kind=InboxItemKind.APPROVAL_REQUEST.value,
    )
    _seed_task(
        env["project_db"], env["project_path"],
        project="demo", title="pm asks something",
        kind=InboxItemKind.PM_QUESTION_UNANSWERED.value,
    )
    _seed_task(
        env["project_db"], env["project_path"],
        project="demo", title="manual call",
        kind=InboxItemKind.MANUAL_DECISION.value,
    )
    _seed_task(
        env["project_db"], env["project_path"],
        project="demo", title="shipped",
        kind=InboxItemKind.COMPLETION_FYI.value,
    )
    _seed_task(
        env["project_db"], env["project_path"],
        project="demo", title="just info",
        kind=InboxItemKind.INFO.value,
    )
    _seed_task(
        env["project_db"], env["project_path"],
        project="demo", title="legacy stub",
        kind=InboxItemKind.LEGACY.value,
    )

    # Messages on the workspace-root DB — same mix.
    _seed_message(
        env["workspace_db"], scope="inbox",
        subject="watchdog dispatch",
        kind=InboxItemKind.WATCHDOG_OPERATOR_DISPATCH.value,
    )
    _seed_message(
        env["workspace_db"], scope="inbox",
        subject="plan ready",
        kind=InboxItemKind.PLAN_REVIEW_PENDING.value,
    )
    _seed_message(
        env["workspace_db"], scope="inbox",
        subject="self-bug report",
        kind=InboxItemKind.SELF_BUG_REPORT.value,
    )
    _seed_message(
        env["workspace_db"], scope="inbox",
        subject="activity event",
        kind=InboxItemKind.ACTIVITY_EVENT.value,
    )

    config = _load_cfg(env["config_path"])
    badge = _count_inbox_tasks_for_label(config)
    listed = pm_inbox_awaits_user_list(config)
    awaits_count = len([item for item in listed if awaits_user(item)])

    # Tasks: 3 user-facing + 1 legacy = 4
    # Messages: 1 watchdog + 1 plan-review = 2
    # Total = 6
    assert badge == 6, f"rail badge expected 6, got {badge}"
    assert badge == awaits_count, (
        f"rail badge ({badge}) must equal awaits_user-filtered list "
        f"length ({awaits_count})"
    )
    # ``pm_inbox_awaits_user_list`` already filters; the re-filter in
    # the assertion above is the invariant the issue spec pins.
    assert len(listed) == awaits_count


def test_cli_awaits_user_flag_matches_predicate(env) -> None:
    """``pm inbox --awaits-user`` filters the listing to the predicate set.

    Single-DB scope: the CLI operates on one DB at a time so the
    comparison is against the rows we seed on that DB.
    """
    # One user-facing + one informational message in the per-project DB.
    _seed_message(
        env["project_db"], scope="demo",
        subject="approve me",
        kind=InboxItemKind.APPROVAL_REQUEST.value,
    )
    _seed_message(
        env["project_db"], scope="demo",
        subject="shipped",
        kind=InboxItemKind.COMPLETION_FYI.value,
    )

    # Default ``pm inbox`` (without ``--all``) hides notify-type FYIs
    # already, but ``--awaits-user`` is the canonical lens — assert
    # both that the actionable row shows up and the FYI does not.
    result = runner.invoke(
        inbox_app, ["--awaits-user", "--all", "--db", str(env["project_db"])],
    )
    assert result.exit_code == 0, result.output
    assert "approve me" in result.output
    assert "shipped" not in result.output


def test_cli_awaits_user_excludes_legacy_when_not_seeded(env) -> None:
    """A row tagged with an informational kind drops out of the CLI listing."""
    _seed_message(
        env["project_db"], scope="demo",
        subject="just info",
        kind=InboxItemKind.INFO.value,
    )
    result = runner.invoke(
        inbox_app, ["--awaits-user", "--all", "--db", str(env["project_db"])],
    )
    assert result.exit_code == 0, result.output
    assert "just info" not in result.output
    # Inbox header still rendered, even on empty result.
    assert "Inbox:" in result.output
