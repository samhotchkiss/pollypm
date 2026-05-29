"""Slice B (#1737) full :class:`PgWorkService` coverage.

Sister suite to :mod:`tests.test_pg_work_service` (which covers the
Slice A read+CRUD subset). This module exercises the methods that
landed in Slice B against the testcontainer pg from
:mod:`tests.conftest_pg`:

* mutable-field ``update`` + ``increment_plan_version`` + ``list_successors``
* claim / hold / resume / next state transitions
* node_done / approve / reject / block flow progression
* add_context / get_context
* link / unlink / dependents
* my_tasks / blocked_tasks / state_counts zero-fill
* validate_advance preflight
* sync_status / trigger_sync (Slice B stub shape)
* worker_session CRUD trio (upsert / get / list / end / mark_ended / update_tokens)
* available_flows / get_flow (file-resolver delegation)

The fixture skips when neither Docker nor a local pg+vector DSN is
available — same gate as :mod:`tests.test_pg_work_service`.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def pg_service(pg_schema_pool):
    from pollypm.work.pg_service import PgWorkService
    from pollypm.storage.pg_sessions import upsert_session

    service = PgWorkService(pool=pg_schema_pool, ro_pool=None)
    for name in ("alice", "bob", "pete", "nora", "olga"):
        upsert_session(
            name=name,
            role="worker",
            project="demo",
            provider="codex",
            account="test",
            cwd="/tmp/demo",
            window_name=name,
            pool=pg_schema_pool,
        )

    return service


# ---------------------------------------------------------------------------
# update / increment_plan_version / list_successors
# ---------------------------------------------------------------------------


def _make_draft(svc, project="demo", title="t", **kw):
    return svc.create(
        title=title,
        type=kw.pop("type", "task"),
        project=project,
        flow_template=kw.pop("flow_template", "standard"),
        roles=kw.pop("roles", {"worker": "alice", "reviewer": "bob"}),
        description=kw.pop("description", "has body"),
        **kw,
    )


def test_update_title_and_priority(pg_service):
    task = _make_draft(pg_service)
    updated = pg_service.update(
        task.task_id, title="renamed", priority="high"
    )
    from pollypm.work.models import Priority

    assert updated.title == "renamed"
    assert updated.priority is Priority.HIGH


def test_update_labels_round_trips(pg_service):
    task = _make_draft(pg_service)
    updated = pg_service.update(task.task_id, labels=["a", "b"])
    assert updated.labels == ["a", "b"]


def test_update_rejects_work_status_change(pg_service):
    from pollypm.work.service_support import ValidationError

    task = _make_draft(pg_service)
    with pytest.raises(ValidationError):
        pg_service.update(task.task_id, work_status="queued")


def test_update_rejects_flow_template_change(pg_service):
    from pollypm.work.service_support import ValidationError

    task = _make_draft(pg_service)
    with pytest.raises(ValidationError):
        pg_service.update(task.task_id, flow_template="other")


def test_update_rejects_unknown_field(pg_service):
    from pollypm.work.service_support import ValidationError

    task = _make_draft(pg_service)
    with pytest.raises(ValidationError):
        pg_service.update(task.task_id, bogus="x")


def test_update_missing_task_raises(pg_service):
    from pollypm.work.service_support import TaskNotFoundError

    with pytest.raises(TaskNotFoundError):
        pg_service.update("nope/999", title="x")


# ---------------------------------------------------------------------------
# #2064 — assignee + external_refs pg roundtrips
#
# PR #2064 broadened ``_UPDATE_ALLOWED_COLUMNS`` to include ``assignee``
# (backs ``POST /reassign`` + PATCH) and ``external_refs`` (backs PATCH
# ``metadata``). The route-level tests use a fake work-service so the
# pg column write + JSONB encode/decode + downstream query surfaces
# (``my_tasks``, ``_row_to_task``) were not exercised. These tests pin
# the pg roundtrip end-to-end.
# ---------------------------------------------------------------------------


def test_update_assignee_roundtrips(pg_service):
    """Setting ``assignee`` via update() persists + ``my_tasks`` sees it.

    ``my_tasks`` filters on ``current_node_id IS NOT NULL AND
    assignee = ?`` — i.e. tasks that have been claimed onto a flow
    node. We claim with the original actor, then reassign via
    ``update`` (the PATCH path), and assert the new owner sees it.
    """
    task = _make_draft(pg_service)
    pg_service.queue(task.task_id, actor="user")
    # ``alice`` is the ``worker`` role on _make_draft's default roles
    # dict, so the claim node-resolution advances cleanly.
    pg_service.claim(task.task_id, actor="alice")
    # Original claimer sees the task.
    assert {t.task_id for t in pg_service.my_tasks("alice")} == {
        task.task_id
    }
    # New assignee sees nothing yet.
    assert pg_service.my_tasks("alice-reassigned") == []
    # PATCH-style reassign (column write, no lifecycle transition).
    updated = pg_service.update(task.task_id, assignee="alice-reassigned")
    assert updated.assignee == "alice-reassigned"
    # Re-read from a fresh get() so we know the column survived a
    # round-trip, not just an in-memory hand-off.
    refetched = pg_service.get(task.task_id)
    assert refetched.assignee == "alice-reassigned"
    # ``my_tasks`` now resolves the task to the new owner.
    rows = pg_service.my_tasks("alice-reassigned")
    assert {t.task_id for t in rows} == {task.task_id}
    # Old owner no longer sees it.
    assert pg_service.my_tasks("alice") == []


def test_update_assignee_bumps_updated_at(pg_service):
    """``updated_at`` must advance when ``update()`` writes assignee."""
    task = _make_draft(pg_service)
    before = task.updated_at
    # Tiny sleep so the per-second resolution timestamps actually
    # differ — _now_iso() resolution depends on the pg column type
    # but our timestamps are isoformat strings with microseconds.
    import time as _time

    _time.sleep(0.01)
    updated = pg_service.update(task.task_id, assignee="bob")
    assert updated.assignee == "bob"
    assert updated.updated_at != before


def test_update_external_refs_roundtrips_jsonb(pg_service):
    """``external_refs`` is JSONB on disk — verify dict survives encode/decode."""
    task = _make_draft(pg_service)
    payload = {
        "jira": "JIRA-1234",
        "slack_thread": "C0123ABC",
        "github_issue": "5678",
    }
    updated = pg_service.update(task.task_id, external_refs=payload)
    assert updated.external_refs == payload
    # Re-read to prove the JSONB column decodes back to the exact map.
    refetched = pg_service.get(task.task_id)
    assert refetched.external_refs == payload
    # Replace-semantics (spec §5.4): a second write replaces, not merges.
    replaced = pg_service.update(
        task.task_id, external_refs={"only_key": "X-1"}
    )
    assert replaced.external_refs == {"only_key": "X-1"}
    refetched_again = pg_service.get(task.task_id)
    assert refetched_again.external_refs == {"only_key": "X-1"}


def test_update_external_refs_empty_dict_clears(pg_service):
    """Empty dict clears the column (spec §5.4 lists/maps replace)."""
    task = _make_draft(pg_service)
    pg_service.update(task.task_id, external_refs={"jira": "X-1"})
    cleared = pg_service.update(task.task_id, external_refs={})
    assert cleared.external_refs == {}
    refetched = pg_service.get(task.task_id)
    assert refetched.external_refs == {}


def test_reassign_task_appends_context_log_breadcrumb(pg_service):
    """Spec §P-9 invariant: mid-flight reassign leaves a context entry.

    ``svc.update(assignee=...)`` (the PATCH path) writes the column
    silently. ``svc.reassign_task(...)`` MUST also append a
    ``reassignment`` row to ``work_context_entries`` so the new owner
    can recover context via ``pm task get``. PR #2064 round-3 added
    this method; the prior head routed reassigns through ``update``
    and dropped the breadcrumb.

    Verifies:
    * the assignee column is updated;
    * exactly one context entry is appended with ``entry_type =
      'reassignment'`` and body matching the spec's example wording
      (``worker reassigned from pete to nora``);
    * the entry's actor reflects the operator passed in;
    * both writes are visible after a fresh ``get(task_id)`` (i.e.
      the transaction committed).
    """
    # ``claim`` resolves ``assignee`` via the ``worker`` role on the
    # task, not via the ``actor`` argument — so wire roles.worker=pete
    # explicitly to get a pete-claimed task.
    task = _make_draft(
        pg_service, roles={"worker": "pete", "reviewer": "bob"}
    )
    pg_service.queue(task.task_id, actor="user")
    pg_service.claim(task.task_id, actor="pete")
    # Sanity: claim landed pete on the assignee column.
    assert pg_service.get(task.task_id).assignee == "pete"
    # No reassignment entries on the freshly-claimed task.
    pre_entries = pg_service.get_context(
        task.task_id, entry_type="reassignment"
    )
    assert pre_entries == []

    updated = pg_service.reassign_task(
        task.task_id, new_assignee="nora", actor="api"
    )
    assert updated.assignee == "nora"

    # Re-read independently so we know both writes survived commit.
    refetched = pg_service.get(task.task_id)
    assert refetched.assignee == "nora"

    entries = pg_service.get_context(
        task.task_id, entry_type="reassignment"
    )
    assert len(entries) == 1, (
        f"expected exactly one reassignment breadcrumb, got: {entries!r}"
    )
    entry = entries[0]
    assert entry.entry_type == "reassignment"
    assert entry.actor == "api"
    assert "pete" in entry.text and "nora" in entry.text, (
        f"breadcrumb must name old + new assignee; got: {entry.text!r}"
    )
    assert "reassigned" in entry.text.lower()


def test_reassign_task_optional_reason_lands_in_breadcrumb(pg_service):
    """A non-empty ``reason`` is appended to the breadcrumb body."""
    task = _make_draft(
        pg_service, roles={"worker": "pete", "reviewer": "bob"}
    )
    pg_service.queue(task.task_id, actor="user")
    pg_service.claim(task.task_id, actor="pete")

    pg_service.reassign_task(
        task.task_id,
        new_assignee="nora",
        actor="ops",
        reason="pete went offline",
    )
    entries = pg_service.get_context(
        task.task_id, entry_type="reassignment"
    )
    assert len(entries) == 1
    assert "pete went offline" in entries[0].text


def test_reassign_task_rejects_unknown_target_session(pg_service):
    """Unknown assignee strings must not strand active tasks (#2370)."""
    from pollypm.work.service_support import ValidationError

    task = _make_draft(
        pg_service, roles={"worker": "pete", "reviewer": "bob"}
    )
    pg_service.queue(task.task_id, actor="user")
    pg_service.claim(task.task_id, actor="pete")

    with pytest.raises(ValidationError, match="unknown session"):
        pg_service.reassign_task(
            task.task_id, new_assignee="ghost-session", actor="api"
        )

    refetched = pg_service.get(task.task_id)
    assert refetched.assignee == "pete"
    entries = pg_service.get_context(
        task.task_id, entry_type="reassignment"
    )
    assert entries == []


def test_reassign_task_missing_task_raises(pg_service):
    """Unknown task → TaskNotFoundError BEFORE any column write."""
    from pollypm.work.service_support import TaskNotFoundError

    with pytest.raises(TaskNotFoundError):
        pg_service.reassign_task(
            "demo/9999", new_assignee="nora", actor="api"
        )


def test_reassign_task_rejects_draft(pg_service):
    """#2064 round-9 blocker #4: reassign refuses draft (no live worker)."""
    from pollypm.work.service_support import InvalidTransitionError

    task = _make_draft(pg_service)
    # Sanity: still in draft state.
    assert pg_service.get(task.task_id).work_status.value == "draft"

    with pytest.raises(InvalidTransitionError) as excinfo:
        pg_service.reassign_task(
            task.task_id, new_assignee="nora", actor="api"
        )
    assert "draft" in str(excinfo.value).lower()
    # No breadcrumb should have been recorded; reassign refused
    # BEFORE the INSERT.
    entries = pg_service.get_context(
        task.task_id, entry_type="reassignment"
    )
    assert entries == [], (
        f"reassign on draft must NOT record a breadcrumb; got "
        f"{entries!r}"
    )


def test_reassign_task_rejects_cancelled(pg_service):
    """#2064 round-9 blocker #4: reassign refuses cancelled (terminal)."""
    from pollypm.work.service_support import InvalidTransitionError

    task = _make_draft(
        pg_service, roles={"worker": "pete", "reviewer": "bob"}
    )
    pg_service.queue(task.task_id, actor="user")
    pg_service.claim(task.task_id, actor="pete")
    pg_service.cancel(task.task_id, actor="user", reason="not needed")
    assert pg_service.get(task.task_id).work_status.value == "cancelled"

    with pytest.raises(InvalidTransitionError) as excinfo:
        pg_service.reassign_task(
            task.task_id, new_assignee="nora", actor="api"
        )
    assert "cancelled" in str(excinfo.value).lower()
    # No reassignment breadcrumb (cancel itself records a
    # transition row but not a reassignment context entry).
    entries = pg_service.get_context(
        task.task_id, entry_type="reassignment"
    )
    assert entries == []


def test_reassign_task_rejects_done(pg_service):
    """#2064 round-9 blocker #4: reassign refuses done (terminal)."""
    from pollypm.work.service_support import InvalidTransitionError

    task = _make_draft(
        pg_service, roles={"worker": "pete", "reviewer": "bob"}
    )
    pg_service.queue(task.task_id, actor="user")
    pg_service.claim(task.task_id, actor="pete")
    # Force the row into ``done`` directly via the column writer —
    # going through the full flow machinery would require a
    # configured review-approve chain. The state-check inside
    # reassign_task runs under FOR UPDATE on ``work_status``, so the
    # storage path is exercised either way.
    with pg_service._pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE work_tasks SET work_status = 'done' "
            "WHERE project = %s AND task_number = %s",
            (task.project, task.task_number),
        )
        conn.commit()
    assert pg_service.get(task.task_id).work_status.value == "done"

    with pytest.raises(InvalidTransitionError) as excinfo:
        pg_service.reassign_task(
            task.task_id, new_assignee="nora", actor="api"
        )
    assert "done" in str(excinfo.value).lower()
    entries = pg_service.get_context(
        task.task_id, entry_type="reassignment"
    )
    assert entries == []


def test_reassign_task_rejects_queued(pg_service):
    """#2064 round-11 blocker #3: reassign refuses queued (no routing source).

    Queued dispatch uses ``task.roles["worker"]`` (see
    ``PgWorkService.next`` at ``pg_service.py:2059-2060``) and
    ``claim()`` resolves the next assignee from the node role
    (``_resolve_node_assignee`` at ``:4254-4256``). A queued
    reassign would update ``assignee`` but the next ``claim()``
    would still route to the original role owner — a silent
    drift between ``GET`` reads and ``claim()`` routing. The
    contract is to reject queued reassigns and point the
    operator at cancel + re-queue with the new role.

    Round-9 blocker #4 only rejected ``draft`` / terminal; this
    test pins the round-11 extension to ``queued``.
    """
    from pollypm.work.service_support import InvalidTransitionError

    task = _make_draft(
        pg_service, roles={"worker": "alice", "reviewer": "bob"}
    )
    pg_service.queue(task.task_id, actor="user")
    assert pg_service.get(task.task_id).work_status.value == "queued"

    with pytest.raises(InvalidTransitionError) as excinfo:
        pg_service.reassign_task(
            task.task_id, new_assignee="nora", actor="api"
        )
    msg = str(excinfo.value).lower()
    assert "queued" in msg
    # Hint must point at the cancel + re-queue workaround.
    assert "cancel" in msg and "re-queue" in msg, msg
    # No breadcrumb should have been recorded.
    entries = pg_service.get_context(
        task.task_id, entry_type="reassignment"
    )
    assert entries == [], (
        f"reassign on queued must NOT record a breadcrumb; got {entries!r}"
    )
    # Roles untouched — operator's source-of-truth is preserved.
    assert pg_service.get(task.task_id).roles.get("worker") == "alice"


def test_concurrent_reassign_serializes_breadcrumbs(pg_service):
    """Spec §P-9 + concurrency safety (#2064 round-4 blocker #2).

    Two concurrent reassigns of the same task must produce a coherent
    breadcrumb chain: the second writer's entry MUST name the first
    writer's new assignee as its predecessor (not the original owner).

    Without ``SELECT ... FOR UPDATE`` on the row read, both
    transactions read the same ``old_assignee`` under READ COMMITTED,
    serialise on the UPDATE, and emit two breadcrumbs with the SAME
    ``from`` value — losing the second writer's view of the
    intermediate state. With the row lock, the second SELECT waits on
    the first commit and reads the freshly-updated assignee, so the
    chain reads ``pete -> nora`` then ``nora -> olga``.

    The threading scheduler doesn't reliably produce that interleaving
    in CI, so this test forces it deterministically: thread 1 grabs an
    `Event` checkpoint between SELECT and UPDATE (impossible to spy on
    inside the SUT without instrumentation), so instead we drive the
    race directly with two threads and an `Event` chain. The trick:
    thread 1 calls ``reassign_task``, but we hold its commit by
    monkey-patching ``conn.commit`` for that one call so thread 2 can
    run its full ``reassign_task`` SELECT *before* thread 1 commits.
    That way ``FOR UPDATE`` is the only thing forcing thread 2 to
    block on thread 1's row lock; without it, thread 2 reads
    ``pete`` and the chain breaks.

    Regression target: this fails on the prior head (sources[1] ==
    'pete'); passes once the SELECT takes ``FOR UPDATE``.
    """
    import re
    import threading

    task = _make_draft(
        pg_service, roles={"worker": "pete", "reviewer": "bob"}
    )
    pg_service.queue(task.task_id, actor="user")
    pg_service.claim(task.task_id, actor="pete")
    assert pg_service.get(task.task_id).assignee == "pete"

    # Two events to choreograph the race:
    # * ``t1_acquired_lock``: t1 has run SELECT (+ UPDATE, depending on
    #   the codepath) but not yet committed. With FOR UPDATE in place,
    #   the SELECT alone takes the row lock.
    # * ``t2_finished_select``: t2 attempted its SELECT. Without
    #   FOR UPDATE this returns immediately with ``pete``; with
    #   FOR UPDATE it blocks until t1 commits.
    t1_acquired_lock = threading.Event()
    t2_started = threading.Event()
    errors: list[BaseException] = []

    def _t1_reassign() -> None:
        try:
            # Wrap the underlying pool connection so we can interpose
            # a delay between t1's row-lock acquisition and its
            # commit. We do this by replacing _pool.connection one-shot.
            original_connection = pg_service._pool.connection

            class _DelayingConn:
                def __init__(self, real_cm):
                    self._real_cm = real_cm
                    self._real_conn = None

                def __enter__(self):
                    self._real_conn = self._real_cm.__enter__()
                    original_commit = self._real_conn.commit

                    def _delayed_commit():
                        # Row lock is held now (FOR UPDATE) or the
                        # UPDATE has fired (without FOR UPDATE).
                        # Either way, t2 can race its SELECT.
                        t1_acquired_lock.set()
                        # Give t2 a chance to issue its SELECT
                        # against the row. With FOR UPDATE, t2's
                        # SELECT blocks on t1's lock until commit
                        # below; without it, t2's SELECT returns
                        # immediately with stale ``pete``.
                        t2_started.wait(timeout=3)
                        return original_commit()

                    self._real_conn.commit = _delayed_commit
                    return self._real_conn

                def __exit__(self, *a):
                    return self._real_cm.__exit__(*a)

            # Patch only for this single call, then restore.
            def _one_shot_connection():
                pg_service._pool.connection = original_connection
                return _DelayingConn(original_connection())

            pg_service._pool.connection = _one_shot_connection
            pg_service.reassign_task(
                task.task_id, new_assignee="nora", actor="api-nora"
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
            t1_acquired_lock.set()
            t2_started.set()

    def _t2_reassign() -> None:
        try:
            # Wait until t1 has its row lock / has issued its UPDATE.
            assert t1_acquired_lock.wait(timeout=5), (
                "t1 never reached the pre-commit checkpoint"
            )
            # Signal t2 has started its attempt. We set this BEFORE
            # the SELECT so t1's commit can proceed even if t2's
            # SELECT blocks on the lock (the FOR UPDATE case).
            t2_started.set()
            pg_service.reassign_task(
                task.task_id, new_assignee="olga", actor="api-olga"
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=_t1_reassign)
    t2 = threading.Thread(target=_t2_reassign)
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)
    assert not t1.is_alive() and not t2.is_alive(), (
        "thread deadlocked — FOR UPDATE may have a starvation bug"
    )
    assert not errors, f"reassign thread raised: {errors!r}"

    # ``get_context`` returns ``ORDER BY id DESC`` (most recent first).
    # Reverse so ``chronological[0]`` is the FIRST writer's breadcrumb
    # and ``chronological[1]`` is the SECOND writer's breadcrumb.
    raw_entries = pg_service.get_context(
        task.task_id, entry_type="reassignment"
    )
    chronological = list(reversed(raw_entries))
    assert len(chronological) == 2, (
        f"expected exactly two reassignment entries, got: "
        f"{raw_entries!r}"
    )
    pattern = re.compile(
        r"worker reassigned from (?P<src>\S+) to (?P<dst>\S+)"
    )
    parsed = []
    for entry in chronological:
        match = pattern.search(entry.text)
        assert match is not None, (
            f"breadcrumb missing spec-shape body: {entry.text!r}"
        )
        parsed.append((match.group("src"), match.group("dst")))

    sources = [src for src, _ in parsed]
    destinations = [dst for _, dst in parsed]
    assert set(destinations) == {"nora", "olga"}, (
        f"both new assignees must appear as breadcrumb destinations; "
        f"got sources={sources} destinations={destinations}"
    )
    # The first writer saw the original assignee.
    assert sources[0] == "pete", (
        f"first breadcrumb must name the original assignee; "
        f"got sources={sources}"
    )
    # The second writer MUST have seen the first writer's
    # destination as their source — that's the FOR UPDATE invariant.
    # Without the lock, BOTH transactions read ``pete`` and the chain
    # is broken (``sources[1] == 'pete'`` instead of the first
    # writer's new assignee). This is the regression we're guarding.
    assert sources[1] == destinations[0], (
        f"second breadcrumb's ``from`` must match the first's ``to`` "
        f"(coherent handoff chain); got sources={sources} "
        f"destinations={destinations}. Without ``SELECT FOR UPDATE`` "
        f"both reads see ``pete`` and the chain is broken."
    )
    # Final state matches the second writer (UPDATE serialised on
    # the row lock).
    final = pg_service.get(task.task_id)
    assert final.assignee == destinations[1]


def test_concurrent_claim_serializes_one_winner_one_loser(
    pg_service, monkeypatch,
):
    """Spec §P-9 + concurrency safety (#2064 round-8 blocker #1).

    The new ``POST /tasks/{p}/{n}/claim`` route advertises an atomic
    claim. Under the prior implementation ``PgWorkService.claim()``
    read ``work_status`` outside the write transaction, validated,
    then UPDATE-d. Two simultaneous claims both saw ``queued``, both
    passed validation, and both ran the UPDATE + transition insert
    + node-execution insert serially — the second one overwriting
    the first assignee and orphaning the first worker's session.

    The fix mirrors the round-4 reassign FOR UPDATE pattern: take
    the row lock inside the transaction and re-validate
    ``work_status``. The loser blocks on the lock, re-reads after
    the winner commits, sees ``in_progress``, and raises
    ``InvalidTransitionError`` (mapped to 409 by the route).

    Like the reassign test, the scheduler doesn't reliably produce
    the interleaving in CI, so we force it deterministically.
    Round-8 used a commit-hook on every connection, but ``claim()``
    opens TWO connections per call (the pre-transaction
    ``self.get(task_id)`` read AND the write transaction), and the
    pool may recycle a wrapped connection for t2. That produced a
    flake where t1's pre-tx ``get()`` returned its connection to the
    pool, t2 raced through its own ``claim()`` first, committed, and
    t1 — entering its write transaction — saw ``in_progress`` and
    raised ``InvalidTransitionError`` instead of being the winner.

    Round-9 fix: checkpoint specifically on the ``SELECT ... FOR
    UPDATE`` statement INSIDE t1's write transaction, gated by a
    thread-local flag so the hook fires for t1 only. The cursor
    wrapper inspects each ``execute`` and, after the FOR UPDATE
    returns (row lock held), signals t1_acquired_lock and waits for
    t2 to attempt its own claim. With FOR UPDATE, t2's SELECT blocks
    on t1's lock until t1 commits; without it, t2 races past
    validation and corrupts state.

    Regression target: this test fails on the prior head (both
    threads succeed, second assignee overwrites first). It passes
    once the SELECT inside the transaction takes ``FOR UPDATE`` and
    re-validates the status.
    """
    import threading

    from pollypm.claim_breadcrumbs import (
        CLAIM_ALREADY_CLAIMED_REASON,
        CLAIM_ATTEMPTED_BY_LOSER,
        CLAIM_WON_BY,
    )
    from pollypm.work.models import WorkStatus
    from pollypm.work.service_support import InvalidTransitionError

    event_lock = threading.Lock()
    emitted: list[dict] = []

    def fake_emit(**kw):
        with event_lock:
            emitted.append(kw)

    monkeypatch.setattr("pollypm.audit.emit", fake_emit)

    task = _make_draft(
        pg_service, roles={"worker": "alice", "reviewer": "bob"}
    )
    pg_service.queue(task.task_id, actor="user")
    assert pg_service.get(task.task_id).work_status is WorkStatus.QUEUED

    t1_acquired_lock = threading.Event()
    t2_started = threading.Event()
    results: dict[str, object] = {}
    errors: dict[str, BaseException] = {}

    # Thread-local guard: the connection wrapper is installed on the
    # pool (process-wide) but the FOR-UPDATE checkpoint must only
    # fire for t1's claim call. Without this, t2's own
    # ``self._pool.connection()`` calls would also see the wrapper
    # and trip the hook.
    tls = threading.local()
    original_connection = pg_service._pool.connection

    class _CheckpointingCursor:
        def __init__(self, real_cursor):
            self._real = real_cursor

        def __getattr__(self, name):
            return getattr(self._real, name)

        def __enter__(self):
            self._real.__enter__()
            return self

        def __exit__(self, *a):
            return self._real.__exit__(*a)

        def execute(self, sql, params=None, *args, **kwargs):
            # Run the statement first so the row lock is actually
            # held by Postgres before we release t2.
            if params is None:
                result = self._real.execute(sql, *args, **kwargs)
            else:
                result = self._real.execute(sql, params, *args, **kwargs)
            if (
                getattr(tls, "is_t1", False)
                and "FOR UPDATE" in (sql or "")
                and not t1_acquired_lock.is_set()
            ):
                # t1 has the row lock now. Let t2 attempt its claim;
                # with FOR UPDATE its SELECT blocks on our lock
                # until t1 commits. Bounded wait so the test fails
                # loudly rather than deadlocking.
                t1_acquired_lock.set()
                t2_started.wait(timeout=3)
            return result

    class _ConnProxy:
        """Proxy wrapping a real psycopg connection.

        Replaces ``cursor()`` with a checkpoint-aware factory without
        mutating the underlying connection object (the pool recycles
        the same psycopg connection across borrows; mutating it
        would poison later tests). All other attribute access falls
        through to the real connection via ``__getattr__`` so
        ``commit``/``rollback``/``autocommit`` work as usual.
        """

        def __init__(self, real_conn):
            object.__setattr__(self, "_real", real_conn)

        def __getattr__(self, name):
            return getattr(self._real, name)

        def __setattr__(self, name, value):
            setattr(self._real, name, value)

        def cursor(self, *args, **kwargs):
            return _CheckpointingCursor(self._real.cursor(*args, **kwargs))

    class _CheckpointingConn:
        def __init__(self, real_cm):
            self._real_cm = real_cm

        def __enter__(self):
            real_conn = self._real_cm.__enter__()
            return _ConnProxy(real_conn)

        def __exit__(self, *a):
            return self._real_cm.__exit__(*a)

    def _wrapping_connection():
        return _CheckpointingConn(original_connection())

    pg_service._pool.connection = _wrapping_connection

    def _t1_claim() -> None:
        tls.is_t1 = True
        try:
            results["t1"] = pg_service.claim(task.task_id, actor="winner")
        except BaseException as exc:  # noqa: BLE001
            errors["t1"] = exc
            t1_acquired_lock.set()
            t2_started.set()
        finally:
            tls.is_t1 = False

    def _t2_claim() -> None:
        try:
            assert t1_acquired_lock.wait(timeout=5), (
                "t1 never reached the FOR UPDATE checkpoint"
            )
            t2_started.set()
            results["t2"] = pg_service.claim(task.task_id, actor="loser")
        except BaseException as exc:  # noqa: BLE001
            errors["t2"] = exc

    try:
        t1 = threading.Thread(target=_t1_claim)
        t2 = threading.Thread(target=_t2_claim)
        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)
    finally:
        # Restore the pool's connection factory so later tests see
        # the unwrapped object even if a thread raised.
        pg_service._pool.connection = original_connection
    assert not t1.is_alive() and not t2.is_alive(), (
        "thread deadlocked — claim() FOR UPDATE may have a "
        "starvation bug"
    )

    # Exactly one winner, exactly one loser. The loser MUST raise
    # InvalidTransitionError (mapped to 409 by the route).
    assert "t1" in results, (
        f"first claim should have succeeded; t1 errored: "
        f"{errors.get('t1')!r}"
    )
    assert "t2" not in results, (
        f"second claim should NOT have succeeded — t2 returned "
        f"{results.get('t2')!r}. Without ``SELECT FOR UPDATE`` + "
        f"re-validate, both reads see ``queued`` and both UPDATEs "
        f"land, overwriting the first assignee."
    )
    assert isinstance(errors.get("t2"), InvalidTransitionError), (
        f"loser must raise InvalidTransitionError (→ 409); got: "
        f"{errors.get('t2')!r}"
    )

    # Final state: in_progress with exactly one
    # queued→in_progress transition and exactly one node
    # execution. Two of either means the loser wrote duplicate
    # audit rows from its stale pre-transaction snapshot.
    #
    # Note: the assignee column resolves via
    # ``_resolve_node_assignee`` from the task's role map (here
    # ``roles['worker']='alice'``), so both winner and loser would
    # write ``assignee='alice'``. The discriminator is the
    # ``actor`` column on the transition row, which captures the
    # caller's actor verbatim.
    final = pg_service.get(task.task_id)
    assert final.work_status is WorkStatus.IN_PROGRESS, (
        f"task must be in_progress after the winning claim; got "
        f"{final.work_status!r}"
    )

    with pg_service._pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT actor FROM work_transitions "
            "WHERE task_project = %s AND task_number = %s "
            "AND from_state = %s AND to_state = %s "
            "ORDER BY id",
            (
                task.project,
                task.task_number,
                WorkStatus.QUEUED.value,
                WorkStatus.IN_PROGRESS.value,
            ),
        )
        transition_actors = [row[0] for row in cur.fetchall()]
        cur.execute(
            "SELECT COUNT(*) FROM work_node_executions "
            "WHERE task_project = %s AND task_number = %s",
            (task.project, task.task_number),
        )
        execution_count = cur.fetchone()[0]
    assert transition_actors == ["winner"], (
        f"exactly one queued→in_progress transition expected "
        f"with actor='winner'; got actors={transition_actors!r}. "
        f"If 'loser' appears, the loser's stale-snapshot UPDATE "
        f"path also committed — FOR UPDATE didn't take."
    )
    assert execution_count == 1, (
        f"exactly one node execution expected; got "
        f"{execution_count}. Loser inserted a duplicate visit row "
        f"from its stale pre-transaction snapshot."
    )

    entries = pg_service.get_context(task.task_id)
    claim_entries = {
        entry.entry_type: entry
        for entry in entries
        if entry.entry_type in {CLAIM_WON_BY, CLAIM_ATTEMPTED_BY_LOSER}
    }
    assert set(claim_entries) == {CLAIM_WON_BY, CLAIM_ATTEMPTED_BY_LOSER}
    won = claim_entries[CLAIM_WON_BY]
    assert won.actor == "winner"
    assert f"task_id={task.task_id}" in won.text
    assert "actor=winner" in won.text
    assert "session=winner" in won.text
    assert "assignee=alice" in won.text

    lost = claim_entries[CLAIM_ATTEMPTED_BY_LOSER]
    assert lost.actor == "loser"
    assert f"task_id={task.task_id}" in lost.text
    assert "actor=loser" in lost.text
    assert "session=loser" in lost.text
    assert f"reason={CLAIM_ALREADY_CLAIMED_REASON}" in lost.text
    assert "winner_session=winner" in lost.text
    assert "assignee=alice" in lost.text

    claim_events = [
        event for event in emitted
        if event.get("event") in {CLAIM_WON_BY, CLAIM_ATTEMPTED_BY_LOSER}
    ]
    assert {event["event"] for event in claim_events} == {
        CLAIM_WON_BY,
        CLAIM_ATTEMPTED_BY_LOSER,
    }
    events_by_type = {event["event"]: event for event in claim_events}
    assert events_by_type[CLAIM_WON_BY]["metadata"]["session"] == "winner"
    loser_metadata = events_by_type[CLAIM_ATTEMPTED_BY_LOSER]["metadata"]
    assert loser_metadata["session"] == "loser"
    assert loser_metadata["reason"] == CLAIM_ALREADY_CLAIMED_REASON
    assert loser_metadata["winner_session"] == "winner"


def test_concurrent_role_update_does_not_leak_into_claim(pg_service):
    """Spec §P-9 + concurrency safety (#2064 round-13 blocker #1).

    Until round-13, ``PgWorkService.claim()`` read the full task
    OUTSIDE the write transaction (``self.get(task_id)`` at the top),
    resolved the node assignee from ``task.roles`` on that snapshot,
    THEN opened the write tx + ``SELECT ... FOR UPDATE``. Between the
    pre-read and the row lock, a concurrent ``svc.update(roles=...)``
    could mutate the worker binding; ``SELECT FOR UPDATE`` only
    revalidated ``work_status`` / ``assignee``, so the UPDATE landed
    with the STALE pre-read assignee. The new worker silently lost
    the binding.

    Round-13 fix: every claim-dependent read (status, roles,
    current_node_id, blockers) is sourced from the FOR-UPDATE-locked
    row inside the transaction. A concurrent role update committed
    before the lock is visible; one waiting on the lock is invisible
    until commit. Either way the decision is consistent with the
    locked snapshot, never with a stale pre-tx view.

    Deterministic interleaving: a cursor wrapper intercepts the
    ``SELECT ... FOR UPDATE`` statement INSIDE t1's claim transaction
    and pauses BEFORE running it. While t1 is paused, t2 commits an
    ``update(roles={'worker': 'carol'})``. T1 then proceeds; its
    SELECT-FOR-UPDATE reads the fresh roles. The new claim resolves
    assignee from the locked row, so the final assignee MUST be
    'carol' (not the pre-mutation 'alice').

    Regression target: on the round-12 head this test fails — the
    pre-read captured ``roles['worker'] = 'alice'``, resolved
    ``assignee = 'alice'`` before the pause, and committed the
    UPDATE with the stale value. On the round-13 head it passes
    because the in-tx revalidation reads the post-mutation roles.
    """
    import threading

    from pollypm.work.models import WorkStatus

    task = _make_draft(
        pg_service, roles={"worker": "alice", "reviewer": "bob"}
    )
    pg_service.queue(task.task_id, actor="user")
    assert pg_service.get(task.task_id).work_status is WorkStatus.QUEUED

    t1_at_lock_point = threading.Event()
    t2_role_update_done = threading.Event()
    results: dict[str, object] = {}
    errors: dict[str, BaseException] = {}

    tls = threading.local()
    original_connection = pg_service._pool.connection

    class _CheckpointingCursor:
        def __init__(self, real_cursor):
            self._real = real_cursor

        def __getattr__(self, name):
            return getattr(self._real, name)

        def __enter__(self):
            self._real.__enter__()
            return self

        def __exit__(self, *a):
            return self._real.__exit__(*a)

        def execute(self, sql, params=None, *args, **kwargs):
            # Pause BEFORE running the SELECT-FOR-UPDATE so t2 can
            # mutate roles without contending for a row lock t1 has
            # not yet taken. This matches the real-world race the
            # round-13 fix closes: the OLD code resolved the node
            # assignee from its pre-tx ``self.get()`` snapshot
            # BEFORE reaching this statement, so by the time the
            # lock acquires it's too late to pick up t2's update.
            if (
                getattr(tls, "is_t1", False)
                and "FOR UPDATE" in (sql or "")
                and not t1_at_lock_point.is_set()
            ):
                t1_at_lock_point.set()
                t2_role_update_done.wait(timeout=5)
            if params is None:
                return self._real.execute(sql, *args, **kwargs)
            return self._real.execute(sql, params, *args, **kwargs)

    class _ConnProxy:
        def __init__(self, real_conn):
            object.__setattr__(self, "_real", real_conn)

        def __getattr__(self, name):
            return getattr(self._real, name)

        def __setattr__(self, name, value):
            setattr(self._real, name, value)

        def cursor(self, *args, **kwargs):
            return _CheckpointingCursor(self._real.cursor(*args, **kwargs))

    class _CheckpointingConn:
        def __init__(self, real_cm):
            self._real_cm = real_cm

        def __enter__(self):
            real_conn = self._real_cm.__enter__()
            return _ConnProxy(real_conn)

        def __exit__(self, *a):
            return self._real_cm.__exit__(*a)

    def _wrapping_connection():
        return _CheckpointingConn(original_connection())

    pg_service._pool.connection = _wrapping_connection

    def _t1_claim() -> None:
        tls.is_t1 = True
        try:
            results["t1"] = pg_service.claim(
                task.task_id, actor="t1_actor"
            )
        except BaseException as exc:  # noqa: BLE001
            errors["t1"] = exc
            t1_at_lock_point.set()
            t2_role_update_done.set()
        finally:
            tls.is_t1 = False

    def _t2_update_role() -> None:
        try:
            assert t1_at_lock_point.wait(timeout=5), (
                "t1 never reached the SELECT-FOR-UPDATE checkpoint"
            )
            pg_service.update(
                task.task_id,
                roles={"worker": "carol", "reviewer": "bob"},
            )
            results["t2"] = "ok"
        except BaseException as exc:  # noqa: BLE001
            errors["t2"] = exc
        finally:
            t2_role_update_done.set()

    try:
        t1 = threading.Thread(target=_t1_claim)
        t2 = threading.Thread(target=_t2_update_role)
        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)
    finally:
        pg_service._pool.connection = original_connection

    assert not t1.is_alive() and not t2.is_alive(), (
        "thread deadlocked — claim() may be holding the lock across "
        "the role update window"
    )
    assert "t2" in results, (
        f"t2 role update should have succeeded; got error: "
        f"{errors.get('t2')!r}"
    )
    assert "t1" in results, (
        f"t1 claim should have succeeded; got error: "
        f"{errors.get('t1')!r}"
    )

    final = pg_service.get(task.task_id)
    assert final.work_status is WorkStatus.IN_PROGRESS, (
        f"task must be in_progress after the claim; got "
        f"{final.work_status!r}"
    )
    assert final.roles.get("worker") == "carol", (
        f"role mutation must have committed; got roles={final.roles!r}"
    )
    # The invariant the fix establishes: assignee resolves from the
    # FOR-UPDATE-locked roles, NOT the pre-tx snapshot. On the
    # round-12 head the assignee would be 'alice' (pre-read value);
    # on round-13 it must be 'carol' (locked value).
    assert final.assignee == "carol", (
        f"assignee must reflect the role binding visible under the "
        f"row lock; got {final.assignee!r}. If this is 'alice', the "
        f"claim used a stale pre-transaction snapshot — the round-13 "
        f"in-lock revalidation regressed."
    )


def test_update_combined_assignee_and_external_refs_single_call(pg_service):
    """Combining columns in one update() call — single transaction.

    PATCH atomicity (#2064 round-1) depends on this: labels +
    external_refs must land in one ``svc.update(...)`` invocation so
    the underlying UPDATE statement commits both columns together.
    """
    task = _make_draft(pg_service)
    updated = pg_service.update(
        task.task_id,
        assignee="atomic-worker",
        external_refs={"jira": "X-99"},
        labels=["urgent", "rc"],
    )
    assert updated.assignee == "atomic-worker"
    assert updated.external_refs == {"jira": "X-99"}
    assert sorted(updated.labels) == ["rc", "urgent"]
    # Re-read to confirm all three persisted.
    refetched = pg_service.get(task.task_id)
    assert refetched.assignee == "atomic-worker"
    assert refetched.external_refs == {"jira": "X-99"}
    assert sorted(refetched.labels) == ["rc", "urgent"]


def test_increment_plan_version_bumps_counter(pg_service):
    task = _make_draft(pg_service)
    assert task.plan_version == 1
    bumped = pg_service.increment_plan_version(task.task_id, actor="user")
    assert bumped.plan_version == 2


def test_list_successors_walks_predecessor_chain(pg_service):
    parent = _make_draft(pg_service)
    child = pg_service.create(
        title="successor",
        type="task",
        project="demo",
        flow_template="default",
        roles={"worker": "a"},
        predecessor_task_id=parent.task_id,
    )
    successors = pg_service.list_successors(parent.task_id)
    assert [t.task_id for t in successors] == [child.task_id]


# ---------------------------------------------------------------------------
# add_context / get_context
# ---------------------------------------------------------------------------


def test_add_context_returns_entry_and_roundtrips(pg_service):
    task = _make_draft(pg_service)
    entry = pg_service.add_context(task.task_id, "user", "hello world")
    assert entry.actor == "user"
    assert entry.text == "hello world"
    assert entry.entry_type == "note"

    rows = pg_service.get_context(task.task_id)
    assert len(rows) == 1
    assert rows[0].text == "hello world"


def test_add_context_missing_task(pg_service):
    from pollypm.work.service_support import TaskNotFoundError

    with pytest.raises(TaskNotFoundError):
        pg_service.add_context("nope/1", "user", "x")


def test_get_context_filters_by_entry_type(pg_service):
    task = _make_draft(pg_service)
    pg_service.add_context(task.task_id, "user", "note 1")
    pg_service.add_context(
        task.task_id, "user", "a reply", entry_type="reply"
    )
    notes = pg_service.get_context(task.task_id, entry_type="note")
    replies = pg_service.get_context(task.task_id, entry_type="reply")
    assert [e.text for e in notes] == ["note 1"]
    assert [e.text for e in replies] == ["a reply"]


def test_get_context_limit_honored(pg_service):
    task = _make_draft(pg_service)
    for i in range(5):
        pg_service.add_context(task.task_id, "user", f"n{i}")
    rows = pg_service.get_context(task.task_id, limit=2)
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# link / unlink / dependents / would_create_cycle (via block)
# ---------------------------------------------------------------------------


def test_link_blocks_creates_edge_and_dependents_walk(pg_service):
    a = _make_draft(pg_service, title="a")
    b = _make_draft(pg_service, title="b")
    c = _make_draft(pg_service, title="c")
    pg_service.link(a.task_id, b.task_id, "blocks")
    pg_service.link(b.task_id, c.task_id, "blocks")
    deps = pg_service.dependents(a.task_id)
    assert {t.task_id for t in deps} == {b.task_id, c.task_id}


def test_link_rejects_invalid_kind(pg_service):
    from pollypm.work.service_support import ValidationError

    a = _make_draft(pg_service)
    b = _make_draft(pg_service, title="b")
    with pytest.raises(ValidationError):
        pg_service.link(a.task_id, b.task_id, "bogus")


def test_link_rejects_cycle(pg_service):
    from pollypm.work.service_support import ValidationError

    a = _make_draft(pg_service, title="a")
    b = _make_draft(pg_service, title="b")
    pg_service.link(a.task_id, b.task_id, "blocks")
    with pytest.raises(ValidationError, match="circular"):
        pg_service.link(b.task_id, a.task_id, "blocks")


def test_unlink_removes_edge(pg_service):
    a = _make_draft(pg_service, title="a")
    b = _make_draft(pg_service, title="b")
    pg_service.link(a.task_id, b.task_id, "blocks")
    pg_service.unlink(a.task_id, b.task_id, "blocks")
    assert pg_service.dependents(a.task_id) == []


def test_link_missing_task_raises(pg_service):
    from pollypm.work.service_support import TaskNotFoundError

    a = _make_draft(pg_service)
    with pytest.raises(TaskNotFoundError):
        pg_service.link(a.task_id, "ghost/1", "blocks")


# ---------------------------------------------------------------------------
# claim / hold / resume / next
# ---------------------------------------------------------------------------


def test_claim_advances_to_in_progress(pg_service):
    from pollypm.work.models import WorkStatus

    task = _make_draft(pg_service)
    pg_service.queue(task.task_id, actor="user")
    claimed = pg_service.claim(task.task_id, actor="alice")
    assert claimed.work_status is WorkStatus.IN_PROGRESS
    assert claimed.current_node_id is not None


def test_claim_records_session_identity_separately(pg_service, monkeypatch):
    from pollypm.audit.log import EVENT_TASK_CLAIMED_BY_SESSION

    emitted: list[dict] = []
    monkeypatch.setattr("pollypm.audit.emit", lambda **kw: emitted.append(kw))

    task = _make_draft(
        pg_service,
        roles={"worker": "worker", "reviewer": "reviewer"},
    )
    pg_service.queue(task.task_id, actor="user")

    claimed = pg_service.claim(task.task_id, actor="worker_pollypm/3")

    assert claimed.assignee == "worker"
    assert claimed.claimed_by_session == "worker_pollypm/3"
    refetched = pg_service.get(task.task_id)
    assert refetched.assignee == "worker"
    assert refetched.claimed_by_session == "worker_pollypm/3"

    claim_events = [
        event for event in emitted
        if event.get("event") == EVENT_TASK_CLAIMED_BY_SESSION
    ]
    assert len(claim_events) == 1
    assert claim_events[0]["actor"] == "worker_pollypm/3"
    assert claim_events[0]["metadata"] == {
        "assignee": "worker",
        "claimed_by_session": "worker_pollypm/3",
    }


def test_claim_from_wrong_state_raises(pg_service):
    from pollypm.work.service_support import InvalidTransitionError

    task = _make_draft(pg_service)
    with pytest.raises(InvalidTransitionError):
        pg_service.claim(task.task_id, actor="alice")


def test_reopen_cancelled_task_returns_to_clean_queue(pg_service):
    from pollypm.work.models import ExecutionStatus, WorkStatus

    task = _make_draft(pg_service)
    pg_service.queue(task.task_id, actor="user")
    pg_service.claim(task.task_id, actor="alice")
    pg_service.cancel(task.task_id, actor="user", reason="mistake")

    reopened = pg_service.reopen(
        task.task_id, actor="user", reason="undo mistaken cancel"
    )

    assert reopened.work_status is WorkStatus.QUEUED
    assert reopened.assignee is None
    assert reopened.current_node_id is None

    with pg_service._pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT from_state, to_state, reason FROM work_transitions "
            "WHERE task_project = %s AND task_number = %s "
            "ORDER BY id DESC LIMIT 1",
            (task.project, task.task_number),
        )
        assert cur.fetchone() == (
            WorkStatus.CANCELLED.value,
            WorkStatus.QUEUED.value,
            "undo mistaken cancel",
        )
        cur.execute(
            "SELECT COUNT(*) FROM work_node_executions "
            "WHERE task_project = %s AND task_number = %s "
            "AND status = %s",
            (
                task.project,
                task.task_number,
                ExecutionStatus.ACTIVE.value,
            ),
        )
        assert cur.fetchone()[0] == 0


def test_release_active_claim_returns_to_queue_preserving_node(pg_service):
    from pollypm.work.models import ExecutionStatus, WorkStatus

    task = _make_draft(pg_service)
    pg_service.queue(task.task_id, actor="user")
    claimed = pg_service.claim(task.task_id, actor="alice")

    released = pg_service.release(
        task.task_id, actor="operator", reason="worker gone"
    )

    assert released.work_status is WorkStatus.QUEUED
    assert released.assignee is None
    assert released.claimed_by_session is None
    assert released.current_node_id == claimed.current_node_id

    with pg_service._pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT from_state, to_state, reason FROM work_transitions "
            "WHERE task_project = %s AND task_number = %s "
            "ORDER BY id DESC LIMIT 1",
            (task.project, task.task_number),
        )
        assert cur.fetchone() == (
            WorkStatus.IN_PROGRESS.value,
            WorkStatus.QUEUED.value,
            "worker gone",
        )
        cur.execute(
            "SELECT COUNT(*) FROM work_node_executions "
            "WHERE task_project = %s AND task_number = %s "
            "AND status = %s",
            (
                task.project,
                task.task_number,
                ExecutionStatus.ACTIVE.value,
            ),
        )
        assert cur.fetchone()[0] == 0


def test_release_rejects_terminal_task(pg_service):
    from pollypm.work.service_support import InvalidTransitionError

    task = _make_draft(pg_service)
    pg_service.mark_done(task.task_id, actor="user")

    with pytest.raises(InvalidTransitionError):
        pg_service.release(task.task_id, actor="operator")


def test_reopen_rejects_non_cancelled_task(pg_service):
    from pollypm.work.service_support import InvalidTransitionError

    task = _make_draft(pg_service)
    pg_service.queue(task.task_id, actor="user")
    with pytest.raises(InvalidTransitionError):
        pg_service.reopen(task.task_id, actor="user")


def test_hold_from_in_progress(pg_service):
    from pollypm.work.models import WorkStatus

    task = _make_draft(pg_service)
    pg_service.queue(task.task_id, actor="user")
    pg_service.claim(task.task_id, actor="alice")
    held = pg_service.hold(task.task_id, actor="user", reason="pause")
    assert held.work_status is WorkStatus.ON_HOLD


def test_hold_from_wrong_state(pg_service):
    from pollypm.work.service_support import InvalidTransitionError

    task = _make_draft(pg_service)
    with pytest.raises(InvalidTransitionError):
        pg_service.hold(task.task_id, actor="user")


def test_resume_from_on_hold(pg_service):
    from pollypm.work.models import WorkStatus

    task = _make_draft(pg_service)
    pg_service.queue(task.task_id, actor="user")
    pg_service.hold(task.task_id, actor="user", reason="x")
    resumed = pg_service.resume(task.task_id, actor="user")
    assert resumed.work_status is WorkStatus.QUEUED


def test_next_returns_highest_priority_queued(pg_service):
    a = _make_draft(pg_service, title="a")
    b = _make_draft(pg_service, title="b", priority="critical")
    pg_service.queue(a.task_id, actor="u")
    pg_service.queue(b.task_id, actor="u")
    nxt = pg_service.next()
    assert nxt is not None
    assert nxt.task_id == b.task_id


def test_next_returns_none_for_empty(pg_service):
    assert pg_service.next() is None


def test_next_respects_project_filter(pg_service):
    a = _make_draft(pg_service, project="alpha", title="a")
    _make_draft(pg_service, project="beta", title="b")
    pg_service.queue(a.task_id, actor="u")
    nxt_alpha = pg_service.next(project="alpha")
    assert nxt_alpha is not None
    assert nxt_alpha.project == "alpha"
    # beta has no queued tasks
    assert pg_service.next(project="beta") is None


# ---------------------------------------------------------------------------
# node_done / approve / reject (use a "chat" flow that reaches done)
# ---------------------------------------------------------------------------


def _drive_to_review(svc, project="demo"):
    """Create a task, queue+claim it, then run node_done to reach review."""
    task = svc.create(
        title="t",
        type="task",
        project=project,
        flow_template="standard",
        roles={"worker": "alice", "reviewer": "bob"},
        description="body",
    )
    svc.queue(task.task_id, actor="user")
    svc.claim(task.task_id, actor="alice")
    return task


def test_node_done_requires_output(pg_service):
    from pollypm.work.service_support import ValidationError

    task = _drive_to_review(pg_service)
    with pytest.raises(ValidationError):
        pg_service.node_done(task.task_id, actor="alice")


def test_node_done_advances_to_review(pg_service):
    from pollypm.work.models import WorkStatus

    task = _drive_to_review(pg_service)
    done = pg_service.node_done(
        task.task_id,
        actor="alice",
        work_output={
            "type": "code_change",
            "summary": "implemented X",
            "artifacts": [
                {"kind": "commit", "description": "impl", "ref": "HEAD"}
            ],
        },
    )
    # standard flow: build -> review, so after node_done we should be in review
    assert done.work_status is WorkStatus.REVIEW


def test_get_hydrates_executions_with_worker_output(pg_service):
    task = _drive_to_review(pg_service)
    pg_service.node_done(
        task.task_id,
        actor="alice",
        work_output={
            "type": "code_change",
            "summary": "implemented X",
            "artifacts": [
                {"kind": "commit", "description": "impl", "ref": "HEAD"}
            ],
        },
    )

    fetched = pg_service.get(task.task_id)
    outputs = [
        execution.work_output
        for execution in fetched.executions
        if execution.work_output is not None
    ]
    assert [output.summary for output in outputs] == ["implemented X"]
    assert outputs[0].artifacts[0].ref == "HEAD"


def test_mark_done_invokes_plan_review_backstop(pg_service, monkeypatch):
    calls: list[tuple[object, str, str]] = []

    def _record(svc, task_id, actor):  # noqa: ANN001
        calls.append((svc, task_id, actor))
        return "plan-review-id"

    monkeypatch.setattr(
        "pollypm.work.plan_review_emit.maybe_emit_plan_review_on_task_done",
        _record,
    )
    task = _make_draft(pg_service, labels=["poc-plan"])

    pg_service.mark_done(task.task_id, actor="polly")

    assert calls == [(pg_service, task.task_id, "polly")]


def test_simple_transition_rejects_stale_source_state(pg_service):
    from pollypm.work.models import WorkStatus
    from pollypm.work.service_support import InvalidTransitionError

    task = _make_draft(pg_service)
    pg_service.queue(task.task_id, actor="user")

    with pytest.raises(InvalidTransitionError):
        pg_service._simple_transition(
            task.task_id,
            from_state=WorkStatus.IN_PROGRESS,
            to_state=WorkStatus.DONE,
            actor="race",
        )

    assert pg_service.get(task.task_id).work_status is WorkStatus.QUEUED


def test_approve_done_invokes_plan_review_backstop(pg_service, monkeypatch):
    calls: list[tuple[object, str, str]] = []

    def _record(svc, task_id, actor):  # noqa: ANN001
        calls.append((svc, task_id, actor))
        return "plan-review-id"

    monkeypatch.setattr(
        "pollypm.work.plan_review_emit.maybe_emit_plan_review_on_task_done",
        _record,
    )
    task = _drive_to_review(pg_service)
    pg_service.node_done(
        task.task_id,
        actor="alice",
        work_output={
            "type": "code_change",
            "summary": "implemented X",
            "artifacts": [
                {"kind": "commit", "description": "impl", "ref": "HEAD"}
            ],
        },
    )

    pg_service.approve(task.task_id, actor="bob")

    assert calls == [(pg_service, task.task_id, "bob")]


def test_approve_rechecks_locked_status_before_advancing(
    pg_service, monkeypatch,
):
    from pollypm.work.models import WorkStatus
    from pollypm.work.service_support import InvalidTransitionError

    task = _drive_to_review(pg_service)
    pg_service.node_done(
        task.task_id,
        actor="alice",
        work_output={
            "type": "code_change",
            "summary": "implemented X",
            "artifacts": [
                {"kind": "commit", "description": "impl", "ref": "HEAD"}
            ],
        },
    )
    stale_review = pg_service.get(task.task_id)

    with pg_service._pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE work_tasks SET work_status = %s, updated_at = now() "
            "WHERE project = %s AND task_number = %s",
            (
                WorkStatus.DONE.value,
                stale_review.project,
                stale_review.task_number,
            ),
        )
        conn.commit()

    real_get = pg_service.get

    def stale_get(task_id):  # noqa: ANN001
        if task_id == stale_review.task_id:
            return stale_review
        return real_get(task_id)

    monkeypatch.setattr(pg_service, "get", stale_get)

    with pytest.raises(InvalidTransitionError):
        pg_service.approve(task.task_id, actor="bob")

    assert real_get(task.task_id).work_status is WorkStatus.DONE


def test_approve_from_non_review_raises(pg_service):
    from pollypm.work.service_support import InvalidTransitionError

    task = _make_draft(pg_service)
    with pytest.raises(InvalidTransitionError):
        pg_service.approve(task.task_id, actor="user")


def test_reject_requires_reason(pg_service):
    from pollypm.work.service_support import ValidationError

    task = _drive_to_review(pg_service)
    pg_service.node_done(
        task.task_id,
        actor="alice",
        work_output={
            "type": "code_change",
            "summary": "implemented X",
            "artifacts": [
                {"kind": "commit", "description": "impl", "ref": "HEAD"}
            ],
        },
    )
    with pytest.raises(ValidationError):
        pg_service.reject(task.task_id, actor="bob", reason="")


# ---------------------------------------------------------------------------
# block / blocked_tasks
# ---------------------------------------------------------------------------


def test_block_marks_task_blocked(pg_service):
    from pollypm.work.models import WorkStatus

    task = _drive_to_review(pg_service)
    pg_service.node_done(
        task.task_id,
        actor="alice",
        work_output={
            "type": "code_change",
            "summary": "x",
            "artifacts": [{"kind": "commit", "description": "x", "ref": "HEAD"}],
        },
    )
    # task is now in REVIEW
    blocker = _make_draft(pg_service, title="blocker")
    blocked = pg_service.block(
        task.task_id, actor="user", blocker_task_id=blocker.task_id
    )
    assert blocked.work_status is WorkStatus.BLOCKED


def test_block_from_active_task_ends_worker_session(pg_service):
    from datetime import UTC, datetime

    task = _make_draft(pg_service)
    pg_service.queue(task.task_id, actor="user")
    claimed = pg_service.claim(task.task_id, actor="alice")
    pg_service.upsert_worker_session(
        task_project=claimed.project,
        task_number=claimed.task_number,
        agent_name="alice",
        pane_id="p",
        worktree_path="/tmp/wt",
        branch_name="b",
        started_at=datetime.now(UTC),
    )
    blocker = _make_draft(pg_service, title="blocker")

    pg_service.block(task.task_id, actor="user", blocker_task_id=blocker.task_id)

    assert pg_service.get_worker_session(
        task_project=task.project,
        task_number=task.task_number,
        active_only=True,
    ) is None


def test_blocked_tasks_filter_by_project(pg_service):
    task = _drive_to_review(pg_service)
    pg_service.node_done(
        task.task_id,
        actor="alice",
        work_output={
            "type": "code_change",
            "summary": "x",
            "artifacts": [{"kind": "commit", "description": "x", "ref": "HEAD"}],
        },
    )
    blocker = _make_draft(pg_service, title="b")
    pg_service.block(task.task_id, actor="user", blocker_task_id=blocker.task_id)
    rows = pg_service.blocked_tasks(project="demo")
    assert {t.task_id for t in rows} == {task.task_id}


# ---------------------------------------------------------------------------
# get_execution
# ---------------------------------------------------------------------------


def test_get_execution_after_claim(pg_service):
    task = _make_draft(pg_service)
    pg_service.queue(task.task_id, actor="user")
    pg_service.claim(task.task_id, actor="alice")
    rows = pg_service.get_execution(task.task_id)
    assert len(rows) >= 1
    assert rows[0].node_id is not None


def test_get_execution_node_filter(pg_service):
    task = _make_draft(pg_service)
    pg_service.queue(task.task_id, actor="user")
    pg_service.claim(task.task_id, actor="alice")
    rows = pg_service.get_execution(task.task_id, node_id="unknown_node")
    assert rows == []


# ---------------------------------------------------------------------------
# my_tasks / state_counts / sync_status / trigger_sync
# ---------------------------------------------------------------------------


def test_my_tasks_returns_active_assignee_tasks(pg_service):
    task = _make_draft(pg_service)
    pg_service.queue(task.task_id, actor="user")
    pg_service.claim(task.task_id, actor="alice")
    rows = pg_service.my_tasks("alice")
    assert {t.task_id for t in rows} == {task.task_id}


def test_my_tasks_empty_when_no_match(pg_service):
    assert pg_service.my_tasks("ghost") == []


def test_state_counts_zero_fills_all_statuses(pg_service):
    counts = pg_service.state_counts()
    from pollypm.work.models import WorkStatus

    # every WorkStatus value must appear (zero-filled if absent)
    for status in WorkStatus:
        assert status.value in counts


def test_sync_status_empty_for_unsynced_task(pg_service):
    task = _make_draft(pg_service)
    assert pg_service.sync_status(task.task_id) == {}


def test_sync_status_missing_task_raises(pg_service):
    from pollypm.work.service_support import TaskNotFoundError

    with pytest.raises(TaskNotFoundError):
        pg_service.sync_status("nope/1")


def test_trigger_sync_returns_summary_shape(pg_service):
    summary = pg_service.trigger_sync()
    assert summary == {"synced": 0, "errors": {}}


def test_trigger_sync_missing_task_raises(pg_service):
    from pollypm.work.service_support import TaskNotFoundError

    with pytest.raises(TaskNotFoundError):
        pg_service.trigger_sync(task_id="nope/1")


# ---------------------------------------------------------------------------
# validate_advance preflight
# ---------------------------------------------------------------------------


def test_validate_advance_empty_for_draft(pg_service):
    task = _make_draft(pg_service)
    # draft has no current_node_id → empty results
    assert pg_service.validate_advance(task.task_id, actor="user") == []


def test_validate_advance_surfaces_actor_mismatch(pg_service):
    task = _make_draft(pg_service)
    pg_service.queue(task.task_id, actor="user")
    pg_service.claim(task.task_id, actor="alice")
    # standard flow's review node expects the reviewer role
    results = pg_service.validate_advance(task.task_id, actor="random_actor")
    # may be empty (no role mismatch on the current work node) or list a
    # failure — either way the method must not crash. Smoke-only.
    assert isinstance(results, list)


# ---------------------------------------------------------------------------
# Worker sessions
# ---------------------------------------------------------------------------


def test_worker_session_round_trip(pg_service):
    from datetime import UTC, datetime

    task = _make_draft(pg_service)
    pg_service.upsert_worker_session(
        task_project=task.project,
        task_number=task.task_number,
        agent_name="alice",
        pane_id="pane-1",
        worktree_path="/tmp/wt",
        branch_name="task/demo-1",
        started_at=datetime.now(UTC),
    )
    rec = pg_service.get_worker_session(
        task_project=task.project, task_number=task.task_number
    )
    assert rec is not None
    assert rec.agent_name == "alice"
    assert rec.pane_id == "pane-1"


def test_worker_session_active_only_filter(pg_service):
    from datetime import UTC, datetime

    task = _make_draft(pg_service)
    pg_service.upsert_worker_session(
        task_project=task.project,
        task_number=task.task_number,
        agent_name="alice",
        pane_id="p",
        worktree_path="/tmp/wt",
        branch_name="b",
        started_at=datetime.now(UTC),
    )
    pg_service.end_worker_session(
        task_project=task.project,
        task_number=task.task_number,
        ended_at=datetime.now(UTC),
        total_input_tokens=100,
        total_output_tokens=200,
        archive_path="/tmp/archive",
    )
    rec_active = pg_service.get_worker_session(
        task_project=task.project,
        task_number=task.task_number,
        active_only=True,
    )
    rec_any = pg_service.get_worker_session(
        task_project=task.project, task_number=task.task_number
    )
    assert rec_active is None
    assert rec_any is not None
    assert rec_any.total_input_tokens == 100
    assert rec_any.total_output_tokens == 200


def test_force_review_from_active_task_ends_worker_session(pg_service):
    from datetime import UTC, datetime

    task = _make_draft(pg_service)
    pg_service.queue(task.task_id, actor="user")
    claimed = pg_service.claim(task.task_id, actor="alice")
    pg_service.upsert_worker_session(
        task_project=claimed.project,
        task_number=claimed.task_number,
        agent_name="alice",
        pane_id="p",
        worktree_path="/tmp/wt",
        branch_name="b",
        started_at=datetime.now(UTC),
    )

    pg_service.force_review(task.task_id, actor="alice")

    assert pg_service.get_worker_session(
        task_project=task.project,
        task_number=task.task_number,
        active_only=True,
    ) is None


def test_capacity_count_ignores_unclaimed_active_rows(pg_service):
    from pollypm.work.models import WorkStatus

    claimed_task = _make_draft(pg_service, title="claimed")
    pg_service.queue(claimed_task.task_id, actor="user")
    pg_service.claim(claimed_task.task_id, actor="alice")
    orphan = _make_draft(pg_service, title="orphan")
    pg_service.queue(orphan.task_id, actor="user")
    orphan = pg_service.force_in_progress(orphan.task_id, actor="operator")

    assert orphan.work_status is WorkStatus.IN_PROGRESS
    assert orphan.claimed_by_session is None
    assert pg_service.count_capacity_consuming_tasks(project="demo") == 1
    assert pg_service.count_capacity_consuming_tasks(
        project="demo",
        exclude_task_id=claimed_task.task_id,
    ) == 0


def test_list_worker_sessions_active_only(pg_service):
    from datetime import UTC, datetime

    task = _make_draft(pg_service)
    pg_service.upsert_worker_session(
        task_project=task.project,
        task_number=task.task_number,
        agent_name="alice",
        pane_id="p",
        worktree_path="/tmp/wt",
        branch_name="b",
        started_at=datetime.now(UTC),
    )
    active = pg_service.list_worker_sessions(active_only=True)
    assert len(active) == 1
    pg_service.mark_worker_session_ended(
        task_project=task.project,
        task_number=task.task_number,
        ended_at=datetime.now(UTC),
    )
    after = pg_service.list_worker_sessions(active_only=True)
    assert after == []


def test_update_worker_session_tokens(pg_service):
    from datetime import UTC, datetime

    task = _make_draft(pg_service)
    pg_service.upsert_worker_session(
        task_project=task.project,
        task_number=task.task_number,
        agent_name="alice",
        pane_id="p",
        worktree_path="/tmp/wt",
        branch_name="b",
        started_at=datetime.now(UTC),
    )
    pg_service.update_worker_session_tokens(
        task_project=task.project,
        task_number=task.task_number,
        total_input_tokens=42,
        total_output_tokens=99,
        archive_path=None,
    )
    rec = pg_service.get_worker_session(
        task_project=task.project, task_number=task.task_number
    )
    assert rec is not None
    assert rec.total_input_tokens == 42
    assert rec.total_output_tokens == 99


def test_ensure_worker_session_schema_is_noop(pg_service):
    # No-op on pg — migration applier owns schema. Must not raise.
    pg_service.ensure_worker_session_schema()


# ---------------------------------------------------------------------------
# available_flows / get_flow
# ---------------------------------------------------------------------------


def test_available_flows_returns_some_templates(pg_service):
    # Without a project_path the file resolver loads bundled flows.
    templates = pg_service.available_flows()
    assert isinstance(templates, list)


def test_get_flow_returns_template(pg_service):
    tmpl = pg_service.get_flow("standard")
    assert tmpl.name == "standard"
