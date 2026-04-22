from __future__ import annotations

import json
import textwrap
from pathlib import Path

from pollypm.plugins_builtin.activity_feed.handlers.event_projector import EventProjector
from pollypm.store import SQLAlchemyStore
from pollypm.work.models import Artifact, ArtifactKind, OutputType, WorkOutput
from pollypm.work.sqlite_service import SQLiteWorkService, first_shipped_at


def _write_first_shipped_flow(root: Path) -> None:
    flows_dir = root / ".pollypm" / "flows"
    flows_dir.mkdir(parents=True, exist_ok=True)
    (flows_dir / "first-shipped.yaml").write_text(
        textwrap.dedent(
            """\
            name: first-shipped
            description: First shipped flow
            roles:
              worker:
                description: Implements
              reviewer:
                description: Reviews
            nodes:
              implement:
                type: work
                actor_type: role
                actor_role: worker
                next_node: review
              review:
                type: review
                actor_type: role
                actor_role: reviewer
                next_node: done
                reject_node: implement
                gates: [has_work_output]
              done:
                type: terminal
            start_node: implement
            """
        )
    )


def test_first_shipped_records_once_on_commit_approval(tmp_path: Path, monkeypatch) -> None:
    state_file = tmp_path / "state.json"
    monkeypatch.setattr("pollypm.work.sqlite_service.state_path", lambda: state_file)
    _write_first_shipped_flow(tmp_path)

    svc = SQLiteWorkService(db_path=tmp_path / "work.db", project_path=tmp_path)
    try:
        task = svc.create(
            title="Ship it",
            description="Do the thing",
            type="task",
            project="demo",
            flow_template="first-shipped",
            roles={"worker": "alice", "reviewer": "polly"},
            priority="normal",
            created_by="tester",
        )
        svc.queue(task.task_id, "pm")
        svc.claim(task.task_id, "alice")
        svc.node_done(
            task.task_id,
            "alice",
            WorkOutput(
                type=OutputType.CODE_CHANGE,
                summary="Implemented the feature",
                artifacts=[
                    Artifact(
                        kind=ArtifactKind.COMMIT,
                        description="Land commit",
                        ref="abc1234",
                    )
                ],
            ),
        )

        svc.approve(task.task_id, "polly", "looks good")
        first = first_shipped_at(state_file)
        assert first is not None
        assert svc.last_first_shipped_created is True
        state_db = tmp_path / ".pollypm" / "state.db"
        store = SQLAlchemyStore(f"sqlite:///{state_db}")
        try:
            activity_rows = [
                row
                for row in store.query_messages(type="event", scope="polly")
                if (row.get("payload") or {}).get("kind") == "first_shipped"
            ]
        finally:
            store.close()
        assert len(activity_rows) == 1
        assert activity_rows[0]["payload"]["pinned"] is True

        second_task = svc.create(
            title="Ship again",
            description="Do the thing again",
            type="task",
            project="demo",
            flow_template="first-shipped",
            roles={"worker": "alice", "reviewer": "polly"},
            priority="normal",
            created_by="tester",
        )
        svc.queue(second_task.task_id, "pm")
        svc.claim(second_task.task_id, "alice")
        svc.node_done(
            second_task.task_id,
            "alice",
            WorkOutput(
                type=OutputType.CODE_CHANGE,
                summary="Implemented the feature again",
                artifacts=[
                    Artifact(
                        kind=ArtifactKind.COMMIT,
                        description="Land commit",
                        ref="def5678",
                    )
                ],
            ),
        )
        svc.approve(second_task.task_id, "polly", "ship again")

        assert first_shipped_at(state_file) == first
        assert svc.last_first_shipped_created is False
        persisted = json.loads(state_file.read_text())
        assert persisted["first_shipped_at"] == first
    finally:
        svc.close()

    reopened = SQLiteWorkService(db_path=tmp_path / "work.db", project_path=tmp_path)
    try:
        entries = EventProjector(tmp_path / ".pollypm" / "state.db").project(limit=20)
        first_shipped_entries = [entry for entry in entries if entry.kind == "first_shipped"]
        assert len(first_shipped_entries) == 1
        assert first_shipped_entries[0].summary == "First PR shipped with Polly 🎉"
        assert first_shipped_entries[0].payload["pinned"] is True
    finally:
        reopened.close()
