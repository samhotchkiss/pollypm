"""SQLite-backed implementation of the WorkService protocol.

Contract:
- Inputs: typed task/workflow fields, notification metadata, dependency
  links, worker-session updates, and sync adapter names.
- Outputs: typed ``Task`` / ``WorkerSessionRecord`` models plus durable
  side effects in the work SQLite database.
- Side effects: owns schema creation, task state transitions, workflow
  execution bookkeeping, dependency mutations, notification staging, and
  sync-attempt persistence.
- Invariants: this module is the only owner of work-database connection
  lifecycle and schema writes; callers use typed methods instead of
  reaching into ``_conn`` or issuing ad-hoc SQL.
- Allowed dependencies: flow engine, gates, schema helpers, and the
  boundary-owned ``service_*.py`` submodules in this package.
- Private: SQL row shapes, sqlite connection helpers, and internal
  adapter plumbing delegated to the ``service_*.py`` modules.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)


# #894 — register the work_service module as an emitter that routes
# through SignalEnvelope. The release gate's
# signal_routing_emitters check inspects ROUTED_EMITTERS for this
# name. Representative migration site: ``maybe_record_first_shipped``
# below builds a SignalEnvelope before the underlying
# ``store.enqueue_message`` write, so the canonical routing policy
# (audience / actionability / dedupe) is exercised on a real path.
from pollypm.signal_routing import (  # noqa: E402
    SignalActionability,
    SignalAudience,
    SignalEnvelope,
    SignalSeverity,
    compute_dedupe_key,
    register_routed_emitter,
    route_signal,
)

register_routed_emitter("work_service")

from pollypm.atomic_io import atomic_write_json
from pollypm.work.flow_engine import resolve_flow
from pollypm.work.gates import GateRegistry, evaluate_gates
from pollypm.work.models import (
    GateResult,
    ActorType,
    Artifact,
    ArtifactKind,
    ContextEntry,
    Decision,
    ExecutionStatus,
    FlowNode,
    FlowNodeExecution,
    FlowTemplate,
    LinkKind,
    NodeType,
    OutputType,
    Priority,
    Task,
    TaskType,
    Transition,
    DigestRollupCandidate,
    WorkerSessionRecord,
    WorkOutput,
    WorkStatus,
    TERMINAL_STATUSES,
)
from pollypm.work.schema import create_work_tables
from pollypm.work.service_notifications import (
    find_flushed_rollup_milestone,
    has_old_pending_digest_rows,
    list_digest_rollup_candidates,
    mark_rollup_candidates_flushed,
    prune_staged_notifications,
    stage_notification_row,
)
from pollypm.work.service_queries import (
    blocked_tasks as read_blocked_tasks,
    create_task,
    get_task,
    list_nonterminal_tasks as read_nonterminal_tasks,
    list_tasks as read_tasks,
    my_tasks as read_my_tasks,
    next_task,
    state_counts as read_state_counts,
    update_task,
)
from pollypm.work.service_support import (  # noqa: F401  (re-exported)
    InvalidTransitionError,
    InvariantViolationError,
    TaskNotFoundError,
    ValidationError,
    WorkServiceError,
    _now,
    _parse_task_id,
)
from pollypm.work.service_sync import (
    record_sync_state,
    sync_status as read_sync_status,
    trigger_sync as run_trigger_sync,
)
from pollypm.work.service_dependency_manager import WorkDependencyManager
from pollypm.work.service_worker_session_manager import WorkSessionManager
from pollypm.work.service_transitions import (
    advance_to_node,
    current_node_visit as read_current_node_visit,
    next_visit as read_next_visit,
    on_task_done,
    on_task_transition,
)
from pollypm.work.service_transition_manager import WorkTransitionManager
from pollypm.work.sync import SyncManager


_STATE_FILENAME = "state.json"


class _HasExecutions(Protocol):
    def get_execution(
        self,
        task_id: str,
        node_id: str | None = None,
        visit: int | None = None,
    ) -> list[Any]:
        ...


def state_path() -> Path:
    return Path.home() / ".pollypm" / _STATE_FILENAME


def load_state(path: Path | None = None) -> dict[str, Any]:
    resolved = path or state_path()
    if not resolved.exists():
        return {}
    try:
        payload = json.loads(resolved.read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def first_shipped_at(path: Path | None = None) -> str | None:
    value = load_state(path).get("first_shipped_at")
    return str(value) if isinstance(value, str) and value else None


def _safe_json_dict(raw: object) -> dict:
    """Decode a JSON column expected to be a dict, defensively.

    Producers always serialise dicts (``json.dumps(template.roles)`` etc.),
    but a hand-edited or legacy DB row could land an empty string,
    null, list, or scalar. Downstream callers do ``parsed.get(...)``
    and would AttributeError on those shapes — propagating the crash
    out of the caller's loop. Coerce non-dict shapes to ``{}`` so a
    single corrupt row degrades gracefully.

    Mirrors the ``_safe_payload`` / ``_safe_tags`` helpers in
    ``pollypm.storage.state`` (cycles 107-109 corrupt-payload defenses).
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _safe_json_list(raw: object) -> list:
    """Decode a JSON column expected to be a list, defensively.

    Companion to ``_safe_json_dict`` for ``labels`` / ``relevant_files``
    / ``gates`` columns. Coerces non-list shapes (dict/string/null/int)
    back to ``[]`` so consumers iterating the result don't iterate the
    wrong thing (e.g. a string would yield characters).
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


_POLLYPM_GENERATED_DOC_FILES = frozenset({
    "project-overview.md",
    "decisions.md",
    "architecture.md",
    "history.md",
    "conventions.md",
    "deprecated-facts.md",
    "history-import-questions.md",
})


def _gitignore_without_pollypm_entry(text: str) -> tuple[str, ...]:
    return tuple(
        line.rstrip()
        for line in text.splitlines()
        if line.strip() and line.strip() != ".pollypm/"
    )


def _gitignore_change_is_pollypm_only(project_path: Path) -> bool:
    gitignore_path = project_path / ".gitignore"
    if not gitignore_path.exists():
        return False
    try:
        current = gitignore_path.read_text(encoding="utf-8")
    except OSError:
        return False
    if ".pollypm/" not in current.splitlines():
        return False

    previous = subprocess.run(
        ["git", "-C", str(project_path), "show", "HEAD:.gitignore"],
        capture_output=True,
        text=True,
        check=False,
    )
    if previous.returncode != 0:
        return _gitignore_without_pollypm_entry(current) == ()
    return (
        _gitignore_without_pollypm_entry(current)
        == _gitignore_without_pollypm_entry(previous.stdout)
    )


def _doc_file_is_pollypm_generated(path: Path) -> bool:
    if path.name not in _POLLYPM_GENERATED_DOC_FILES:
        return False
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return False
    if path.name == "history-import-questions.md":
        return content.startswith("# Review:") and "history import" in content
    return "*Last updated:" in content


def _docs_change_is_pollypm_only(project_path: Path, rel_path: str) -> bool:
    docs_root = project_path / "docs"
    candidate = project_path / rel_path.rstrip("/")
    if rel_path.rstrip("/") == "docs":
        if not docs_root.exists():
            return False
        files = [path for path in docs_root.rglob("*") if path.is_file()]
    elif candidate.is_file() and candidate.parent == docs_root:
        files = [candidate]
    else:
        return False
    return bool(files) and all(_doc_file_is_pollypm_generated(path) for path in files)


def _issues_path_is_pollypm_managed(rel_path: str) -> bool:
    """Return True if ``rel_path`` (relative to the project root) lies
    inside the PollyPM-managed ``issues/`` tree.

    Every file under ``issues/`` is written exclusively by the
    file-sync adapter / ``FileTaskBackend`` (phase folders, the
    per-task markdown snapshots, ``.latest_issue_number``, and the
    seeded ``notes.md`` / ``progress-log.md`` / ``instructions.md``).
    The user never edits paths inside this tree by hand, so any dirty
    or untracked entry whose path starts with ``issues/`` is safe to
    treat as scaffold-only and is allowed past the approve dirty-tree
    gate (#930).
    """
    cleaned = rel_path.rstrip("/")
    return cleaned == "issues" or cleaned.startswith("issues/")


# #945 — markers used to recognise PollyPM-generated itsalive scaffold
# files that ``pm itsalive`` writes into the project root. Approving a
# task on a project that's already wired into itsalive must not bounce
# on these untracked files.
_ITSALIVE_DOC_MARKER = "Generated by PollyPM's itsalive integration"
_CLAUDE_POINTER_MARKER = "See ITSALIVE.md for itsalive.co deployment"
# Pointer-stub CLAUDE.md is one short line; cap well under 1KB so a
# user-edited CLAUDE.md (which can grow quickly) keeps blocking.
_CLAUDE_POINTER_MAX_BYTES = 200


def _itsalive_path_is_pollypm_managed(project_path: Path, rel_path: str) -> bool:
    """Return True if ``rel_path`` is one of the three itsalive scaffold
    files PollyPM writes during ``pm itsalive`` setup AND the contents
    still match the generated template.

    - ``.itsalive``: deployToken JSON, exclusively PollyPM-managed.
      Always allowed.
    - ``ITSALIVE.md``: doc file with a "Generated by PollyPM's itsalive
      integration" marker. Allowed only when the marker is present so a
      hand-written ITSALIVE.md keeps surfacing as real dirt.
    - ``CLAUDE.md``: by default we leave CLAUDE.md alone because users
      do edit it. The pointer-stub form ``pm itsalive`` writes when no
      CLAUDE.md exists is a single line under 200 bytes; we recognise
      that exact stub and let it through, but anything larger or any
      file lacking the pointer marker keeps blocking.
    """
    cleaned = rel_path.rstrip("/")
    if cleaned == ".itsalive":
        return True
    if cleaned == "ITSALIVE.md":
        candidate = project_path / cleaned
        try:
            content = candidate.read_text(encoding="utf-8")
        except OSError:
            return False
        return _ITSALIVE_DOC_MARKER in content
    if cleaned == "CLAUDE.md":
        candidate = project_path / cleaned
        try:
            stat = candidate.stat()
        except OSError:
            return False
        if stat.st_size > _CLAUDE_POINTER_MAX_BYTES:
            return False
        try:
            content = candidate.read_text(encoding="utf-8")
        except OSError:
            return False
        return _CLAUDE_POINTER_MARKER in content
    return False


def _porcelain_status_path(line: str) -> str:
    """Parse a single ``git status --porcelain`` line into its rel_path.

    Mirrors the slicing used by :func:`_status_is_only_pollypm_scaffold`:
    each porcelain line is 2 status chars + space + path. The status
    chars can be a literal space (e.g. ``` M`` for "modified in
    worktree, not staged") so we slice from index 3 directly. Renames
    are encoded as ``orig -> new``; we want the destination path.
    """
    rel_path = line[3:] if len(line) > 3 else ""
    rel_path = rel_path.rstrip()
    if " -> " in rel_path:
        rel_path = rel_path.rsplit(" -> ", 1)[1].strip()
    return rel_path.strip('"')


def _path_is_pollypm_scaffold(project_path: Path, rel_path: str) -> bool:
    """Return True if ``rel_path`` matches the approve-gate allowlist.

    Mirrors the per-line predicates in
    :func:`_status_is_only_pollypm_scaffold`: gitignore + docs + the
    #930 ``issues/`` tree + the #945 itsalive scaffold trio. Reused by
    the #946 pre-stage step so the auto-merge code path can stage
    exactly the files the dirty-tree gate already lets through.
    """
    if rel_path == ".gitignore" and _gitignore_change_is_pollypm_only(project_path):
        return True
    if (rel_path == "docs" or rel_path.startswith("docs/")) and (
        _docs_change_is_pollypm_only(project_path, rel_path)
    ):
        return True
    if _issues_path_is_pollypm_managed(rel_path):
        return True
    if _itsalive_path_is_pollypm_managed(project_path, rel_path):
        return True
    return False


def _status_is_only_pollypm_scaffold(project_path: Path, status_stdout: str) -> bool:
    lines = [line for line in status_stdout.splitlines() if line.strip()]
    if not lines:
        return False
    for line in lines:
        rel_path = _porcelain_status_path(line)
        if not _path_is_pollypm_scaffold(project_path, rel_path):
            return False
    return True


def _union_merge_text(ours: str, theirs: str) -> str:
    """Concatenate two line-oriented texts, dedupe preserving first-seen order.

    Used to resolve add/add conflicts on concat-safe files (.gitignore et al.)
    where a union of both sides' lines is the correct merge result. Trailing
    newline is preserved if either input had one.
    """
    seen: set[str] = set()
    out: list[str] = []
    needs_trailing_newline = False
    for source in (ours, theirs):
        if not source:
            continue
        if source.endswith("\n"):
            needs_trailing_newline = True
        for line in source.splitlines():
            if line in seen:
                continue
            seen.add(line)
            out.append(line)
    if not out:
        return ""
    text = "\n".join(out)
    if needs_trailing_newline:
        text += "\n"
    return text


def _resolve_disjoint_addition_conflicts(text: str) -> str | None:
    """Resolve diff3 conflict markers when every hunk is a pure disjoint
    addition.

    Expects ``text`` to contain ``<<<<<<<`` / ``|||||||`` / ``=======``
    / ``>>>>>>>`` markers (i.e. ``--diff3`` style). For each conflict
    hunk, the function only auto-resolves when the BASE section is
    empty — meaning neither side modified existing content, both
    independently added new code in the same region (the issue's
    case 1: "disjoint new code in the same region"). The resolution
    is ``ours_lines + theirs_lines``.

    Returns ``None`` (= bounce to operator) when:
    - any hunk has a non-empty base section (both sides modified a
      shared line — case 3, "needs human / worker judgement")
    - markers are malformed or text has no conflict markers at all
      (e.g. pure add/add with no textual base, or binary conflict)

    #1072: parallel task branches frequently produce exactly this
    shape (polly_remote/13 repro: HEAD added new commands, /13 added
    different new commands, both at EOF of the same file with no base
    content disturbed). This helper lets approve self-heal that case
    instead of bouncing to the operator.
    """
    if "<<<<<<<" not in text or "|||||||" not in text:
        # Either no conflict at all, or not in diff3 format — bounce.
        return None
    out: list[str] = []
    ours: list[str] | None = None
    base: list[str] | None = None
    theirs: list[str] | None = None
    state = "outside"  # outside | ours | base | theirs
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        if state == "outside":
            if stripped.startswith("<<<<<<<"):
                ours = []
                base = None
                theirs = None
                state = "ours"
                continue
            out.append(line)
            continue
        if state == "ours":
            if stripped.startswith("|||||||"):
                base = []
                state = "base"
                continue
            if stripped.startswith("======="):
                # No base section — not diff3, bail.
                return None
            if stripped.startswith("<<<<<<<") or stripped.startswith(">>>>>>>"):
                return None
            assert ours is not None
            ours.append(line)
            continue
        if state == "base":
            if stripped.startswith("======="):
                theirs = []
                state = "theirs"
                continue
            if stripped.startswith("<<<<<<<") or stripped.startswith(">>>>>>>"):
                return None
            assert base is not None
            base.append(line)
            continue
        if state == "theirs":
            if stripped.startswith(">>>>>>>"):
                assert ours is not None and theirs is not None and base is not None
                # The "disjoint addition" rule: empty base means the
                # region was untouched in the merge ancestor and both
                # sides are pure additions there. If the base has any
                # content, both sides modified shared lines and a
                # human / worker should review.
                if base:
                    return None
                # Even with empty base, if ours and theirs share any
                # non-blank line, naive concat would duplicate it.
                # That's a signal of a pure add/add where both sides
                # independently wrote the same boilerplate (e.g. the
                # README.md headers in the unsafe-addadd test): the
                # textual answer is ambiguous, so bounce.
                ours_nonblank = {
                    line.strip() for line in ours if line.strip()
                }
                theirs_nonblank = {
                    line.strip() for line in theirs if line.strip()
                }
                if ours_nonblank & theirs_nonblank:
                    return None
                out.extend(ours)
                out.extend(theirs)
                ours = None
                base = None
                theirs = None
                state = "outside"
                continue
            if stripped.startswith("<<<<<<<") or stripped.startswith("======="):
                return None
            assert theirs is not None
            theirs.append(line)
            continue
    if state != "outside":
        return None
    return "".join(out)


def mark_first_shipped(
    *,
    path: Path | None = None,
    when: datetime | None = None,
) -> bool:
    resolved = path or state_path()
    state = load_state(resolved)
    if isinstance(state.get("first_shipped_at"), str) and state["first_shipped_at"]:
        return False
    state["first_shipped_at"] = (when or datetime.now(UTC)).isoformat()
    atomic_write_json(resolved, state)
    return True


def _record_first_shipped_activity(
    *,
    project_path: Path | None,
    project_key: str | None,
    when: datetime | None = None,
) -> None:
    """Persist the one-time shipment milestone into the project feed."""
    if project_path is None:
        return
    try:
        from pollypm.store import SQLAlchemyStore
    except Exception:  # noqa: BLE001
        return

    state_db = project_path / ".pollypm" / "state.db"
    state_db.parent.mkdir(parents=True, exist_ok=True)
    shipped_at = (when or datetime.now(UTC)).isoformat()
    body = json.dumps(
        {
            "summary": "First PR shipped with Polly 🎉",
            "severity": "routine",
            "verb": "celebrated",
            "subject": "first shipment",
            "project": project_key,
            "shipped_at": shipped_at,
        }
    )
    payload = {
        "kind": "first_shipped",
        "project": project_key,
        "pinned": True,
        "shipped_at": shipped_at,
    }
    # #894 — route through SignalEnvelope before the legacy
    # store.enqueue_message write. first_shipped is informational —
    # it lands on Activity + Inbox without toasting (the user
    # discovers the celebration in their feed).
    route_signal(
        SignalEnvelope(
            audience=SignalAudience.USER,
            severity=SignalSeverity.INFO,
            actionability=SignalActionability.INFORMATIONAL,
            source="work_service",
            subject="First PR shipped",
            body="First PR shipped with Polly 🎉",
            project=project_key,
            dedupe_key=compute_dedupe_key(
                source="work_service",
                kind="first_shipped",
                target=project_key,
            ),
            payload=payload,
        )
    )
    store = SQLAlchemyStore(f"sqlite:///{state_db}")
    try:
        store.enqueue_message(
            type="event",
            tier="immediate",
            recipient="*",
            sender="polly",
            subject="first_shipped",
            body=body,
            scope="polly",
            payload=payload,
        )
    finally:
        store.close()


def task_landed_commit(service: _HasExecutions, task_id: str) -> bool:
    try:
        executions = service.get_execution(task_id)
    except Exception:  # noqa: BLE001
        return False
    for execution in reversed(executions):
        work_output = getattr(execution, "work_output", None)
        if work_output is None:
            continue
        artifacts = getattr(work_output, "artifacts", None) or []
        for artifact in artifacts:
            if getattr(artifact, "kind", None) == ArtifactKind.COMMIT:
                return True
    return False


def maybe_record_first_shipped(
    service: _HasExecutions,
    task_id: str,
    *,
    path: Path | None = None,
    project_path: Path | None = None,
    when: datetime | None = None,
) -> bool:
    if not task_landed_commit(service, task_id):
        return False
    created = mark_first_shipped(path=path, when=when)
    if not created:
        return False
    project_key = task_id.split("/", 1)[0] if "/" in task_id else None
    _record_first_shipped_activity(
        project_path=project_path,
        project_key=project_key,
        when=when,
    )
    return True


# ---------------------------------------------------------------------------
# SQLiteWorkService
# ---------------------------------------------------------------------------


class SQLiteWorkService:
    """SQLite-backed work service implementing the WorkService protocol."""

    # TODO(savethenovel-followup): emit ``work_table.cleared`` from
    # the codepath that recreates ``work_tasks`` from scratch (DB
    # rebuild on first open against a missing/empty file, or any
    # admin reset path). Today the only ``DELETE FROM work_tasks``
    # is the per-row prune in :meth:`_prune_cancelled_critique_child`
    # which is already audited as ``task.deleted``. The wholesale
    # wipe vector that prompted this audit log is suspected to be
    # an external ``pm reset``-class operation; instrument that
    # caller (and any future bulk-clear path) here.
    def __init__(
        self,
        db_path: Path,
        project_path: Path | None = None,
        sync_manager: SyncManager | None = None,
        session_manager: object | None = None,
    ) -> None:
        self._db_path = db_path
        self._project_path = project_path
        self._sync = sync_manager
        self._session_mgr = session_manager
        # #1095: route through ``open_workspace_db`` so the
        # ``OperationalError: unable to open database file`` branch
        # carries structured diagnostics (path tried, exists?, size,
        # parent writable, stale -wal / -shm sidecars). Three workers
        # hit this intermittently in the wild and we couldn't tell
        # which path/sidecar was at fault. Any other open error keeps
        # the original message.
        from pollypm.storage.sqlite_pragmas import open_workspace_db

        self._conn = open_workspace_db(db_path, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        # #1018: centralised WAL + busy_timeout. Wait up to 30 s for
        # busy locks before raising — prevents the Textual inbox UI
        # from erroring when the heartbeat or other writers hold the
        # DB briefly.
        from pollypm.storage.sqlite_pragmas import apply_workspace_pragmas

        apply_workspace_pragmas(self._conn, busy_timeout_ms=30000)
        self._conn.execute("PRAGMA foreign_keys=ON")
        # #savethenovel-followup: forensic breadcrumb for the dual-DB
        # layout. The workspace has TWO state DBs by accident — the
        # CLI-side ``~/.pollypm/state.db`` (messages/sessions) and the
        # workspace-local ``<root>/.pollypm/state.db`` (work tables) —
        # and several callsites historically passed the wrong path,
        # silently stamping empty work tables on the messages-side DB
        # and looking like a wipe. Snapshot the pre-existing tables so
        # the audit row distinguishes "first-time workspace open" from
        # "stamped onto the wrong DB".
        _had_messages_table_pre_open = False
        _had_work_tables_pre_open = False
        try:
            _row = self._conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name IN ('messages', 'work_tasks')",
            ).fetchall()
            _names = {r[0] for r in _row}
            _had_messages_table_pre_open = "messages" in _names
            _had_work_tables_pre_open = "work_tasks" in _names
        except Exception:  # noqa: BLE001 — diagnostic only
            pass
        create_work_tables(self._conn)
        # Emit AFTER stamping so ``tables_created`` reflects whether
        # ``create_work_tables`` actually had work to do. Wrapped in a
        # broad try/except so audit infra failure cannot block work-
        # service init — matches the pattern used at the other audit
        # callsites in this module.
        try:
            from pollypm.audit import emit as _audit_emit
            from pollypm.audit.log import EVENT_WORK_DB_OPENED

            # ``emit()`` skips the central tail when ``project`` is
            # empty (no project file to address), so use a synthetic
            # workspace-level key. Keeps these rows out of any real
            # project's central file while still landing them on disk
            # for ``pm doctor`` / heartbeat to grep.
            _audit_emit(
                event=EVENT_WORK_DB_OPENED,
                project="_workspace",
                subject=str(db_path),
                actor="system",
                metadata={
                    "had_messages_table_pre_open": _had_messages_table_pre_open,
                    "tables_created": not _had_work_tables_pre_open,
                    "project_path": (
                        str(project_path) if project_path is not None else None
                    ),
                },
                project_path=project_path,
            )
        except Exception:  # noqa: BLE001 — audit must never break init
            pass
        self._gate_registry = GateRegistry(project_path=project_path)
        self._flow_cache: dict[tuple[str, int], FlowTemplate] = {}
        self._work_output_cache: dict[str, dict[str, Any]] = {}
        self._dependency_mgr = WorkDependencyManager(self)
        self._transition_mgr = WorkTransitionManager(self)
        self._worker_session_mgr = WorkSessionManager(self)
        # Last-provision-error breadcrumb — set by ``claim()`` when
        # ``provision_worker`` fails so the CLI can surface it instead
        # of reporting a silent success (#243).
        self.last_provision_error: str | None = None
        self.last_first_shipped_created: bool = False

    def set_session_manager(self, session_manager: object) -> None:
        """Wire up the session manager after construction.

        This supports two-phase init: the service is created first, then
        the session manager (which needs a reference to the service) is
        created and registered back.
        """
        self._session_mgr = session_manager

    def close(self) -> None:
        """Close the database connection."""
        self._flow_cache.clear()
        self._work_output_cache.clear()
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _sync_transition(self, task: Task, old_status: str, new_status: str) -> None:
        """Fire sync adapter hooks + task-assignment bus for a state transition."""
        if self._sync:
            self._sync.on_transition(task, old_status, new_status)

        # Emit a TaskAssignmentEvent whenever the task's new current node
        # is waiting on a non-user actor. The event bus is consumed by
        # the built-in ``task_assignment_notify`` plugin which resolves
        # the target session by naming convention and sends a short
        # imperative ping — see ``docs/`` and issue #244.
        #
        # Best-effort: bus dispatch never bubbles exceptions, and any
        # lookup error here is swallowed so the transition always
        # commits cleanly.
        try:
            if not task.flow_template_id:
                return
            # #927: a transition into a terminal / parked status (cancel,
            # done, on_hold, draft) must not enqueue a fresh assignment
            # ping. The task's flow node may still point to a machine
            # actor (cancel doesn't unwind ``current_node_id``), so
            # without this guard the notify listener would route a ping
            # to a worker for a task that was just cancelled. The sweep
            # path has its own guard (see ``_NON_ACTIVE_SWEEP_STATUSES``)
            # so the two emitters stay aligned.
            if task.work_status in (
                WorkStatus.CANCELLED,
                WorkStatus.DONE,
                WorkStatus.ON_HOLD,
                WorkStatus.DRAFT,
                WorkStatus.BLOCKED,
            ):
                return
            flow = self._load_flow_from_db(
                task.flow_template_id, task.flow_template_version,
            )
            # Task nodes don't live on the task until claim() wires
            # ``current_node_id``. For a queued task (and similarly for
            # a freshly-queued task with no active execution), the
            # *implicit* current node is the flow's start node — that's
            # where the next actor will pick up. The spec treats the
            # queued→worker handoff as a first-class assignment event,
            # so we fall back to ``flow.start_node`` when the task has
            # no explicit current node set.
            node_id = task.current_node_id or flow.start_node
            if not node_id:
                return
            node = flow.nodes.get(node_id)
            if node is None:
                return
            from pollypm.work.task_assignment import (
                build_event_from_task,
                dispatch as _dispatch_task_assignment,
            )
            # Inject the effective current_node so build_event sees it
            # on tasks that haven't yet been claimed.
            effective_task = task
            if task.current_node_id is None:
                # Shallow copy avoids mutating the caller's dataclass.
                from dataclasses import replace
                effective_task = replace(task, current_node_id=node_id)
            # #279: carry the current node's visit counter so the
            # notifier's dedupe treats a reject-bounce (which opens a
            # fresh execution row at a higher visit) as a new ping
            # opportunity instead of suppressing it inside the 30-min
            # window keyed on (session, task).
            try:
                execution_version = self.current_node_visit(
                    effective_task.project,
                    effective_task.task_number,
                    node_id,
                )
            except Exception:  # noqa: BLE001
                execution_version = 0
            event = build_event_from_task(
                effective_task,
                node,
                transitioned_by=task.assignee or "system",
                execution_version=execution_version,
            )
            if event is not None:
                _dispatch_task_assignment(event)
        except Exception:  # noqa: BLE001
            logger.debug(
                "task_assignment dispatch failed for %s", task.task_id,
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Internal: flow template persistence
    # ------------------------------------------------------------------

    @staticmethod
    def _flow_content_hash(template: FlowTemplate) -> str:
        """Compute a stable content hash over the flow's structural fields.

        Used to detect YAML changes. We intentionally ignore the ``version``
        and ``is_current`` fields so that re-saving the same content with
        a different version number doesn't count as a change.
        """
        payload = {
            "description": template.description,
            "roles": template.roles,
            "start_node": template.start_node,
            "nodes": {
                nid: {
                    "name": n.name,
                    "type": n.type.value,
                    "actor_type": n.actor_type.value if n.actor_type else None,
                    "actor_role": n.actor_role,
                    "agent_name": n.agent_name,
                    "next_node_id": n.next_node_id,
                    "reject_node_id": n.reject_node_id,
                    "gates": n.gates,
                }
                for nid, n in template.nodes.items()
            },
        }
        serialised = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialised.encode("utf-8")).hexdigest()

    def _insert_flow_version(
        self, template: FlowTemplate, version: int,
    ) -> None:
        """Persist ``template`` at ``version`` in work_flow_templates/nodes."""
        self._invalidate_flow_cache(name=template.name, version=version)
        now = _now()
        self._conn.execute(
            "INSERT INTO work_flow_templates "
            "(name, version, description, roles, start_node, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                template.name,
                version,
                template.description,
                json.dumps(template.roles),
                template.start_node,
                now,
            ),
        )
        for node_id, node in template.nodes.items():
            self._conn.execute(
                "INSERT INTO work_flow_nodes "
                "(flow_template_name, flow_template_version, node_id, name, "
                "type, actor_type, actor_role, agent_name, "
                "next_node_id, reject_node_id, gates) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    template.name,
                    version,
                    node_id,
                    node.name,
                    node.type.value,
                    node.actor_type.value if node.actor_type else None,
                    node.actor_role,
                    node.agent_name,
                    node.next_node_id,
                    node.reject_node_id,
                    json.dumps(node.gates),
                ),
            )

    def _invalidate_flow_cache(
        self,
        *,
        name: str | None = None,
        version: int | None = None,
    ) -> None:
        if name is None:
            self._flow_cache.clear()
            return
        if version is not None:
            self._flow_cache.pop((name, version), None)
            return
        stale = [key for key in self._flow_cache if key[0] == name]
        for key in stale:
            self._flow_cache.pop(key, None)

    def _ensure_flow_in_db(self, name: str) -> FlowTemplate:
        """Load a flow via the engine and persist it, bumping version on change.

        - If no row exists for this flow name, persist at version 1 (or the
          template's stated version).
        - If the latest stored row matches the current YAML content hash,
          reuse that version.
        - If the latest stored row differs from the current YAML, INSERT a
          new row at max(version)+1, keeping older versions intact so
          in-flight tasks that reference them still execute the old graph.

        Returns a FlowTemplate whose ``version`` is the one new tasks
        should be pinned to.
        """
        template = resolve_flow(name, self._project_path)
        current_hash = self._flow_content_hash(template)

        # Look up the latest stored version for this flow.
        latest = self._conn.execute(
            "SELECT version FROM work_flow_templates "
            "WHERE name = ? ORDER BY version DESC LIMIT 1",
            (template.name,),
        ).fetchone()

        if latest is None:
            # First time we've seen this flow. Persist at the stated version.
            self._insert_flow_version(template, template.version)
            self._conn.commit()
            return template

        latest_version = int(latest["version"])

        # Compare stored content hash at latest version to the current YAML.
        stored = self._load_flow_from_db(template.name, latest_version)
        stored_hash = self._flow_content_hash(stored)

        if stored_hash == current_hash:
            # Unchanged — reuse the latest version.
            # Return a template object whose ``version`` reflects the stored
            # row so new tasks get pinned correctly.
            if template.version != latest_version:
                template = FlowTemplate(
                    name=template.name,
                    description=template.description,
                    roles=template.roles,
                    nodes=template.nodes,
                    start_node=template.start_node,
                    version=latest_version,
                    is_current=True,
                )
            return template

        # YAML differs — bump to a new version row.
        new_version = latest_version + 1
        self._insert_flow_version(template, new_version)
        self._conn.commit()

        return FlowTemplate(
            name=template.name,
            description=template.description,
            roles=template.roles,
            nodes=template.nodes,
            start_node=template.start_node,
            version=new_version,
            is_current=True,
        )

    def _load_flow_from_db(self, name: str, version: int) -> FlowTemplate:
        """Load a flow template from the database."""
        cache_key = (name, version)
        cached = self._flow_cache.get(cache_key)
        if cached is not None:
            return cached
        row = self._conn.execute(
            "SELECT * FROM work_flow_templates WHERE name = ? AND version = ?",
            (name, version),
        ).fetchone()
        if row is None:
            # Fall back to engine resolution
            flow = resolve_flow(name, self._project_path)
            self._flow_cache[cache_key] = flow
            return flow

        roles = _safe_json_dict(row["roles"])
        nodes: dict[str, FlowNode] = {}
        node_rows = self._conn.execute(
            "SELECT * FROM work_flow_nodes "
            "WHERE flow_template_name = ? AND flow_template_version = ?",
            (name, version),
        ).fetchall()
        for nr in node_rows:
            # agent_name column may not exist on very old DBs — guard with
            # a dict-style fallback.
            try:
                agent_name = nr["agent_name"]
            except (IndexError, KeyError):
                agent_name = None
            nodes[nr["node_id"]] = FlowNode(
                name=nr["name"],
                type=NodeType(nr["type"]),
                actor_type=ActorType(nr["actor_type"]) if nr["actor_type"] else None,
                actor_role=nr["actor_role"],
                agent_name=agent_name,
                next_node_id=nr["next_node_id"],
                reject_node_id=nr["reject_node_id"],
                gates=_safe_json_list(nr["gates"]),
            )

        flow = FlowTemplate(
            name=row["name"],
            description=row["description"],
            roles=roles,
            nodes=nodes,
            start_node=row["start_node"],
            version=row["version"],
            is_current=bool(row["is_current"]),
        )
        self._flow_cache[cache_key] = flow
        return flow

    # ------------------------------------------------------------------
    # Internal: task reconstruction
    # ------------------------------------------------------------------

    def _row_to_task(
        self,
        row: sqlite3.Row,
        token_sums: dict[tuple[str, int], tuple[int, int, int]] | None = None,
        *,
        include_history: bool = True,
    ) -> Task:
        """Build a Task dataclass from a database row.

        ``token_sums`` is an optional pre-computed map keyed by
        ``(project, task_number)`` holding
        ``(total_input_tokens, total_output_tokens, session_count)`` so
        callers issuing batch reads (e.g. ``list_tasks``) can avoid the
        N+1 query hit. When ``None``, a single per-task aggregate query
        is issued against ``work_sessions`` (#86).
        """
        project = row["project"]
        task_number = row["task_number"]

        if include_history:
            transitions = self._load_transitions(project, task_number)
            executions = self._load_executions(project, task_number)
            rels = self._load_relationships(project, task_number)
            context_entries = self._load_context_entries(project, task_number)
        else:
            transitions = []
            executions = []
            rels = {}
            context_entries = []

        if token_sums is not None:
            tokens_in, tokens_out, sess_count = token_sums.get(
                (project, task_number), (0, 0, 0)
            )
        else:
            tokens_in, tokens_out, sess_count = self._load_task_token_sum(
                project, task_number
            )

        task = Task(
            project=project,
            task_number=task_number,
            title=row["title"],
            type=TaskType(row["type"]),
            labels=_safe_json_list(row["labels"]),
            work_status=WorkStatus(row["work_status"]),
            flow_template_id=row["flow_template_id"],
            flow_template_version=row["flow_template_version"],
            current_node_id=row["current_node_id"],
            assignee=row["assignee"],
            priority=Priority(row["priority"]),
            requires_human_review=bool(row["requires_human_review"]),
            description=row["description"],
            acceptance_criteria=row["acceptance_criteria"],
            constraints=row["constraints"],
            relevant_files=_safe_json_list(row["relevant_files"]),
            parent_project=row["parent_project"],
            parent_task_number=row["parent_task_number"],
            blocks=rels.get("blocks", []),
            blocked_by=rels.get("blocked_by", []),
            relates_to=rels.get("relates_to", []),
            children=rels.get("children", []),
            supersedes_project=row["supersedes_project"],
            supersedes_task_number=row["supersedes_task_number"],
            superseded_by_project=rels.get("superseded_by_project"),
            superseded_by_task_number=rels.get("superseded_by_task_number"),
            roles=_safe_json_dict(row["roles"]),
            external_refs=_safe_json_dict(row["external_refs"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            created_by=row["created_by"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
            transitions=transitions,
            executions=executions,
            context=context_entries,
            total_input_tokens=tokens_in,
            total_output_tokens=tokens_out,
            session_count=sess_count,
        )
        return task

    # ------------------------------------------------------------------
    # Per-task token aggregation (#86)
    # ------------------------------------------------------------------
    #
    # Token counts live on the ``work_sessions`` rows (populated by the
    # session teardown path — see #150). Per-task visibility is a SUM
    # over every session row bound to the task. The single-row helper is
    # cheap enough for ``get()`` / ``_row_to_task`` lookups; the batch
    # helper avoids N+1 in ``list_tasks`` when large result sets are
    # involved.

    def _load_task_token_sum(
        self, project: str, task_number: int
    ) -> tuple[int, int, int]:
        """Return ``(tokens_in, tokens_out, session_count)`` for one task."""
        row = self._conn.execute(
            "SELECT "
            "COALESCE(SUM(total_input_tokens), 0) AS tin, "
            "COALESCE(SUM(total_output_tokens), 0) AS tout, "
            "COUNT(*) AS cnt "
            "FROM work_sessions "
            "WHERE task_project = ? AND task_number = ?",
            (project, task_number),
        ).fetchone()
        if row is None:
            return (0, 0, 0)
        return (int(row["tin"] or 0), int(row["tout"] or 0), int(row["cnt"] or 0))

    def _load_task_token_sums_bulk(
        self,
        project: str | None = None,
    ) -> dict[tuple[str, int], tuple[int, int, int]]:
        """Return aggregated tokens for every task, optionally filtered.

        Keyed by ``(project, task_number)``. Returns zero-sum tuples
        only for tasks that have at least one session row — callers
        should default to ``(0, 0, 0)`` for misses.
        """
        clauses: list[str] = []
        params: list[object] = []
        if project is not None:
            clauses.append("task_project = ?")
            params.append(project)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            "SELECT task_project, task_number, "
            "COALESCE(SUM(total_input_tokens), 0) AS tin, "
            "COALESCE(SUM(total_output_tokens), 0) AS tout, "
            "COUNT(*) AS cnt "
            f"FROM work_sessions{where} "
            "GROUP BY task_project, task_number"
        )
        rows = self._conn.execute(sql, tuple(params)).fetchall()
        out: dict[tuple[str, int], tuple[int, int, int]] = {}
        for r in rows:
            key = (r["task_project"], int(r["task_number"]))
            out[key] = (
                int(r["tin"] or 0),
                int(r["tout"] or 0),
                int(r["cnt"] or 0),
            )
        return out

    def _load_relationships(
        self, project: str, task_number: int
    ) -> dict:
        """Load dependency relationships for a task from work_task_dependencies."""
        rows = self._conn.execute(
            "SELECT "
            "from_project, from_task_number, "
            "to_project, to_task_number, "
            "kind, "
            "1 AS is_outgoing, "
            "0 AS is_incoming "
            "FROM work_task_dependencies "
            "WHERE from_project = ? AND from_task_number = ? "
            "UNION ALL "
            "SELECT "
            "from_project, from_task_number, "
            "to_project, to_task_number, "
            "kind, "
            "0 AS is_outgoing, "
            "1 AS is_incoming "
            "FROM work_task_dependencies "
            "WHERE to_project = ? AND to_task_number = ?",
            (project, task_number, project, task_number),
        ).fetchall()

        rels: dict = {
            "blocks": [],
            "blocked_by": [],
            "relates_to": [],
            "children": [],
            "superseded_by_project": None,
            "superseded_by_task_number": None,
        }

        for r in rows:
            kind = r["kind"]
            if r["is_outgoing"]:
                target = (r["to_project"], r["to_task_number"])
                if kind == LinkKind.BLOCKS.value:
                    rels["blocks"].append(target)
                elif kind == LinkKind.RELATES_TO.value:
                    rels["relates_to"].append(target)
                elif kind == LinkKind.PARENT.value:
                    rels["children"].append(target)
                elif kind == LinkKind.SUPERSEDES.value:
                    # outgoing supersedes: this task supersedes target
                    pass  # stored in supersedes_project/supersedes_task_number columns
            if r["is_incoming"]:
                source = (r["from_project"], r["from_task_number"])
                if kind == LinkKind.BLOCKS.value:
                    rels["blocked_by"].append(source)
                elif kind == LinkKind.RELATES_TO.value:
                    # relates_to is bidirectional
                    if source not in rels["relates_to"]:
                        rels["relates_to"].append(source)
                elif kind == LinkKind.PARENT.value:
                    # incoming parent: source is parent of this task
                    # update parent fields (override column-based values)
                    pass  # parent is set via from_id=parent, to_id=child
                elif kind == LinkKind.SUPERSEDES.value:
                    # incoming supersedes: source supersedes this task
                    rels["superseded_by_project"] = r["from_project"]
                    rels["superseded_by_task_number"] = r["from_task_number"]

        return rels

    def _load_context_entries(
        self, project: str, task_number: int,
    ) -> list[ContextEntry]:
        """Attach work_context_entries rows to the hydrated task.

        Ordered oldest-first so the rendering layer can scan for the
        first matching entry_type (e.g. ``plain_summary``) without
        walking a reverse list.
        """
        rows = self._conn.execute(
            "SELECT actor, created_at, text, entry_type "
            "FROM work_context_entries "
            "WHERE task_project = ? AND task_number = ? ORDER BY id",
            (project, task_number),
        ).fetchall()
        entries: list[ContextEntry] = []
        for r in rows:
            try:
                etype = r["entry_type"] or "note"
            except (KeyError, IndexError):
                etype = "note"
            entries.append(
                ContextEntry(
                    actor=r["actor"],
                    timestamp=datetime.fromisoformat(r["created_at"]),
                    text=r["text"],
                    entry_type=etype,
                )
            )
        return entries

    def _load_transitions(self, project: str, task_number: int) -> list[Transition]:
        rows = self._conn.execute(
            "SELECT * FROM work_transitions "
            "WHERE task_project = ? AND task_number = ? ORDER BY id",
            (project, task_number),
        ).fetchall()
        return [
            Transition(
                from_state=r["from_state"],
                to_state=r["to_state"],
                actor=r["actor"],
                timestamp=datetime.fromisoformat(r["created_at"]),
                reason=r["reason"],
            )
            for r in rows
        ]

    def _load_executions(
        self, project: str, task_number: int
    ) -> list[FlowNodeExecution]:
        rows = self._conn.execute(
            "SELECT * FROM work_node_executions "
            "WHERE task_project = ? AND task_number = ? ORDER BY id",
            (project, task_number),
        ).fetchall()
        result: list[FlowNodeExecution] = []
        for r in rows:
            work_output = self._decode_work_output(r["work_output"])
            result.append(
                FlowNodeExecution(
                    task_id=f"{r['task_project']}/{r['task_number']}",
                    node_id=r["node_id"],
                    visit=r["visit"],
                    status=ExecutionStatus(r["status"]),
                    work_output=work_output,
                    decision=(
                        Decision(r["decision"]) if r["decision"] else None
                    ),
                    decision_reason=r["decision_reason"],
                    started_at=(
                        datetime.fromisoformat(r["started_at"])
                        if r["started_at"]
                        else None
                    ),
                    completed_at=(
                        datetime.fromisoformat(r["completed_at"])
                        if r["completed_at"]
                        else None
                    ),
                )
            )
        return result

    def _decode_work_output(self, raw: str | None) -> WorkOutput | None:
        if not raw:
            return None
        wo_dict = self._work_output_cache.get(raw)
        if wo_dict is None:
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError):
                return None
            if not isinstance(parsed, dict):
                return None
            wo_dict = parsed
            self._work_output_cache[raw] = wo_dict
        return WorkOutput(
            type=OutputType(wo_dict["type"]),
            summary=wo_dict["summary"],
            artifacts=[
                Artifact(
                    kind=ArtifactKind(a["kind"]),
                    description=a.get("description", ""),
                    ref=a.get("ref"),
                    path=a.get("path"),
                    external_ref=a.get("external_ref"),
                )
                for a in wo_dict.get("artifacts", [])
            ],
        )

    def _record_transition(
        self,
        project: str,
        task_number: int,
        from_state: str,
        to_state: str,
        actor: str,
        reason: str | None = None,
        *,
        allow_invariant_violation: bool = False,
    ) -> None:
        # #899 / #909 — every transition write runs through the
        # canonical invariant validator (#886). Behavior split:
        #
        # * known WorkStatus -> known WorkStatus + violation:
        #   raise :class:`InvariantViolationError` and DO NOT insert.
        #   The canonical TASK_TRANSITION_TABLE is the contract;
        #   bypassing it accumulates impossible task histories
        #   (the audit's #806 deleted-execution-history shape).
        # * unknown / legacy status on either side: log a warning
        #   and still write the row. Legacy databases predate the
        #   WorkStatus enum so the validator cannot reason about
        #   them; refusing the write would brick existing installs
        #   on first read after a migration. Migration writers can
        #   keep using this branch without ceremony.
        # * validator itself blew up: log via ``logger.exception``
        #   and proceed with the write — a broken validator must
        #   never block a transition that the caller already
        #   committed to.
        #
        # Escape hatch: ``allow_invariant_violation=True`` keeps the
        # legacy lenient behavior (warn-and-write) for known→known
        # violations. This is reserved for narrow admin / migration
        # paths (``pm task repair``-class callers) and must never
        # be set by normal task-action callsites. Callers that set
        # it are documenting "yes, I know this is outside the
        # canonical table; record it anyway because I am repairing
        # an existing broken row."
        try:
            from pollypm.task_invariants import validate_transition
            from pollypm.work.models import WorkStatus

            try:
                from_enum = WorkStatus(from_state)
                to_enum = WorkStatus(to_state)
            except ValueError:
                # One side is not a known WorkStatus — log and
                # proceed; legacy rows can carry custom states the
                # validator does not know about. Migration path
                # only — known→unknown is not enforced.
                logger.warning(
                    "work transition uses unknown status: %s/%s -> %s/%s "
                    "(actor=%s)",
                    project, task_number, from_state, to_state, actor,
                )
            else:
                violation = validate_transition(
                    task_id=f"{project}/{task_number}",
                    from_status=from_enum,
                    to_status=to_enum,
                )
                if violation is not None:
                    if allow_invariant_violation:
                        logger.warning(
                            "work transition violates canonical invariant "
                            "(allow_invariant_violation=True, recorded "
                            "anyway): %s",
                            violation.summary,
                        )
                    else:
                        # #909 — refuse the write. Raise BEFORE
                        # the INSERT so no row lands in
                        # work_transitions.
                        logger.warning(
                            "work transition violates canonical invariant: %s",
                            violation.summary,
                        )
                        raise InvariantViolationError(violation.summary)
        except InvariantViolationError:
            raise
        except Exception:  # noqa: BLE001 — validator must not break writes
            logger.exception(
                "work transition validation failed unexpectedly; "
                "proceeding with the underlying write (project=%s, "
                "task=%s, %s -> %s)",
                project, task_number, from_state, to_state,
            )

        self._conn.execute(
            "INSERT INTO work_transitions "
            "(task_project, task_number, from_state, to_state, actor, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (project, task_number, from_state, to_state, actor, reason, _now()),
        )
        # Audit hook (#savethenovel) — every transition lands here.
        # Mirrors the work_transitions row so post-mortem readers
        # can correlate the audit log with the DB even when one of
        # the two has been wiped.
        try:
            from pollypm.audit import emit as _audit_emit
            from pollypm.audit.log import EVENT_TASK_STATUS_CHANGED

            _audit_emit(
                event=EVENT_TASK_STATUS_CHANGED,
                project=project,
                subject=f"{project}/{task_number}",
                actor=actor or "",
                metadata={
                    "from": from_state,
                    "to": to_state,
                    "reason": reason,
                },
                project_path=self._project_path,
            )
        except Exception:  # noqa: BLE001 — audit must never break transitions
            pass

    @staticmethod
    def _gate_skip_reason(results: list[GateResult]) -> str | None:
        """Build a reason string from skipped gate failures."""
        failures = [r for r in results if not r.passed]
        if not failures:
            return None
        parts = [f"[skip-gates] {r.gate_name}: {r.reason}" for r in failures]
        return "; ".join(parts)

    def _gate_kwargs(self) -> dict[str, object]:
        """Build common kwargs for gate evaluation."""
        kwargs: dict[str, object] = {"get_task": self.get}
        if self._project_path is not None:
            kwargs["project_root"] = self._project_path
        return kwargs

    # ------------------------------------------------------------------
    # Owner derivation
    # ------------------------------------------------------------------

    def derive_owner(self, task: Task) -> str | None:
        """Derive the current owner from the flow node's actor configuration."""
        if task.current_node_id is None:
            if task.work_status == WorkStatus.DRAFT:
                return "project_manager"
            return None

        try:
            flow = self._load_flow_from_db(
                task.flow_template_id,
                task.flow_template_version,
            )
        except Exception:
            return task.assignee

        node = flow.nodes.get(task.current_node_id)
        if node is None:
            return task.assignee

        if node.actor_type == ActorType.ROLE:
            return task.roles.get(node.actor_role or "", task.assignee)
        elif node.actor_type == ActorType.HUMAN:
            return "human"
        elif node.actor_type == ActorType.PROJECT_MANAGER:
            return "project_manager"
        elif node.actor_type == ActorType.AGENT:
            # Return the specific named agent from the flow YAML. Fall back
            # to the assignee only if no agent_name was configured (which
            # validate_flow now rejects, but guard anyway for legacy DBs).
            return node.agent_name or task.assignee
        return task.assignee

    def _resolve_node_assignee(self, task: Task, node: FlowNode) -> str | None:
        """Resolve the assignee to store when transitioning into ``node``."""
        if node.actor_type == ActorType.ROLE:
            return task.roles.get(node.actor_role or "", task.assignee)
        if node.actor_type == ActorType.HUMAN:
            return "human"
        if node.actor_type == ActorType.PROJECT_MANAGER:
            return "project_manager"
        if node.actor_type == ActorType.AGENT:
            return node.agent_name or task.assignee
        return task.assignee

    # ------------------------------------------------------------------
    # Task CRUD
    # ------------------------------------------------------------------

    def create(
        self,
        *,
        title: str,
        description: str = "",
        type: str,
        project: str,
        flow_template: str,
        roles: dict[str, str],
        priority: str = "normal",
        created_by: str = "system",
        acceptance_criteria: str | None = None,
        constraints: str | None = None,
        relevant_files: list[str] | None = None,
        labels: list[str] | None = None,
        requires_human_review: bool = False,
    ) -> Task:
        """Create a task in draft state."""
        return create_task(
            self,
            title=title,
            description=description,
            type=type,
            project=project,
            flow_template=flow_template,
            roles=roles,
            priority=priority,
            created_by=created_by,
            acceptance_criteria=acceptance_criteria,
            constraints=constraints,
            relevant_files=relevant_files,
            labels=labels,
            requires_human_review=requires_human_review,
        )

    def has_human_review_approval(self, task_id: str) -> bool:
        """Return True when a pre-queue human review approval is recorded."""
        task = self.get(task_id)
        rows = self.get_context(task_id, entry_type="human_review_approved", limit=1)
        return bool(rows) or not task.requires_human_review

    def ensure_human_review_request_task(
        self,
        task_id: str,
        actor: str,
    ) -> Task:
        """Materialize a user-owned task requesting pre-queue approval."""
        target = self.get(task_id)
        label = f"target_task:{target.task_id}"
        for candidate in self.list_tasks(project=target.project):
            labels = set(candidate.labels or [])
            if (
                "human_review_request" in labels
                and label in labels
                and candidate.work_status not in TERMINAL_STATUSES
            ):
                return candidate

        description = "\n".join(
            [
                f"Review whether `{target.task_id}` should enter the worker queue.",
                "",
                f"Task: {target.title}",
                target.description or "(no description)",
                "",
                "Approve if this work is authorized to proceed. Reject or reply "
                "with clarification if it needs changes before delegation.",
            ]
        )
        return self.create(
            title=f"Human review required before queueing {target.task_id}",
            description=description,
            type="task",
            project=target.project,
            flow_template="chat",
            roles={"requester": "user", "operator": actor or "polly"},
            priority=target.priority.value,
            created_by=actor or "system",
            labels=[
                "human_review_request",
                f"project:{target.project}",
                label,
            ],
            requires_human_review=False,
        )

    def approve_human_review(
        self,
        task_id: str,
        actor: str,
        reason: str | None = None,
        *,
        fast_track_authorized: bool = False,
    ) -> Task:
        """Record pre-queue approval for a ``requires_human_review`` task."""
        task = self.get(task_id)
        actor_norm = (actor or "").strip().lower()
        is_user = actor_norm in {"user", "sam", "human"}
        if not is_user and not fast_track_authorized:
            raise InvalidTransitionError(
                "Only the user can approve this review unless the operator "
                "explicitly records --fast-track-authorized."
            )
        detail = reason.strip() if reason else "approved"
        if fast_track_authorized and not is_user:
            detail = f"fast-track authorized by {actor}: {detail}"
        self.add_context(
            task_id,
            actor or "user",
            detail,
            entry_type="human_review_approved",
        )

        label = f"target_task:{task.task_id}"
        for candidate in self.list_tasks(project=task.project):
            labels = set(candidate.labels or [])
            if (
                "human_review_request" in labels
                and label in labels
                and candidate.work_status not in TERMINAL_STATUSES
            ):
                try:
                    self.add_context(
                        candidate.task_id,
                        actor or "user",
                        f"approved target {task.task_id}",
                        entry_type="reply",
                    )
                    self.mark_done(candidate.task_id, actor or "user")
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "failed to close human review request %s",
                        candidate.task_id,
                        exc_info=True,
                    )
        return self.get(task_id)

    def set_external_ref(self, task_id: str, key: str, value: str) -> None:
        """Persist one external reference on a task."""
        if not key.strip():
            raise ValidationError(
                "Cannot persist an external ref with an empty key. "
                "The work service would have no stable name for the external "
                "system identifier, so later sync hooks could not retrieve it. "
                "Fix: pass a non-empty key such as 'github_issue'."
            )
        task = self.get(task_id)
        refs = dict(task.external_refs)
        refs[str(key)] = str(value)
        project, task_number = _parse_task_id(task_id)
        self._conn.execute(
            "UPDATE work_tasks SET external_refs = ?, updated_at = ? "
            "WHERE project = ? AND task_number = ?",
            (json.dumps(refs), _now(), project, task_number),
        )
        self._conn.commit()

    def get(self, task_id: str) -> Task:
        """Read a task by its ``project/number`` identifier."""
        return get_task(self, task_id)

    def list_tasks(
        self,
        *,
        work_status: str | None = None,
        owner: str | None = None,
        project: str | None = None,
        assignee: str | None = None,
        blocked: bool | None = None,
        type: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[Task]:
        """Query tasks with optional filters."""
        return read_tasks(
            self,
            work_status=work_status,
            owner=owner,
            project=project,
            assignee=assignee,
            blocked=blocked,
            type=type,
            limit=limit,
            offset=offset,
        )

    def list_nonterminal_tasks(
        self,
        *,
        project: str | None = None,
    ) -> list[Task]:
        """Return tasks that can still require inbox attention."""
        return read_nonterminal_tasks(self, project=project)

    def update(self, task_id: str, **fields: object) -> Task:
        """Update mutable fields on a task."""
        return update_task(self, task_id, **fields)

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def queue(self, task_id: str, actor: str, skip_gates: bool = False) -> Task:
        """Move from draft to queued."""
        return self._transition_mgr.queue(task_id, actor, skip_gates=skip_gates)

    def claim(self, task_id: str, actor: str, skip_gates: bool = False) -> Task:
        """Atomically claim a queued task."""
        return self._transition_mgr.claim(task_id, actor, skip_gates=skip_gates)

    def cancel(self, task_id: str, actor: str, reason: str) -> Task:
        """Move any non-terminal task to cancelled."""
        return self._transition_mgr.cancel(task_id, actor, reason)

    def hold(self, task_id: str, actor: str, reason: str | None = None) -> Task:
        """Move in_progress or queued to on_hold."""
        return self._transition_mgr.hold(task_id, actor, reason)

    def resume(self, task_id: str, actor: str) -> Task:
        """Move on_hold back to queued (or in_progress if a flow node is active)."""
        return self._transition_mgr.resume(task_id, actor)

    def release_stale_claim(
        self,
        task_id: str,
        actor: str,
        reason: str = "worker session missing",
    ) -> Task:
        """Return an orphaned active worker claim to the queued pool.

        Recovery plugins call this only after proving the worker
        session/window is gone. The work service owns the private SQLite
        mutation so plugins do not reach into ``_conn`` directly.

        Preserves the task's flow position (#806). Earlier behaviour
        deleted every execution row for the active node and cleared
        ``current_node_id``, so the next claim restarted from
        ``flow.start_node`` and lost rejection history, prior review
        artifacts, and visit counts. Now the active execution row is
        marked ``ABANDONED`` and ``current_node_id`` is left in place,
        so the next claim resumes the same node and ``next_visit``
        keeps incrementing past the abandoned attempt.
        """
        task = self.get(task_id)
        if task.work_status not in (WorkStatus.IN_PROGRESS, WorkStatus.REWORK):
            raise InvalidTransitionError(
                f"Cannot release stale claim in '{task.work_status.value}' state. "
                "Task must be in 'in_progress' or 'rework' state."
            )
        now = _now()
        try:
            if task.current_node_id:
                # Mark only the live ACTIVE execution row as abandoned
                # — completed prior visits stay intact so the timeline
                # still reads "v1 rejected → v2 (abandoned by sweep)".
                self._conn.execute(
                    "UPDATE work_node_executions "
                    "SET status = ?, completed_at = COALESCE(completed_at, ?) "
                    "WHERE task_project = ? "
                    "AND task_number = ? "
                    "AND node_id = ? "
                    "AND status = ?",
                    (
                        ExecutionStatus.ABANDONED.value,
                        now,
                        task.project,
                        task.task_number,
                        task.current_node_id,
                        ExecutionStatus.ACTIVE.value,
                    ),
                )
            self._record_transition(
                task.project,
                task.task_number,
                task.work_status.value,
                WorkStatus.QUEUED.value,
                actor,
                reason,
            )
            self._conn.execute(
                "UPDATE work_tasks SET work_status = ?, assignee = NULL, "
                "updated_at = ? "
                "WHERE project = ? AND task_number = ?",
                (WorkStatus.QUEUED.value, now, task.project, task.task_number),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        result = self.get(task_id)
        self._sync_transition(
            result,
            task.work_status.value,
            WorkStatus.QUEUED.value,
        )
        return result

    # ------------------------------------------------------------------
    # Flow progression
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_work_output(output: WorkOutput) -> None:
        """Validate a WorkOutput has required fields and at least one artifact."""
        if not isinstance(output.type, OutputType):
            try:
                OutputType(output.type)
            except (ValueError, KeyError):
                raise ValidationError(
                    f"Invalid output type '{output.type}'."
                )
        if not output.summary or not output.summary.strip():
            raise ValidationError(
                "Work output has an empty summary.\n"
                "\n"
                "Why: the reviewer needs a one-paragraph explanation of "
                "what you built.\n"
                "\n"
                "Fix: include a non-empty \"summary\" in your --output "
                "JSON, for example:\n"
                "    pm task done <id> --output '{\n"
                "      \"type\": \"code_change\",\n"
                "      \"summary\": \"Implemented X; all tests green.\",\n"
                "      \"artifacts\": [{\"kind\": \"commit\", \"description\": "
                "\"impl\", \"ref\": \"HEAD\"}]\n"
                "    }'"
            )
        if not output.artifacts:
            raise ValidationError(
                "Work output must have at least one artifact.\n"
                "\n"
                "Why: the reviewer needs concrete evidence of what you "
                "built — a commit SHA, a changed file, or a recorded "
                "action — before a task can advance to review.\n"
                "\n"
                "Fix: include an \"artifacts\" array in your --output "
                "JSON. Common shapes:\n"
                "    commit:      {\"kind\": \"commit\", \"description\": "
                "\"impl\", \"ref\": \"HEAD\"}\n"
                "    file change: {\"kind\": \"file_change\", \"description\": "
                "\"docs\", \"path\": \"README.md\"}\n"
                "    note:        {\"kind\": \"note\", \"description\": "
                "\"investigated X; no code change needed\"}\n"
                "\n"
                "Full example:\n"
                "    pm task done <id> --output '{\n"
                "      \"type\": \"code_change\",\n"
                "      \"summary\": \"...\",\n"
                "      \"artifacts\": [{\"kind\": \"commit\", \"description\": "
                "\"impl\", \"ref\": \"HEAD\"}]\n"
                "    }'"
            )
        for i, art in enumerate(output.artifacts):
            if not isinstance(art.kind, ArtifactKind):
                try:
                    ArtifactKind(art.kind)
                except (ValueError, KeyError):
                    raise ValidationError(
                        f"Artifact {i}: invalid kind '{art.kind}'. "
                        f"Expected one of: commit, file_change, action, note. "
                        f"Fix: change \"kind\" in your --output JSON to one "
                        f"of the four supported values."
                    )
            if not (art.description or art.ref or art.path):
                raise ValidationError(
                    f"Artifact {i}: must have at least one of "
                    f"description, ref, or path. "
                    f"Fix: add a \"description\" field, or a \"ref\" (SHA) "
                    f"for commits, or a \"path\" for file changes."
                )

    @staticmethod
    def _coerce_work_output(
        work_output: WorkOutput | dict | None,
    ) -> WorkOutput | None:
        """Convert a dict to WorkOutput if needed."""
        if work_output is None:
            return None
        if isinstance(work_output, dict):
            artifacts = [
                Artifact(
                    kind=(
                        ArtifactKind(a["kind"])
                        if isinstance(a.get("kind"), str)
                        else a.get("kind", ArtifactKind.NOTE)
                    ),
                    description=a.get("description", ""),
                    ref=a.get("ref"),
                    path=a.get("path"),
                    external_ref=a.get("external_ref"),
                )
                for a in work_output.get("artifacts", [])
            ]
            out_type = work_output.get("type", OutputType.CODE_CHANGE)
            if isinstance(out_type, str):
                out_type = OutputType(out_type)
            return WorkOutput(
                type=out_type,
                summary=work_output.get("summary", ""),
                artifacts=artifacts,
            )
        return work_output

    @staticmethod
    def _serialize_work_output(output: WorkOutput) -> str:
        """Serialize a WorkOutput to a JSON string for DB storage."""
        return json.dumps(
            {
                "type": (
                    output.type.value
                    if isinstance(output.type, OutputType)
                    else output.type
                ),
                "summary": output.summary,
                "artifacts": [
                    {
                        "kind": (
                            a.kind.value
                            if isinstance(a.kind, ArtifactKind)
                            else a.kind
                        ),
                        "description": a.description,
                        "ref": a.ref,
                        "path": a.path,
                        "external_ref": a.external_ref,
                    }
                    for a in output.artifacts
                ],
            }
        )

    def _get_current_flow_node(
        self, task: Task
    ) -> tuple[FlowTemplate, FlowNode]:
        """Load the flow and return the current node."""
        flow = self._load_flow_from_db(
            task.flow_template_id, task.flow_template_version,
        )
        if task.current_node_id is None:
            raise InvalidTransitionError("Task has no current flow node.")
        node = flow.nodes.get(task.current_node_id)
        if node is None:
            raise InvalidTransitionError(
                f"Current node '{task.current_node_id}' not found in flow "
                f"'{task.flow_template_id}'."
            )
        return flow, node

    # Actors that are always treated as human for actor_type=HUMAN nodes.
    _HUMAN_ACTOR_NAMES = frozenset({"human", "user", "sam"})

    @staticmethod
    def _ordered_names(*names: str | None) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for name in names:
            if not name or name in seen:
                continue
            ordered.append(name)
            seen.add(name)
        return ordered

    def _validate_actor_role(
        self, task: Task, node: FlowNode, actor: str
    ) -> None:
        """Validate that actor matches the node's expected role."""
        if node.actor_type == ActorType.HUMAN:
            # Accept well-known human names plus the assigned reviewer role value
            reviewer = None
            if node.actor_role:
                reviewer = task.roles.get(node.actor_role)
            allowed = self._ordered_names(
                reviewer, *sorted(self._HUMAN_ACTOR_NAMES),
            )
            if actor not in allowed:
                schema = "actor_type='human'"
                if node.actor_role:
                    schema += f", actor_role='{node.actor_role}'"
                    if reviewer:
                        schema += (
                            f", task.roles['{node.actor_role}']='{reviewer}'"
                        )
                raise ValidationError(
                    f"Node '{node.name}' requires human review ({schema}). "
                    f"Actor '{actor}' is not authorized. "
                    f"Accepted actors: {', '.join(repr(name) for name in allowed)}. "
                    f"Fix: rerun this action with --actor {allowed[0]}."
                )
        elif node.actor_type == ActorType.ROLE and node.actor_role:
            expected_actor = task.roles.get(node.actor_role)
            if expected_actor and actor != expected_actor:
                # Also accept the role name itself (e.g. "worker" matches role "worker")
                if actor != node.actor_role:
                    raise ValidationError(
                        f"Actor '{actor}' does not match role "
                        f"'{node.actor_role}' (expected '{expected_actor}'). "
                        f"Node '{node.name}' uses actor_type='role', "
                        f"actor_role='{node.actor_role}', "
                        f"task.roles['{node.actor_role}']='{expected_actor}'. "
                        f"Accepted actors: '{expected_actor}' or literal role "
                        f"name '{node.actor_role}'. "
                        f"Fix: rerun this action with --actor {expected_actor}."
                    )
            elif expected_actor is None and actor != node.actor_role:
                raise ValidationError(
                    f"Actor '{actor}' does not match role '{node.actor_role}'. "
                    f"Node '{node.name}' uses actor_type='role', "
                    f"actor_role='{node.actor_role}', but this task has no "
                    f"binding in task.roles['{node.actor_role}']. "
                    f"Accepted actor: '{node.actor_role}'. "
                    f"Fix: rerun this action with --actor {node.actor_role}, "
                    f"or update the role binding."
                )
        elif node.actor_type == ActorType.AGENT and node.agent_name:
            if actor != node.agent_name:
                raise ValidationError(
                    f"Node '{node.name}' is pinned to agent "
                    f"'{node.agent_name}' (actor_type='agent', "
                    f"agent_name='{node.agent_name}'). Actor '{actor}' is not "
                    f"authorized. Fix: rerun this action with --actor "
                    f"{node.agent_name}."
                )

    def _next_visit(
        self, project: str, task_number: int, node_id: str
    ) -> int:
        """Return the next visit number for a node execution."""
        return read_next_visit(self, project, task_number, node_id)

    def current_node_visit(
        self, project: str, task_number: int, node_id: str
    ) -> int:
        """Return the visit number of the current execution at ``node_id``.

        Used by the task-assignment notifier (#279) to key its dedupe on
        ``(session, task, execution_version)`` so a reject-bounce back to
        an earlier node unlocks the retry ping instead of being
        suppressed by the 30-minute window that originally pinged the
        worker at ``visit=1``.

        Returns ``0`` when the task has no recorded execution for the
        node yet — a queued task whose start node hasn't been entered
        carries an implicit "zeroth visit", and downstream dedupe
        treats that as one identity bucket (matching the pre-#279
        column default).
        """
        return read_current_node_visit(self, project, task_number, node_id)

    def kickoff_sent_at(
        self,
        project: str,
        task_number: int,
        node_id: str,
        visit: int | None = None,
    ) -> str | None:
        """Return the ISO timestamp the kickoff ping was delivered, if any.

        ``visit`` defaults to the current (latest) execution visit so the
        sweep can ask "has the kickoff for the active execution landed
        yet?" without wiring the visit counter through every call site.
        Returns ``None`` when no row exists or the row's ``kickoff_sent_at``
        is unstamped — both cases are "delivery still required" for the
        sweep's force-kickoff path (#922).
        """
        if visit is None:
            visit = self.current_node_visit(project, task_number, node_id)
        # Visit 0 is the implicit zeroth visit for queued tasks that
        # haven't been claimed yet — no execution row exists, so the
        # kickoff hasn't been delivered.
        if not visit:
            return None
        row = self._conn.execute(
            "SELECT kickoff_sent_at FROM work_node_executions "
            "WHERE task_project = ? AND task_number = ? "
            "AND node_id = ? AND visit = ?",
            (project, task_number, node_id, visit),
        ).fetchone()
        if row is None:
            return None
        return row["kickoff_sent_at"]

    def mark_kickoff_sent(
        self,
        project: str,
        task_number: int,
        node_id: str,
        visit: int | None = None,
    ) -> None:
        """Stamp the active execution row with the kickoff delivery time.

        Called by the task-assignment notifier (#922) after a worker
        kickoff ping lands successfully. The stamp is per-execution-visit
        so a reject-bounce (which opens a fresh ``work_node_executions``
        row at the next visit) gets a NULL ``kickoff_sent_at`` and the
        sweep re-delivers the kickoff for the resumed work.

        Best-effort: silent no-op when no execution row exists yet (e.g.
        a queued task whose start node hasn't been entered).
        """
        if visit is None:
            visit = self.current_node_visit(project, task_number, node_id)
        if not visit:
            return
        try:
            self._conn.execute(
                "UPDATE work_node_executions SET kickoff_sent_at = ? "
                "WHERE task_project = ? AND task_number = ? "
                "AND node_id = ? AND visit = ?",
                (_now(), project, task_number, node_id, visit),
            )
            self._conn.commit()
        except Exception:  # noqa: BLE001
            logger.debug(
                "mark_kickoff_sent failed for %s/%d node=%s visit=%d",
                project, task_number, node_id, visit,
                exc_info=True,
            )

    def _advance_to_node(
        self,
        task: Task,
        flow: FlowTemplate,
        next_node_id: str | None,
        actor: str,
        from_status: WorkStatus,
    ) -> None:
        """Advance the task to the next node, updating status and execution."""
        advance_to_node(self, task, flow, next_node_id, actor, from_status)

    def node_done(
        self,
        task_id: str,
        actor: str,
        work_output: WorkOutput | dict | None = None,
        skip_gates: bool = False,
    ) -> Task:
        """Signal that the current work node is complete."""
        return self._transition_mgr.node_done(
            task_id,
            actor,
            work_output=work_output,
            skip_gates=skip_gates,
        )

    def approve(
        self,
        task_id: str,
        actor: str,
        reason: str | None = None,
        skip_gates: bool = False,
        resume_merge: bool = False,
    ) -> Task:
        """Approve at a review node.

        ``resume_merge=True`` lets a caller continue an approve after
        hand-resolving a non-safelist merge conflict surfaced by a previous
        attempt; see ``_auto_merge_approved_task_branch``.
        """
        self.last_first_shipped_created = False
        result = self._transition_mgr.approve(
            task_id,
            actor,
            reason=reason,
            skip_gates=skip_gates,
            resume_merge=resume_merge,
        )
        if result.work_status == WorkStatus.DONE:
            self.last_first_shipped_created = maybe_record_first_shipped(
                self,
                task_id,
                project_path=self._project_path,
            )
        return result

    def reject(
        self,
        task_id: str,
        actor: str,
        reason: str,
    ) -> Task:
        """Reject at a review node."""
        return self._transition_mgr.reject(task_id, actor, reason)

    def block(self, task_id: str, actor: str, blocker_task_id: str) -> Task:
        """Mark a task as blocked by another task."""
        return self._transition_mgr.block(task_id, actor, blocker_task_id)

    def get_execution(
        self,
        task_id: str,
        node_id: str | None = None,
        visit: int | None = None,
    ) -> list[FlowNodeExecution]:
        """Read execution records for a task with optional filters."""
        project, task_number = _parse_task_id(task_id)

        clauses = ["task_project = ?", "task_number = ?"]
        params: list[object] = [project, task_number]

        if node_id is not None:
            clauses.append("node_id = ?")
            params.append(node_id)
        if visit is not None:
            clauses.append("visit = ?")
            params.append(visit)

        where = " AND ".join(clauses)
        rows = self._conn.execute(
            f"SELECT * FROM work_node_executions "
            f"WHERE {where} ORDER BY id",
            params,
        ).fetchall()

        result: list[FlowNodeExecution] = []
        for r in rows:
            wo = self._decode_work_output(r["work_output"])
            result.append(
                FlowNodeExecution(
                    task_id=f"{r['task_project']}/{r['task_number']}",
                    node_id=r["node_id"],
                    visit=r["visit"],
                    status=ExecutionStatus(r["status"]),
                    work_output=wo,
                    decision=(
                        Decision(r["decision"])
                        if r["decision"]
                        else None
                    ),
                    decision_reason=r["decision_reason"],
                    started_at=(
                        datetime.fromisoformat(r["started_at"])
                        if r["started_at"]
                        else None
                    ),
                    completed_at=(
                        datetime.fromisoformat(r["completed_at"])
                        if r["completed_at"]
                        else None
                    ),
                )
            )
        return result

    # ------------------------------------------------------------------
    # Gate validation (dry-run)
    # ------------------------------------------------------------------

    def validate_advance(self, task_id: str, actor: str) -> list[GateResult]:
        """Dry-run: would advancing the current node succeed for this actor?

        Evaluates all gates listed on the current flow node, plus an
        actor-vs-role check matching what the real transition methods do.
        Returns the combined results without modifying any state.
        """
        task = self.get(task_id)
        if task.current_node_id is None:
            return []

        try:
            flow, node = self._get_current_flow_node(task)
        except InvalidTransitionError:
            return []

        results: list[GateResult] = []

        # Actor-vs-role check: synthesised as a hard gate result so callers
        # using validate_advance for permission preflight get a correct answer.
        try:
            self._validate_actor_role(task, node, actor)
        except Exception as exc:  # noqa: BLE001
            results.append(
                GateResult(
                    passed=False,
                    reason=str(exc),
                    gate_name="actor_role",
                    gate_type="hard",
                )
            )

        if node.gates:
            results.extend(
                evaluate_gates(
                    task, node.gates, self._gate_registry,
                    **self._gate_kwargs(),
                )
            )

        return results

    # ------------------------------------------------------------------
    # Dependencies
    # ------------------------------------------------------------------

    def link(self, from_id: str, to_id: str, kind: str) -> None:
        """Create a relationship between two tasks.

        ``kind`` must be one of: blocks, relates_to, supersedes, parent.
        For ``blocks``, cycle detection is performed before committing.
        """
        self._dependency_mgr.link(from_id, to_id, kind)

    def unlink(self, from_id: str, to_id: str, kind: str) -> None:
        """Remove a relationship between two tasks."""
        self._dependency_mgr.unlink(from_id, to_id, kind)

    def dependents(self, task_id: str) -> list[Task]:
        """Return all tasks blocked by this task, transitively.

        Follows ``blocks`` edges from task_id outward via BFS.
        """
        return self._dependency_mgr.dependents(task_id)

    # ------------------------------------------------------------------
    # Dependency helpers
    # ------------------------------------------------------------------

    def _would_create_cycle(
        self,
        from_project: str,
        from_number: int,
        to_project: str,
        to_number: int,
    ) -> bool:
        """DFS from to_id following blocks edges; returns True if from_id is reachable."""
        return self._dependency_mgr.would_create_cycle(
            from_project,
            from_number,
            to_project,
            to_number,
        )

    def _has_unresolved_blockers(self, task_id: str) -> bool:
        """Check if a task has any blockers that are not done."""
        return self._dependency_mgr.has_unresolved_blockers(task_id)

    def _maybe_block(self, task_id: str) -> None:
        """If task is queued or in_progress and has unresolved blockers, block it."""
        self._dependency_mgr.maybe_block(task_id)

    def _maybe_unblock(self, task_id: str) -> None:
        """If task is blocked and has no remaining unresolved blockers, unblock it."""
        self._dependency_mgr.maybe_unblock(task_id)

    def _check_auto_unblock(self, task_id: str) -> None:
        """After a task moves to done, auto-unblock any tasks it was blocking."""
        self._dependency_mgr.check_auto_unblock(task_id)

    def _on_cancelled(self, task_id: str) -> None:
        """After a task is cancelled, add context entries on blocked dependents."""
        self._dependency_mgr.on_cancelled(task_id)

    def _has_incoming_parent_link(self, task: Task) -> bool:
        """Return True when ``task`` is linked as a child of another task."""
        row = self._conn.execute(
            "SELECT 1 FROM work_task_dependencies "
            "WHERE to_project = ? AND to_task_number = ? AND kind = ? "
            "LIMIT 1",
            (
                task.project,
                task.task_number,
                LinkKind.PARENT.value,
            ),
        ).fetchone()
        return row is not None

    def _prune_cancelled_critique_child(self, task: Task) -> None:
        """Delete cancelled planner critic subtasks after side effects land.

        Project-planning critic reviews are short-lived helper tasks that
        exist only to fan work out to critic sessions during the planner's
        stage-5 panel. Keeping their cancelled rows around pollutes
        ``pm task list`` and shifts the visible numbering of the first real
        implementation task. Only prune ``critique_flow`` tasks that are
        linked as a parent/child subtask so standalone uses of the flow keep
        normal cancelled-task semantics.
        """
        if task.flow_template_id != "critique_flow":
            return
        if not self._has_incoming_parent_link(task):
            return

        params = (task.project, task.task_number)
        dep_params = (
            task.project,
            task.task_number,
            task.project,
            task.task_number,
        )
        try:
            self._conn.execute(
                "DELETE FROM work_sync_state "
                "WHERE task_project = ? AND task_number = ?",
                params,
            )
            self._conn.execute(
                "DELETE FROM work_sessions "
                "WHERE task_project = ? AND task_number = ?",
                params,
            )
            self._conn.execute(
                "DELETE FROM work_context_entries "
                "WHERE task_project = ? AND task_number = ?",
                params,
            )
            self._conn.execute(
                "DELETE FROM work_transitions "
                "WHERE task_project = ? AND task_number = ?",
                params,
            )
            self._conn.execute(
                "DELETE FROM work_node_executions "
                "WHERE task_project = ? AND task_number = ?",
                params,
            )
            self._conn.execute(
                "DELETE FROM work_task_dependencies "
                "WHERE (from_project = ? AND from_task_number = ?) "
                "OR (to_project = ? AND to_task_number = ?)",
                dep_params,
            )
            self._conn.execute(
                "DELETE FROM work_tasks "
                "WHERE project = ? AND task_number = ?",
                params,
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        # Audit hook (#savethenovel) — record the row delete.
        # work_tasks is the spine; once a row leaves it the only
        # forensic trail of "this task ever existed" is the audit
        # log. Emit AFTER commit so we don't record phantom
        # deletes for rolled-back transactions.
        try:
            from pollypm.audit import emit as _audit_emit
            from pollypm.audit.log import EVENT_TASK_DELETED

            _audit_emit(
                event=EVENT_TASK_DELETED,
                project=task.project,
                subject=task.task_id,
                actor="system",
                metadata={
                    "reason": "prune_cancelled_critique_child",
                    "flow_template": task.flow_template_id,
                    "title": task.title,
                },
                project_path=self._project_path,
            )
        except Exception:  # noqa: BLE001
            pass

    def _on_task_done(self, task_id: str, actor: str) -> None:
        """Post-commit hook — fire milestone/digest flush on done transitions.

        Best-effort: failures are logged and swallowed so a broken
        notification side-channel never blocks the primary transition.
        Runs after ``_check_auto_unblock`` and ``teardown_worker`` so
        the ordering matches the rest of the completion pipeline.
        """
        on_task_done(self, task_id, actor)

    def _on_task_transition(
        self,
        task_id: str,
        from_state: str,
        to_state: str,
        actor: str,
    ) -> None:
        """Post-commit hook for any state transition.

        Currently routes regression detection (away from ``done``). Safe
        on every transition; cheap when not applicable.
        """
        on_task_transition(self, task_id, from_state, to_state, actor)

    def mark_done(self, task_id: str, actor: str) -> Task:
        """Move a task to done and trigger auto-unblock on dependents.

        This is a helper for completing tasks. Full flow-based completion
        (approve/node_done) will call ``_check_auto_unblock`` as well.
        """
        return self._transition_mgr.mark_done(task_id, actor)

    # ------------------------------------------------------------------
    # Context log
    # ------------------------------------------------------------------

    def add_context(
        self,
        task_id: str,
        actor: str,
        text: str,
        *,
        entry_type: str = "note",
    ) -> ContextEntry:
        """Append a context entry to a task's log.

        ``entry_type`` classifies the row. ``"note"`` is the default (generic
        context log, mirrors prior behaviour). Inbox callers use ``"reply"``
        or ``"read"`` via :meth:`add_reply` and :meth:`mark_read` helpers.
        """
        project, number = _parse_task_id(task_id)
        # Validate task exists
        row = self._conn.execute(
            "SELECT 1 FROM work_tasks WHERE project = ? AND task_number = ?",
            (project, number),
        ).fetchone()
        if row is None:
            raise TaskNotFoundError(f"Task '{task_id}' not found.")

        now = _now()
        self._conn.execute(
            "INSERT INTO work_context_entries "
            "(task_project, task_number, actor, text, created_at, entry_type) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (project, number, actor, text, now, entry_type),
        )
        self._conn.commit()
        return ContextEntry(
            actor=actor,
            timestamp=datetime.fromisoformat(now),
            text=text,
            entry_type=entry_type,
        )

    def stage_notification(
        self,
        *,
        project: str,
        subject: str,
        body: str,
        actor: str,
        priority: str,
        milestone_key: str | None,
        payload: dict[str, object] | None = None,
    ) -> int:
        """Public typed API for digest/silent notification staging."""
        return stage_notification_row(
            self,
            project=project,
            subject=subject,
            body=body,
            actor=actor,
            priority=priority,
            milestone_key=milestone_key,
            payload=payload,
        )

    def list_digest_rollup_candidates(
        self,
        *,
        project: str,
        milestone_key: str | None,
    ) -> list[DigestRollupCandidate]:
        return list_digest_rollup_candidates(
            self,
            project=project,
            milestone_key=milestone_key,
        )

    def mark_rollup_candidates_flushed(
        self,
        candidates: list[DigestRollupCandidate],
        *,
        rollup_task_id: str,
        flushed_at: str,
    ) -> None:
        mark_rollup_candidates_flushed(
            self,
            candidates,
            rollup_task_id=rollup_task_id,
            flushed_at=flushed_at,
        )

    def has_old_pending_digest_rows(
        self,
        *,
        project: str,
        milestone_key: str | None,
        min_age_seconds: int,
    ) -> bool:
        return has_old_pending_digest_rows(
            self,
            project=project,
            milestone_key=milestone_key,
            min_age_seconds=min_age_seconds,
        )

    def find_flushed_rollup_milestone(self, *, task_id: str) -> str | None:
        return find_flushed_rollup_milestone(self, task_id=task_id)

    def prune_staged_notifications(self, *, retain_days: int = 30) -> dict[str, int]:
        return prune_staged_notifications(self, retain_days=retain_days)

    def get_context(
        self,
        task_id: str,
        limit: int | None = None,
        since: datetime | None = None,
        entry_type: str | None = None,
    ) -> list[ContextEntry]:
        """Query context entries for a task, most recent first.

        When ``entry_type`` is given, only rows matching that tag are
        returned — pass ``"reply"`` for the inbox thread view,
        ``"read"`` for read markers, or ``None`` for every row.
        """
        project, number = _parse_task_id(task_id)
        clauses = ["task_project = ?", "task_number = ?"]
        params: list[object] = [project, number]

        if since is not None:
            clauses.append("created_at > ?")
            params.append(since.isoformat())

        if entry_type is not None:
            clauses.append("entry_type = ?")
            params.append(entry_type)

        where = " AND ".join(clauses)
        sql = f"SELECT * FROM work_context_entries WHERE {where} ORDER BY id DESC"

        if limit is not None:
            sql += f" LIMIT {int(limit)}"

        rows = self._conn.execute(sql, params).fetchall()
        entries = []
        for r in rows:
            # entry_type may be absent on rows written before migration 3
            # on a DB that was still being upgraded; coerce defensively.
            try:
                etype = r["entry_type"] or "note"
            except (KeyError, IndexError):
                etype = "note"
            entries.append(
                ContextEntry(
                    actor=r["actor"],
                    timestamp=datetime.fromisoformat(r["created_at"]),
                    text=r["text"],
                    entry_type=etype,
                )
            )
        return entries

    # ------------------------------------------------------------------
    # Inbox actions — reply / archive / read-marker
    #
    # These three methods are the work-service backing for the cockpit's
    # interactive inbox screen. Each wraps a primitive (add_context,
    # mark_done) with the idempotency + event-emission shape the UI
    # expects, so the Textual layer stays focused on interaction and
    # doesn't reinvent state management.
    # ------------------------------------------------------------------

    def add_reply(
        self, task_id: str, body: str, actor: str = "user",
    ) -> ContextEntry:
        """Record a user reply on an inbox task.

        Stored as a ``work_context_entries`` row with ``entry_type='reply'``
        so :meth:`list_replies` and the inbox thread view can render chat
        turns without collision with system/notes context.

        Raises :class:`ValidationError` when ``body`` is empty after strip.
        """
        if not body or not body.strip():
            raise ValidationError("Reply body must not be empty.")
        return self.add_context(
            task_id, actor, body.strip(), entry_type="reply",
        )

    def archive_task(self, task_id: str, actor: str = "user") -> Task:
        """Flip an inbox task to the chat-flow terminal state.

        Idempotent: archiving an already-terminal task is a no-op and
        returns the current record unchanged. Uses the same underlying
        transition shape as :meth:`mark_done` so dashboard counts and
        dependency unblocking stay consistent.
        """
        task = self.get(task_id)
        if task.work_status in TERMINAL_STATUSES:
            return task
        now = _now()
        self._record_transition(
            task.project,
            task.task_number,
            task.work_status.value,
            WorkStatus.DONE.value,
            actor,
            reason="inbox.archive",
        )
        self._conn.execute(
            "UPDATE work_tasks SET work_status = ?, updated_at = ? "
            "WHERE project = ? AND task_number = ?",
            (WorkStatus.DONE.value, now, task.project, task.task_number),
        )
        self._conn.commit()
        # Cascade: any dependents blocked on this task should unblock,
        # same as mark_done. archive_task is effectively 'done' for the
        # chat-flow, so we respect the same contract.
        try:
            self._check_auto_unblock(task_id)
        except Exception:  # noqa: BLE001
            # Unblock cascading is best-effort — never let it break archive.
            logger.debug("auto_unblock after archive failed", exc_info=True)
        # #1013 — close any open ``notify`` whose payload references
        # this task. The architect's plan_review handoff (and other
        # ``pm notify --priority immediate`` rows) carry
        # ``payload.task_id`` pointing at the chat-flow task they
        # materialised; once the task is archived the announcement
        # is stale and shouldn't keep appearing in ``pm inbox``.
        try:
            self._sweep_related_notifies(task_id)
        except Exception:  # noqa: BLE001
            logger.debug(
                "related-notify sweep after archive failed", exc_info=True,
            )
        return self.get(task_id)

    def _sweep_related_notifies(self, task_id: str) -> None:
        """Close any open notify whose ``payload.task_id`` matches.

        Best-effort — the unified messages store may live in a different
        SQLite file than this work-service connection, so we open it
        on the project's state.db (same path the work-service uses) and
        let the sweep helper drive ``query_messages`` / ``close_message``.
        """
        if not getattr(self, "_db_path", None):
            return
        try:
            from pollypm.store import SQLAlchemyStore
        except Exception:  # noqa: BLE001
            return
        from pollypm.inbox_sweep import sweep_notifies_for_done_task

        store = SQLAlchemyStore(f"sqlite:///{self._db_path}")
        try:
            sweep_notifies_for_done_task(store, task_id)
        finally:
            store.close()

    def mark_read(self, task_id: str, actor: str = "user") -> bool:
        """Record a read-marker on an inbox task if one isn't already present.

        Returns ``True`` when a new marker row was written, ``False`` when
        a read row already existed (idempotent repeat-open). Callers use
        the return value to gate event emission so the activity feed only
        sees the *first* open.
        """
        project, number = _parse_task_id(task_id)
        row = self._conn.execute(
            "SELECT 1 FROM work_tasks WHERE project = ? AND task_number = ?",
            (project, number),
        ).fetchone()
        if row is None:
            raise TaskNotFoundError(f"Task '{task_id}' not found.")
        existing = self._conn.execute(
            "SELECT 1 FROM work_context_entries "
            "WHERE task_project = ? AND task_number = ? AND entry_type = 'read' "
            "LIMIT 1",
            (project, number),
        ).fetchone()
        if existing is not None:
            return False
        now = _now()
        self._conn.execute(
            "INSERT INTO work_context_entries "
            "(task_project, task_number, actor, text, created_at, entry_type) "
            "VALUES (?, ?, ?, ?, ?, 'read')",
            (project, number, actor, "opened in cockpit inbox", now),
        )
        self._conn.commit()
        return True

    def list_replies(self, task_id: str) -> list[ContextEntry]:
        """Return reply entries for a task in chronological (oldest first) order.

        Thin wrapper over :meth:`get_context` so callers don't need to
        remember the ``entry_type='reply'`` convention, and so the inbox
        view can render the thread in natural reading order without the
        reverse() gymnastics the general context log requires.
        """
        entries = self.get_context(task_id, entry_type="reply")
        entries.reverse()  # get_context returns newest-first
        return entries

    # ------------------------------------------------------------------
    # Bulk context queries — N+1 escape hatch for the inbox loader.
    #
    # The cockpit inbox loader used to call ``get_context`` and
    # ``list_replies`` once per inbox task. At ~9 projects × ~10 inbox
    # tasks each, that was ~180 separate SQLite roundtrips on every 8s
    # refresh tick. These two methods collapse the per-task pattern
    # into one query per project.
    # ------------------------------------------------------------------

    def task_numbers_with_context_entry(
        self, *, project: str, entry_type: str,
    ) -> set[int]:
        """Return task numbers that have at least one context entry of
        ``entry_type``. Used by the inbox loader's read-marker check —
        replaces a per-task ``get_context(..., entry_type='read', limit=1)``
        roundtrip with a single project-wide query.
        """
        rows = self._conn.execute(
            "SELECT DISTINCT task_number FROM work_context_entries "
            "WHERE task_project = ? AND entry_type = ?",
            (project, entry_type),
        ).fetchall()
        return {int(r["task_number"]) for r in rows}

    def bulk_list_replies(self, *, project: str) -> dict[int, list[ContextEntry]]:
        """Return ``task_number -> [reply entries (oldest first)]`` for
        every task in ``project`` that has at least one reply.

        One query, bucketed in Python. Replaces a per-task
        ``list_replies`` loop on the inbox loader hot path.
        """
        rows = self._conn.execute(
            "SELECT task_number, actor, created_at, text, entry_type "
            "FROM work_context_entries "
            "WHERE task_project = ? AND entry_type = 'reply' "
            "ORDER BY id ASC",
            (project,),
        ).fetchall()
        out: dict[int, list[ContextEntry]] = {}
        for r in rows:
            try:
                etype = r["entry_type"] or "reply"
            except (KeyError, IndexError):
                etype = "reply"
            entry = ContextEntry(
                actor=r["actor"],
                timestamp=datetime.fromisoformat(r["created_at"]),
                text=r["text"],
                entry_type=etype,
            )
            out.setdefault(int(r["task_number"]), []).append(entry)
        return out

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def next(
        self, *, agent: str | None = None, project: str | None = None
    ) -> Task | None:
        """Return the highest-priority queued+unblocked task.

        Priority ordering: critical > high > normal > low, then FIFO by created_at.
        Does NOT claim the task.
        """
        return next_task(self, agent=agent, project=project)

    def my_tasks(self, agent: str) -> list[Task]:
        """All tasks where *agent* fills a role that owns the current node.

        For each task with a non-null current_node_id, resolve the current
        node's actor and check if the agent matches the expected role.
        """
        return read_my_tasks(self, agent)

    def state_counts(self, project: str | None = None) -> dict[str, int]:
        """Task counts by work_status. For dashboards."""
        return read_state_counts(self, project)

    def blocked_tasks(self, project: str | None = None) -> list[Task]:
        """All tasks with ``work_status == blocked`` in a non-terminal state.

        Per spec (§5 + OQ-7): a task that is currently ``blocked`` must be
        surfaced regardless of whether its blocker is still active, done,
        or cancelled — the PM needs to see cancelled-blocker cases to
        decide whether to unblock or cancel. Dependency-resolution gating
        is internal to ``next()`` and auto-unblock.
        """
        return read_blocked_tasks(self, project)

    # ------------------------------------------------------------------
    # Flows (public API)
    # ------------------------------------------------------------------

    def _resolve_project_path(self, project: str | None) -> Path | None:
        """Resolve a project name to a filesystem path.

        Falls back to the constructor-provided ``project_path`` when the
        name can't be resolved via config. Returns ``None`` only when no
        fallback is available.
        """
        if project is None:
            return self._project_path

        # A service explicitly scoped to ``/path/to/foo-bar`` should keep
        # using that path for tasks whose project name is ``foo-bar`` or
        # ``foo_bar``. Otherwise a global config entry with the same key can
        # leak into isolated/project-scoped services and send git operations
        # to the wrong checkout.
        if self._project_path is not None:
            bound_name = self._project_path.name
            normalized_project = project.replace("-", "_")
            bound_aliases = {
                bound_name,
                bound_name.replace("-", "_"),
                bound_name.replace("_", "-"),
            }
            if project in bound_aliases or normalized_project in bound_aliases:
                return self._project_path

        # Try the pollypm config for a matching project name.
        try:
            from pollypm.config import load_config
            config = load_config()
            normalized = project.replace("-", "_")
            key = project if project in config.projects else (
                normalized if normalized in config.projects else None
            )
            if key is not None:
                return config.projects[key].path
        except Exception:
            pass

        # Fallback: if it looks like a path, use it; otherwise stick with
        # the service's bound project_path.
        candidate = Path(project)
        if candidate.exists() and candidate.is_dir():
            return candidate

        return self._project_path

    # Files that are safe to merge by concatenating + dedup-preserving-order.
    # All are line-oriented ignore/attribute lists where union semantics are
    # the right answer when both sides independently add the file.
    _UNION_SAFE_MERGE_FILES = frozenset(
        {
            ".gitignore",
            ".dockerignore",
            ".eslintignore",
            ".prettierignore",
            ".npmignore",
            ".gitattributes",
        }
    )

    def _auto_merge_approved_task_branch(
        self,
        task: Task,
        resume_merge: bool = False,
    ) -> None:
        """Merge an approved task branch into the repo's current branch.

        When the standard merge produces conflicts only on
        ``_UNION_SAFE_MERGE_FILES`` (e.g. ``.gitignore``), each conflicting
        file is resolved by taking the union of both sides' lines (deduped,
        order-preserving) and the merge is finalized. Conflicts on other
        files (e.g. ``README.md``) are reported with a friendlier error and
        the in-progress merge is aborted.

        ``resume_merge=True`` lets a caller continue after a hand-resolved
        conflict: if the merge has already been completed externally (the
        task branch is an ancestor of HEAD) or an in-progress merge has
        every conflict resolved + staged, this commits + returns.
        """
        project_path = self._resolve_project_path(task.project)
        if project_path is None or not (project_path / ".git").exists():
            return

        task_branch = f"task/{task.project}-{task.task_number}"
        current_branch = self._git_stdout(
            project_path,
            "rev-parse",
            "--abbrev-ref",
            "HEAD",
            error_prefix="Cannot determine the canonical branch for auto-merge.",
        )
        if current_branch == task_branch:
            return

        # --resume path: the user (or a previous run) may have hand-resolved
        # an earlier conflict. Two valid states to detect here.
        if resume_merge:
            already_merged = self._git_run(
                project_path,
                "merge-base",
                "--is-ancestor",
                task_branch,
                "HEAD",
            )
            if already_merged.returncode == 0:
                return

            merge_head = project_path / ".git" / "MERGE_HEAD"
            if merge_head.exists():
                # If unresolved conflicts remain, surface them; otherwise
                # commit the staged merge.
                conflicts = self._git_run(
                    project_path,
                    "diff",
                    "--name-only",
                    "--diff-filter=U",
                )
                if (
                    conflicts.returncode == 0
                    and conflicts.stdout.strip() == ""
                ):
                    commit = self._git_run(
                        project_path,
                        "commit",
                        "--no-edit",
                    )
                    if commit.returncode == 0:
                        return
                    detail = (
                        commit.stderr.strip()
                        or commit.stdout.strip()
                        or "git commit failed"
                    )
                    raise ValidationError(
                        f"Could not finalize the in-progress merge of "
                        f"`{task_branch}` into `{current_branch}`. "
                        f"Git said: {detail}"
                    )
                # Conflicts remain unresolved; fall through to the standard
                # error-surfacing path below.
                still = (
                    conflicts.stdout.strip()
                    if conflicts.returncode == 0
                    else "(unable to list conflicts)"
                )
                raise ValidationError(
                    f"Cannot resume merge of `{task_branch}` into "
                    f"`{current_branch}`: unresolved conflicts in:\n"
                    f"  {still}\n"
                    f"Resolve them by editing each file, then run "
                    f"`git -C {project_path} add <file>` and retry "
                    f"`pm task approve --resume`."
                )

        status = self._git_run(project_path, "status", "--porcelain")
        if status.returncode != 0:
            detail = status.stderr.strip() or status.stdout.strip() or "git status failed"
            raise ValidationError(
                "Cannot auto-merge approved work right now. "
                f"Git status failed in {project_path}: {detail}"
            )
        # ``rstrip`` not ``strip`` so we keep any leading space from
        # the first porcelain status code (e.g. ` M path` for a
        # modified-in-worktree-only file). ``.strip()`` would shift
        # the path by one character and break the helper's parser
        # (#930).
        dirty_status = status.stdout.rstrip()
        if dirty_status and not _status_is_only_pollypm_scaffold(
            project_path,
            dirty_status,
        ):
            raise ValidationError(
                "Cannot auto-merge approved work because the project root has "
                "uncommitted changes. Commit or stash them, then retry approve."
            )

        branch_exists = self._git_run(project_path, "rev-parse", "--verify", task_branch)
        if branch_exists.returncode != 0:
            raise ValidationError(
                f"Cannot auto-merge approved work because branch `{task_branch}` "
                "does not exist."
            )

        already_merged = self._git_run(
            project_path,
            "merge-base",
            "--is-ancestor",
            task_branch,
            "HEAD",
        )
        if already_merged.returncode == 0:
            return

        # #946 — handle PollyPM-allowlisted untracked files at the
        # project root before invoking ``git merge``. The dirty-tree
        # gate above lets these files past, but git's merge engine
        # still refuses to overwrite untracked files.
        #
        # #947 — for each allowlisted untracked entry we now check the
        # worker branch tip: if the worker committed the same path,
        # the file is removed so the merge can bring in the worker's
        # authoritative version (allowlist contract: PollyPM-managed,
        # worker wins). If the worker branch does NOT commit the path
        # (e.g. ``.itsalive`` written by a separate ``pm itsalive
        # deploy`` run), we instead stage + commit the file on the
        # current branch so the merge preserves it on main rather than
        # silently deleting the deploy-config a worker just produced.
        # Add/add conflicts on tracked safelisted files (e.g.
        # ``.gitignore``) still flow through the existing #925
        # union-strategy logic; non-safelist conflicts still surface
        # via the existing approve-conflict path.
        self._stage_pollypm_untracked_for_merge(
            project_path, dirty_status, task_branch
        )

        ff_only = self._git_run(project_path, "merge", "--ff-only", task_branch)
        if ff_only.returncode == 0:
            return

        # Use ``merge.conflictStyle=diff3`` so any conflict markers in the
        # working tree carry the merge base section. The disjoint-addition
        # auto-resolver (#1072) needs the base to distinguish "both sides
        # added new code" (case 1, auto-resolvable) from "both sides
        # modified the same shared line" (case 3, bounce to operator).
        merge = self._git_run(
            project_path,
            "-c", "merge.conflictStyle=diff3",
            "merge", "--no-ff", "--no-edit", task_branch,
        )
        if merge.returncode == 0:
            return

        # Merge produced conflicts. Inspect them: if every conflict is on a
        # union-safe file, resolve each via line-union and commit. Otherwise
        # abort and surface a clearer error.
        conflicts_result = self._git_run(
            project_path,
            "diff",
            "--name-only",
            "--diff-filter=U",
        )
        conflicted_files = (
            [
                line.strip()
                for line in conflicts_result.stdout.splitlines()
                if line.strip()
            ]
            if conflicts_result.returncode == 0
            else []
        )

        unsafe = [
            f for f in conflicted_files
            if f not in self._UNION_SAFE_MERGE_FILES
        ]
        if conflicted_files and not unsafe:
            # Every conflict is union-safe — resolve them in place.
            try:
                for rel_path in conflicted_files:
                    self._resolve_union_safe_conflict(project_path, rel_path)
            except ValidationError:
                self._git_run(project_path, "merge", "--abort")
                raise

            commit = self._git_run(
                project_path,
                "commit",
                "--no-edit",
            )
            if commit.returncode == 0:
                return
            self._git_run(project_path, "merge", "--abort")
            detail = (
                commit.stderr.strip()
                or commit.stdout.strip()
                or "git commit failed after union resolution"
            )
            raise ValidationError(
                f"Could not finalize the auto-merge of `{task_branch}` "
                f"into `{current_branch}` after resolving "
                f"{', '.join(conflicted_files)} via line union. "
                f"Git said: {detail}"
            )

        # At least one conflict is on a non-safelist file. Before bouncing
        # to the operator, try the disjoint-addition resolver: parallel
        # task branches frequently produce conflicts whose every hunk is
        # "both sides added new code, base region was empty" (#1072). That
        # shape is textually unambiguous — we can auto-resolve by including
        # both sides. Anything more complex (overlapping edits to shared
        # base content) still surfaces to the operator.
        if conflicted_files:
            # Conflict markers are already in diff3 form (see merge
            # invocation above). Walk each file's hunks; auto-resolve
            # iff every hunk has an empty base section (= pure addition).
            resolved_paths: list[str] = []
            for rel_path in conflicted_files:
                target = project_path / rel_path
                try:
                    current_text = target.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    resolved_paths = []
                    break
                merged = _resolve_disjoint_addition_conflicts(current_text)
                if merged is None:
                    resolved_paths = []
                    break
                target.write_text(merged, encoding="utf-8")
                add = self._git_run(project_path, "add", "--", rel_path)
                if add.returncode != 0:
                    resolved_paths = []
                    break
                resolved_paths.append(rel_path)
            if resolved_paths and len(resolved_paths) == len(conflicted_files):
                commit = self._git_run(
                    project_path, "commit", "--no-edit",
                )
                if commit.returncode == 0:
                    return
                # Commit failed even after textual resolution — fall through
                # and let ``git merge --abort`` below clean the worktree.

        # At least one conflict is on a non-safelist file. Abort the merge
        # cleanly and tell the user how to recover.
        self._git_run(project_path, "merge", "--abort")
        if unsafe:
            files_list = "\n".join(f"  - {f}" for f in unsafe)
            raise ValidationError(
                f"Could not auto-merge `{task_branch}` into "
                f"`{current_branch}`: add/add or content conflict on "
                f"file(s) that are not safe to merge automatically:\n"
                f"{files_list}\n"
                f"\n"
                f"Resolve in the project root and retry approve:\n"
                f"  cd {project_path}\n"
                f"  git merge {task_branch}      # re-attempt the merge\n"
                f"  # edit each conflicted file, then:\n"
                f"  git add <file> && git commit\n"
                f"  pm task approve {task.task_id} --resume"
            )

        # Fallback: no diagnosable conflict list, surface raw git output.
        detail = (
            merge.stderr.strip()
            or merge.stdout.strip()
            or ff_only.stderr.strip()
            or "git merge failed"
        )
        raise ValidationError(
            f"Could not auto-merge `{task_branch}` into `{current_branch}`. "
            f"Resolve the repo state and retry approve. Git said: {detail}"
        )

    def _resolve_union_safe_conflict(
        self,
        project_path: Path,
        rel_path: str,
    ) -> None:
        """Resolve a single union-safe add/add or content conflict in place.

        Reads the "ours" (stage 2) and "theirs" (stage 3) blobs, concatenates
        their lines, deduplicates while preserving order, writes the result
        and stages it. For pure add/add (no merge base) the missing stage
        is treated as empty.
        """
        ls_files = self._git_run(
            project_path,
            "ls-files",
            "-u",
            "--",
            rel_path,
        )
        if ls_files.returncode != 0:
            raise ValidationError(
                f"Could not inspect conflict on `{rel_path}`: "
                f"{ls_files.stderr.strip() or 'git ls-files failed'}"
            )

        # ls-files -u format: "<mode> <sha> <stage>\t<path>"
        stage_blobs: dict[int, str] = {}
        for line in ls_files.stdout.splitlines():
            if not line.strip():
                continue
            try:
                meta, _path = line.split("\t", 1)
                _mode, sha, stage = meta.split()
                stage_blobs[int(stage)] = sha
            except ValueError:
                continue

        ours_text = ""
        theirs_text = ""
        if 2 in stage_blobs:
            ours = self._git_run(project_path, "cat-file", "-p", stage_blobs[2])
            if ours.returncode != 0:
                raise ValidationError(
                    f"Could not read 'ours' side of `{rel_path}`: "
                    f"{ours.stderr.strip() or 'git cat-file failed'}"
                )
            ours_text = ours.stdout
        if 3 in stage_blobs:
            theirs = self._git_run(project_path, "cat-file", "-p", stage_blobs[3])
            if theirs.returncode != 0:
                raise ValidationError(
                    f"Could not read 'theirs' side of `{rel_path}`: "
                    f"{theirs.stderr.strip() or 'git cat-file failed'}"
                )
            theirs_text = theirs.stdout

        merged_text = _union_merge_text(ours_text, theirs_text)
        target = project_path / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(merged_text, encoding="utf-8")

        add = self._git_run(project_path, "add", "--", rel_path)
        if add.returncode != 0:
            raise ValidationError(
                f"Could not stage union-merged `{rel_path}`: "
                f"{add.stderr.strip() or 'git add failed'}"
            )

    # #946 — defensive cap on per-file size for pre-merge auto-stage.
    # The allowlist predicates already constrain the candidates (200B
    # CLAUDE.md stub, small JSON ``.itsalive``, marker-bearing
    # ``ITSALIVE.md``, line-oriented ``issues/`` markdown), so this is
    # a belt-and-braces guard against an unexpectedly large file
    # sneaking past the predicate.
    _PRE_STAGE_MAX_BYTES = 1 * 1024 * 1024

    def _stage_pollypm_untracked_for_merge(
        self,
        project_path: Path,
        status_stdout: str,
        task_branch: str,
    ) -> None:
        """Resolve PollyPM-allowlisted untracked files for ``git merge``.

        The approve dirty-tree gate (#930 + #945) lets a known set of
        scaffold files past even when untracked. Git's merge engine,
        however, refuses to overwrite untracked working-tree files —
        so when the worker's branch commits the same allowlisted file
        the merge aborts with "untracked working tree files would be
        overwritten" (#946).

        For each untracked allowlisted entry we now branch on whether
        the worker's branch tip commits that path:

        - **Worker branch has the path**: delete the working-tree copy
          so ``git merge`` can bring in the worker's authoritative
          content. This matches the allowlist's contract: every
          covered path is exclusively PollyPM-written (deployToken
          JSON, marker-bearing scaffold docs, the issues/ tree, etc.),
          so the worker's commit is the right post-merge truth.

        - **Worker branch does NOT have the path** (#947): stage and
          commit the file on the current branch *before* the merge so
          it's preserved through the merge. Without this, an untracked
          ``.itsalive`` written by a separate ``pm itsalive deploy``
          run would be silently deleted on every approve — destroying
          the deploy config the worker just produced.

        Only files matching the same allowlist as the dirty-tree gate
        are touched. Modified-but-tracked entries are skipped (they
        don't trigger the untracked-overwrite error). Anything outside
        the allowlist is left alone — we never blanket-delete user
        content here.
        """
        preserve_paths: list[str] = []
        for line in status_stdout.splitlines():
            if not line.strip():
                continue
            # Porcelain XY status: untracked entries are ``??``. We
            # only need to handle untracked files because tracked
            # modifications don't trigger the
            # "untracked-working-tree-files-would-be-overwritten"
            # merge error.
            xy = line[:2]
            if xy != "??":
                continue
            rel_path = _porcelain_status_path(line)
            if not rel_path:
                continue
            if not _path_is_pollypm_scaffold(project_path, rel_path):
                continue

            # #947 (reopen): ``git status`` collapses an untracked
            # tree to a single ``?? issues/`` line, so a per-path
            # decision lumps the whole directory together. Expand
            # any directory entry to its individual untracked leaf
            # files via ``git ls-files --others`` so we can decide
            # per file: a colliding leaf still gets the #946 worker-
            # wins delete, but a sibling local-only file under the
            # same allowlisted dir survives via the #947 preserve
            # path.
            target = project_path / rel_path
            if target.is_dir() and not target.is_symlink():
                leaf_paths = self._expand_untracked_directory(
                    project_path, rel_path
                )
            else:
                leaf_paths = [rel_path]

            for leaf_rel_path in leaf_paths:
                # Re-apply the allowlist on each leaf: callers
                # passed the predicate at the directory level, but
                # individual files under it must still match (in
                # practice ``_issues_path_is_pollypm_managed``
                # accepts every path under ``issues/``, but we keep
                # the check defensive).
                if not _path_is_pollypm_scaffold(project_path, leaf_rel_path):
                    continue

                leaf_target = project_path / leaf_rel_path
                # Mirror the predicate's content gate: a binary or
                # unexpectedly large file aborts the auto-merge
                # with a clear error rather than getting silently
                # swept into the commit.
                if leaf_target.is_file():
                    try:
                        size = leaf_target.stat().st_size
                    except OSError:
                        size = None
                    if size is not None and size > self._PRE_STAGE_MAX_BYTES:
                        raise ValidationError(
                            f"Cannot auto-merge approved work: PollyPM "
                            f"scaffold file `{leaf_rel_path}` is unexpectedly "
                            f"large ({size} bytes). Inspect it and either "
                            f"commit or remove it before retrying approve."
                        )

                # #947: if the worker branch doesn't commit this
                # path, stage + commit it on the current branch so
                # the merge preserves it. Otherwise (worker branch
                # has the path) fall back to the #946 behavior:
                # remove the working-tree copy so the worker's
                # version wins through the merge.
                if not self._worker_branch_has_path(
                    project_path, task_branch, leaf_rel_path
                ):
                    preserve_paths.append(leaf_rel_path)
                    continue

                try:
                    if leaf_target.is_symlink() or leaf_target.is_file():
                        leaf_target.unlink()
                    elif leaf_target.is_dir():
                        import shutil

                        shutil.rmtree(leaf_target)
                    else:
                        # Path vanished between status output and
                        # now; nothing to do.
                        continue
                except OSError as exc:
                    raise ValidationError(
                        f"Cannot auto-merge approved work: failed to "
                        f"clear PollyPM scaffold path `{leaf_rel_path}` from "
                        f"the working tree before merge: {exc}"
                    ) from exc

        if preserve_paths:
            # Stage all preserved paths in one ``git add`` and commit
            # them with a single PollyPM-attributed commit so the
            # subsequent merge sees them as tracked content on the
            # current branch.
            add = self._git_run(
                project_path, "add", "--", *preserve_paths
            )
            if add.returncode != 0:
                raise ValidationError(
                    f"Cannot auto-merge approved work: failed to "
                    f"stage PollyPM scaffold paths "
                    f"{', '.join(preserve_paths)} for preservation "
                    f"before merge: "
                    f"{add.stderr.strip() or 'git add failed'}"
                )
            commit = self._git_run(
                project_path,
                "commit",
                "-m",
                "chore(pollypm): preserve scaffold files through approve merge",
            )
            if commit.returncode != 0:
                raise ValidationError(
                    f"Cannot auto-merge approved work: failed to "
                    f"commit PollyPM scaffold paths "
                    f"{', '.join(preserve_paths)} for preservation "
                    f"before merge: "
                    f"{commit.stderr.strip() or commit.stdout.strip() or 'git commit failed'}"
                )

    def _worker_branch_has_path(
        self,
        project_path: Path,
        task_branch: str,
        rel_path: str,
    ) -> bool:
        """Return True iff ``task_branch`` tip contains ``rel_path``.

        Used by ``_stage_pollypm_untracked_for_merge`` (#947) to decide
        whether an untracked allowlisted file should be deleted (worker
        branch has it — worker wins) or staged + committed (worker
        branch doesn't have it — preserve the local copy through the
        merge so it isn't silently deleted).
        """
        ls_tree = self._git_run(
            project_path,
            "ls-tree",
            "-r",
            "--name-only",
            task_branch,
            "--",
            rel_path.rstrip("/"),
        )
        if ls_tree.returncode != 0:
            # Conservative fallback: if we can't inspect the branch,
            # behave like the pre-#947 code (assume worker has it,
            # delete locally so merge proceeds). This preserves
            # forward-progress semantics for the existing test cases.
            return True
        return bool(ls_tree.stdout.strip())

    def _expand_untracked_directory(
        self,
        project_path: Path,
        rel_dir: str,
    ) -> list[str]:
        """Expand an untracked directory into its individual leaf files.

        ``git status --porcelain`` collapses a fully-untracked tree to
        a single ``?? <dir>/`` entry. The #947 reopen showed that a
        per-directory delete-or-preserve decision destroys non-
        colliding local-only files when *any* path under the directory
        is committed on the worker branch. Expanding to per-file
        granularity lets the caller apply the worker-wins / preserve
        decision per leaf.

        We use ``git ls-files --others --exclude-standard`` (rather
        than ``os.walk``) so the result respects ``.gitignore`` —
        same predicate git itself used to mark the directory
        untracked in the first place.
        """
        cleaned = rel_dir.rstrip("/")
        ls = self._git_run(
            project_path,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            cleaned,
        )
        if ls.returncode != 0 or not ls.stdout:
            # Couldn't enumerate — fall back to treating the entry
            # as a single path so the caller's existing handling
            # (including the worker-has-path delete) still runs. This
            # preserves forward-progress semantics; the worst case is
            # the pre-#947-reopen behavior on this one entry.
            return [cleaned]
        leaves: list[str] = []
        for raw in ls.stdout.split("\x00"):
            leaf = raw.strip()
            if not leaf:
                continue
            # Defense-in-depth: never touch ``.git/`` if it somehow
            # surfaces here.
            if leaf == ".git" or leaf.startswith(".git/"):
                continue
            leaves.append(leaf)
        return leaves or [cleaned]

    def _git_run(self, project_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(project_path), *args],
            check=False,
            capture_output=True,
            text=True,
        )

    def _git_stdout(
        self,
        project_path: Path,
        *args: str,
        error_prefix: str,
    ) -> str:
        result = self._git_run(project_path, *args)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "command failed"
            raise ValidationError(f"{error_prefix} {detail}")
        return result.stdout.strip()

    def available_flows(self, project: str | None = None) -> list[FlowTemplate]:
        """List all available flows after override resolution.

        When ``project`` is supplied, resolves to that project's path (via
        the pollypm config) and includes its project-local flows.
        """
        from pollypm.work.flow_engine import available_flows as _available_flows

        project_path = self._resolve_project_path(project)
        flow_map = _available_flows(project_path)
        templates: list[FlowTemplate] = []
        for name, path in flow_map.items():
            try:
                tmpl = resolve_flow(name, project_path)
                templates.append(tmpl)
            except Exception:
                pass
        return templates

    def get_flow(self, name: str, project: str | None = None) -> FlowTemplate:
        """Resolve a flow by name through the override chain.

        When ``project`` is supplied, resolves to that project's path (via
        the pollypm config) so project-local overrides apply.
        """
        project_path = self._resolve_project_path(project)
        return resolve_flow(name, project_path)

    # ------------------------------------------------------------------
    # Sync
    # ------------------------------------------------------------------

    def _record_sync_state(
        self,
        project: str,
        task_number: int,
        adapter_name: str,
        success: bool,
        error: str | None,
    ) -> None:
        """Upsert a work_sync_state row after attempting to sync a task."""
        record_sync_state(
            self,
            project,
            task_number,
            adapter_name,
            success=success,
            error=error,
        )

    def sync_status(self, task_id: str) -> dict[str, object]:
        """Current sync state per registered adapter for a task.

        Returns a mapping ``adapter_name -> {last_synced_at, last_error,
        attempts}``. Adapters that have never attempted a sync for this
        task appear with ``None`` fields and ``attempts=0``.
        """
        return read_sync_status(self, task_id)

    def trigger_sync(
        self,
        task_id: str | None = None,
        adapter: str | None = None,
    ) -> dict[str, object]:
        """Force a sync cycle. Optional filters.

        - ``task_id``: only sync this task (otherwise sync every task).
        - ``adapter``: only dispatch to the adapter with this ``name``.

        Returns a summary: ``{synced: int, errors: {adapter_name:
        [task_id, ...]}}``.
        """
        return run_trigger_sync(self, task_id=task_id, adapter=adapter)

    # ------------------------------------------------------------------
    # Worker sessions
    # ------------------------------------------------------------------
    #
    # Schema + CRUD for the work_sessions table backing SessionManager.
    # Owning these rows here (instead of SessionManager reaching into
    # ``self._conn``) keeps the session manager honest about the service
    # protocol surface and makes the whole binding mockable.

    def ensure_worker_session_schema(self) -> None:
        self._worker_session_mgr.ensure_schema()

    def upsert_worker_session(
        self,
        *,
        task_project: str,
        task_number: int,
        agent_name: str,
        pane_id: str,
        worktree_path: str,
        branch_name: str,
        started_at: str,
        provider: str | None = None,
        provider_home: str | None = None,
    ) -> None:
        self._worker_session_mgr.upsert(
            task_project=task_project,
            task_number=task_number,
            agent_name=agent_name,
            pane_id=pane_id,
            worktree_path=worktree_path,
            branch_name=branch_name,
            started_at=started_at,
            provider=provider,
            provider_home=provider_home,
        )

    def get_worker_session(
        self,
        *,
        task_project: str,
        task_number: int,
        active_only: bool = False,
    ) -> WorkerSessionRecord | None:
        return self._worker_session_mgr.get(
            task_project=task_project,
            task_number=task_number,
            active_only=active_only,
        )

    def list_worker_sessions(
        self,
        *,
        project: str | None = None,
        active_only: bool = True,
    ) -> list[WorkerSessionRecord]:
        return self._worker_session_mgr.list(
            project=project,
            active_only=active_only,
        )

    def end_worker_session(
        self,
        *,
        task_project: str,
        task_number: int,
        ended_at: str,
        total_input_tokens: int,
        total_output_tokens: int,
        archive_path: str | None,
    ) -> None:
        self._worker_session_mgr.end(
            task_project=task_project,
            task_number=task_number,
            ended_at=ended_at,
            total_input_tokens=total_input_tokens,
            total_output_tokens=total_output_tokens,
            archive_path=archive_path,
        )

    def mark_worker_session_ended(
        self,
        *,
        task_project: str,
        task_number: int,
        ended_at: str,
    ) -> None:
        """Stamp ``ended_at`` without touching token counters (#1014).

        Used by the orphan-reap path so a crash-recovery doesn't zero
        out the tokens an earlier session already wrote. The next
        teardown's archive scan still overwrites with the worktree's
        cumulative total via SET semantics.
        """
        self._worker_session_mgr.mark_ended(
            task_project=task_project,
            task_number=task_number,
            ended_at=ended_at,
        )

    def update_worker_session_tokens(
        self,
        *,
        task_project: str,
        task_number: int,
        total_input_tokens: int,
        total_output_tokens: int,
        archive_path: str | None,
    ) -> None:
        self._worker_session_mgr.update_tokens(
            task_project=task_project,
            task_number=task_number,
            total_input_tokens=total_input_tokens,
            total_output_tokens=total_output_tokens,
            archive_path=archive_path,
        )
