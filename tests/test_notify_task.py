"""Unit tests for :mod:`pollypm.notify_task` (#1003, #1013, #1020)."""

from __future__ import annotations

from datetime import UTC, datetime

from pollypm.notify_task import (
    NOTIFY_LABEL,
    is_notify_inbox_task,
    is_notify_only_inbox_entry,
)
from pollypm.work.models import Priority, Task, TaskType, WorkStatus


def _make_task(
    *,
    title: str = "Plan ready for review: demo",
    labels: list[str] | None = None,
    flow_template_id: str | None = "chat",
    roles: dict[str, str] | None = None,
) -> Task:
    return Task(
        project="demo",
        task_number=7,
        title=title,
        type=TaskType.TASK,
        labels=list(labels or []),
        work_status=WorkStatus.DRAFT,
        flow_template_id=flow_template_id,
        flow_template_version=1 if flow_template_id else None,
        current_node_id=None,
        priority=Priority.HIGH,
        roles=dict(roles or {"requester": "user", "operator": "architect"}),
        created_at=datetime(2026, 4, 30, 10, 0, tzinfo=UTC),
        created_by="architect",
        updated_at=datetime(2026, 4, 30, 10, 0, tzinfo=UTC),
    )


class TestIsNotifyInboxTask:
    def test_pm_notify_immediate_task_is_inbox_only(self) -> None:
        """``pm notify --priority immediate`` always tags the task with
        ``notify`` and a sidecar ``notify_message:<id>`` (see
        ``cli_features/session_runtime.py``). Label-based detection is
        the historical signature kept as a backstop after #1020 made
        ``roles.operator == "user"`` the primary discriminator.
        """
        task = _make_task(
            labels=[NOTIFY_LABEL, "notify_message:42"],
        )
        assert is_notify_inbox_task(task) is True

    def test_plan_review_task_is_inbox_only(self) -> None:
        """The architect's stage-7 plan_review handoff carries a richer
        label set — plan_review, project:<key>, plan_task:<id>,
        explainer:<path> — alongside the bare ``notify`` marker.
        Filtering still keys off the ``notify`` label so we don't have
        to teach the filter every plan_review-shaped variant.
        """
        task = _make_task(
            labels=[
                "plan_review",
                "project:demo",
                "plan_task:demo/1",
                "explainer:/tmp/demo-plan-review.html",
                NOTIFY_LABEL,
                "notify_message:42",
            ],
        )
        assert is_notify_inbox_task(task) is True

    def test_real_work_task_is_not_filtered(self) -> None:
        """A normal plan_project / standard task without the ``notify``
        label MUST stay visible. The filter intentionally over-favours
        keeping work tasks visible — false positives in the Tasks view
        are far worse than false negatives.
        """
        task = _make_task(
            title="Implement module 3",
            labels=["project:demo"],
            flow_template_id="plan_project",
            roles={"worker": "worker", "reviewer": "reviewer", "requester": "architect"},
        )
        assert is_notify_inbox_task(task) is False

    def test_chat_flow_without_notify_label_is_not_filtered(self) -> None:
        """Chat-flow alone is not enough — real PM/user chat threads
        also use the ``chat`` template and need to remain visible.
        """
        task = _make_task(
            title="Discussion on demo",
            labels=["chat:user", "project:demo"],
            flow_template_id="chat",
            roles={"requester": "user", "operator": "polly"},
        )
        assert is_notify_inbox_task(task) is False

    def test_missing_labels_attr_returns_false(self) -> None:
        class _BareTask:
            pass

        assert is_notify_inbox_task(_BareTask()) is False

    # ------------------------------------------------------------------
    # #1020 — structural signal: ``roles.operator == "user"`` means the
    # task is addressed to the human, not assigned to a worker. Every
    # writer that does this means "this is a notification". Catches the
    # rejection-feedback rows that label-only detection missed.
    # ------------------------------------------------------------------

    def test_rejection_feedback_task_hidden_by_operator_user(self) -> None:
        """The bikepath/17, bikepath/18 case: reviewer's "Rejected …"
        feedback writer sets ``roles.operator = "user"`` but does NOT
        carry the ``notify`` label. Pre-#1020 the filter let these
        through to ``pm task list``. Post-#1020 the structural signal
        catches them.
        """
        task = _make_task(
            title="Rejected bikepath/8 — Implement module 1: Project Scaffold",
            labels=["review_feedback", "task:bikepath/8", "project:bikepath"],
            flow_template_id=None,
            roles={"requester": "reviewer", "operator": "user"},
        )
        assert is_notify_inbox_task(task) is True

    def test_done_emitted_task_hidden_by_title_prefix(self) -> None:
        """Heartbeat / done emitter writes ``Done: <task>`` rows. They
        may not always set ``operator = "user"`` (older writers), but
        the title prefix is a reliable signal so we catch them via the
        title-prefix backup. Regression guard for the bikepath/15-16
        case noted in #1020.
        """
        task = _make_task(
            title="Done: bikepath/8 — module 1",
            labels=["project:bikepath"],
            flow_template_id=None,
            roles={"requester": "worker"},
        )
        assert is_notify_inbox_task(task) is True

    def test_supervisor_heartbeat_alert_hidden(self) -> None:
        """Supervisor / heartbeat emits ``URGENT:`` / ``Heartbeat:``
        framed tasks. Title prefix matching covers them.
        """
        for prefix_title in (
            "URGENT: bikepath worker stuck for 30m",
            "Heartbeat: bikepath/3 awaiting review",
            "Repeated stale heartbeat on bikepath",
            "Nth retry exhausted on bikepath/9",
            "Project: bikepath paused",
            "Queued: bikepath/12 ready to claim",
        ):
            task = _make_task(
                title=prefix_title,
                labels=["project:bikepath"],
                flow_template_id=None,
                roles={"requester": "supervisor"},
            )
            assert is_notify_inbox_task(task) is True, (
                f"title-prefix {prefix_title!r} should be filtered as a notify"
            )

    def test_real_worker_assigned_task_surfaced(self) -> None:
        """The negative case: a real worker-assigned implementation task
        must still surface. ``roles.operator`` is a worker-shaped role,
        title doesn't start with a notify prefix, no ``notify`` label.
        """
        task = _make_task(
            title="Implement module 1: Project Scaffold",
            labels=[
                "implement_module",
                "plan_task:bikepath/1",
                "module:project-scaffold",
            ],
            flow_template_id=None,
            roles={
                "worker": "worker",
                "reviewer": "reviewer",
                "requester": "architect",
            },
        )
        assert is_notify_inbox_task(task) is False

    def test_roles_serialised_as_json_string_still_handled(self) -> None:
        """Defensive: a partially-hydrated row may surface ``roles`` as
        a JSON string instead of a dict. The coercer should still read
        ``operator`` correctly so the filter doesn't crash on bad rows.
        """

        class _StringRolesTask:
            roles = '{"requester": "reviewer", "operator": "user"}'
            labels: list[str] = []
            title = "Rejected bikepath/8"

        assert is_notify_inbox_task(_StringRolesTask()) is True

    def test_corrupt_roles_falls_back_to_other_signals(self) -> None:
        """If ``roles`` is a malformed JSON string, the predicate must
        not crash — it falls back to label / title-prefix matching.
        """

        class _BadRolesTask:
            roles = "not-json"
            labels = [NOTIFY_LABEL]
            title = "Plan ready for review: demo"

        assert is_notify_inbox_task(_BadRolesTask()) is True


class TestIsNotifyOnlyInboxEntry:
    """#1027 — predicate that decides whether a row is a default-hide FYI.

    Backs both the cockpit Inbox panel's ``n``-toggle and the ``pm
    inbox`` CLI's ``--all`` opt-in. Returns True for the bulk of
    completion / heartbeat noise (``msg:639  notify  Done: …``,
    ``msg:633  notify  Repeated stale review ping``) so they stay
    behind the toggle while real action items, alerts, and tasks
    surface immediately.
    """

    @staticmethod
    def _entry(**values):
        # Plain object instead of the InboxEntry adapter so the test
        # stays independent of the cockpit module — the predicate
        # only reads attributes via getattr.
        class _E:
            pass

        e = _E()
        for key, value in values.items():
            setattr(e, key, value)
        return e

    def test_notify_message_is_notify_only(self) -> None:
        """``type=notify`` message rows are the canonical FYI surface."""
        entry = self._entry(
            source="message",
            message_type="notify",
            title="Done: Phase 2 rework resubmitted",
            labels=[],
        )
        assert is_notify_only_inbox_entry(entry) is True

    def test_alert_message_stays_visible(self) -> None:
        """``type=alert`` messages carry actionable supervisor / heartbeat
        signals; they must not be hidden by the default lens.
        """
        entry = self._entry(
            source="message",
            message_type="alert",
            title="Worker bikepath/3 stuck for 30m",
            labels=[],
        )
        assert is_notify_only_inbox_entry(entry) is False

    def test_inbox_task_message_stays_visible(self) -> None:
        """``type=inbox_task`` messages are a structured action surface
        and must remain visible by default.
        """
        entry = self._entry(
            source="message",
            message_type="inbox_task",
            title="Review changes for bikepath/4",
            labels=[],
        )
        assert is_notify_only_inbox_entry(entry) is False

    def test_uppercase_message_type_is_normalised(self) -> None:
        """The store may surface the type in any case; predicate is
        case-insensitive so a row tagged ``NOTIFY`` still hides.
        """
        entry = self._entry(
            source="message",
            message_type="NOTIFY",
            title="Done: shipped",
            labels=[],
        )
        assert is_notify_only_inbox_entry(entry) is True

    def test_real_work_task_is_not_notify_only(self) -> None:
        """A normal work-service task must remain visible by default."""
        task = _make_task(
            title="Implement module 3",
            labels=["project:demo"],
            flow_template_id="plan_project",
            roles={"worker": "worker", "reviewer": "reviewer", "requester": "architect"},
        )
        assert is_notify_only_inbox_entry(task) is False

    def test_notify_stub_task_is_notify_only(self) -> None:
        """Notify-stub tasks (label / title) defer to is_notify_inbox_task."""
        task = _make_task(labels=[NOTIFY_LABEL])
        assert is_notify_only_inbox_entry(task) is True
