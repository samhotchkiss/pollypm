"""Storage source-of-truth read APIs and migration guards (#887).

One registry that names the canonical reader for every storage
concept (project, session, inbox item, activity event, alert,
task, execution, transcript, token usage, provider account)
plus migration-completion guards that fail if a legacy writer
can still produce user-visible state.

The pre-launch audit (``docs/launch-issue-audit-2026-04-27.md``
§6) cites the recurring shape: data exists, but a reader queries
the wrong scope, project DB, legacy table, namespace, provider
home, or ambient environment. Counts diverge because Home, Rail,
Inbox, Activity, and CLI paths each maintain their own reader.

* `#820` — Home counted only tracked projects while Rail counted
  registered projects.
* `#259` / `#377` — workspace DB vs per-project DB confusion
  killed task pickup / task-status discovery.
* `#271` — notifications written to a namespace the cockpit did
  not surface.
* `#809` / `#812` / `#813` / `#814` — transcript / token
  accounting used the wrong provider root, wrong tree, or
  incomplete token schema.
* `#704` — ``notification_staging`` compatibility seam still
  reachable.

The structural fix is to declare *one* canonical read API per
concept and surface (via :func:`audit_legacy_writers`) the legacy
writers that can still produce user-visible state. The release
gate (#889) consults the audit at tag time; v1 cannot ship
while a legacy writer's row would be read by a user surface.

This module is intentionally thin — it does *not* replace the
existing reader implementations. It declares the canonical
location and gives every consumer one import path so future
moves of the underlying implementation do not fork callers.

Architecture:

* :class:`StorageConcept` — enum of every concept the cockpit
  needs to read.
* :class:`ReadAPI` — declarative pointer to ``module:function``.
* :data:`STORAGE_CONTRACTS` — concept → read API mapping.
* :data:`LEGACY_WRITERS` — known compatibility-seam writers
  that must not produce user-visible state.
* :func:`canonical_reader_for` / :func:`audit_legacy_writers` —
  helpers consumers use.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Mapping


# ---------------------------------------------------------------------------
# Concepts
# ---------------------------------------------------------------------------


class StorageConcept(enum.Enum):
    """Every cockpit-relevant storage concept.

    Adding a new concept requires (a) registering a canonical
    read API in :data:`STORAGE_CONTRACTS` and (b) listing any
    legacy writers in :data:`LEGACY_WRITERS`. Both are checked
    by the release-gate audit."""

    PROJECT = "project"
    SESSION = "session"
    INBOX_ITEM = "inbox_item"
    ACTIVITY_EVENT = "activity_event"
    ALERT = "alert"
    TASK = "task"
    EXECUTION = "execution"
    TRANSCRIPT = "transcript"
    TOKEN_USAGE = "token_usage"
    PROVIDER_ACCOUNT = "provider_account"


# ---------------------------------------------------------------------------
# Read API descriptor
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReadAPI:
    """Declarative pointer to a canonical read function.

    Storing the location as ``module:function`` strings lets the
    audit verify the symbol exists at import time without
    forcing this module to import every concept's storage
    backend (which would dramatically increase cockpit startup
    time)."""

    module: str
    function: str
    description: str = ""
    notes: str = ""

    @property
    def dotted(self) -> str:
        return f"{self.module}:{self.function}"


# ---------------------------------------------------------------------------
# The canonical contract registry
# ---------------------------------------------------------------------------


STORAGE_CONTRACTS: Mapping[StorageConcept, ReadAPI] = {
    StorageConcept.PROJECT: ReadAPI(
        module="pollypm.config",
        function="load_config",
        description=(
            "Project identity is config-driven: load_config(...).projects "
            "is the dict of KnownProject rows, including the tracked flag."
        ),
        notes=(
            "Tracked vs registered (#820): config.projects exposes both "
            "via the .tracked attribute. Callers that need tracked-only "
            "must filter; the shared inbox count helper does this "
            "consistently."
        ),
    ),
    StorageConcept.SESSION: ReadAPI(
        module="pollypm.storage.state",
        function="StateStore.list_sessions",
        description="Live session rows from the persisted sessions table.",
        notes=(
            "Cross-checked with `tmux list-windows` via "
            "`launch_state.reconcile_session_inventory` (#871)."
        ),
    ),
    StorageConcept.INBOX_ITEM: ReadAPI(
        module="pollypm.signal_routing",
        function="shared_inbox_count",
        description=(
            "The unified inbox count helper. Re-exports the consolidated "
            "reader so Rail, Home, and Inbox panel cannot diverge (#820)."
        ),
        notes=(
            "Underlying scan is cockpit_inbox._count_inbox_tasks_for_label."
        ),
    ),
    StorageConcept.ACTIVITY_EVENT: ReadAPI(
        module="pollypm.store.sqlalchemy_store",
        function="SQLAlchemyStore.query_messages",
        description=(
            "Filters for `type='event'` produce the activity feed "
            "query. All cockpit, CLI, and plugin readers go through "
            "this single method."
        ),
    ),
    StorageConcept.ALERT: ReadAPI(
        module="pollypm.signal_routing",
        function="shared_alert_count",
        description=(
            "Filters operational alerts by default (#879). Callers "
            "that need raw count pass include_operational=True."
        ),
    ),
    StorageConcept.TASK: ReadAPI(
        module="pollypm.work.sqlite_service",
        function="SQLiteWorkService.list_tasks",
        description=(
            "The work service's task list. Resolved to "
            "<workspace_root>/.pollypm/state.db by "
            "pollypm.work.db_resolver — project isolation is row-level "
            "via the work_tasks.project column (#1004)."
        ),
    ),
    StorageConcept.EXECUTION: ReadAPI(
        module="pollypm.work.sqlite_service",
        function="SQLiteWorkService.get_execution",
        description=(
            "Flow node execution history. Recovery must NEVER delete "
            "rows (#806); transitions are append-only."
        ),
    ),
    StorageConcept.TRANSCRIPT: ReadAPI(
        module="pollypm.transcript_ingest",
        function="TranscriptIngestor",
        description=(
            "Provider-aware transcript ingestion. Provider root path "
            "is persisted at launch and used at teardown (#809/#812)."
        ),
    ),
    StorageConcept.TOKEN_USAGE: ReadAPI(
        module="pollypm.storage.state",
        function="StateStore.get_token_sample",
        description=(
            "Live cumulative tokens per session. Hourly aggregates "
            "live in StateStore.query_token_usage_hourly."
        ),
        notes=(
            "Two storage rows per concept (token_samples + "
            "token_usage_hourly) is intentional — different "
            "granularities, different consumers. Both go through "
            "StateStore methods, no parallel readers."
        ),
    ),
    StorageConcept.PROVIDER_ACCOUNT: ReadAPI(
        module="pollypm.config",
        function="load_config",
        description=(
            "Account identity is config-driven; runtime status lives "
            "in StateStore.account_runtime."
        ),
        notes=(
            "AccountConfig is the identity source; "
            "StateStore.list_account_runtimes is the runtime status "
            "source. Two reads, two distinct concerns."
        ),
    ),
}


# ---------------------------------------------------------------------------
# Legacy writers (compatibility seams)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LegacyWriter:
    """A writer the audit identified as a compatibility seam.

    The release gate fails when any of these can still produce
    user-visible state. Each entry names the writer, the concept
    it shadows, the migration plan, and a removal condition.
    """

    name: str
    concept: StorageConcept
    migration_plan: str
    removal_condition: str
    is_isolated: bool = False
    """``True`` once the writer is gated behind a documented
    compatibility-only path that cannot reach user surfaces."""
    tracked_issue: str | None = None
    """Issue number that owns the migration. When set, the audit
    treats the entry as an *accepted-risk* downgrade rather than a
    hard blocker — the release gate logs it as a warning. The audit
    also requires :attr:`is_isolated` to be False on tracked
    entries; an isolated writer needs no tracking issue."""


LEGACY_WRITERS: tuple[LegacyWriter, ...] = (
    LegacyWriter(
        name="notification_staging table",
        concept=StorageConcept.INBOX_ITEM,
        migration_plan=(
            "Port to type='staging' partition of the unified messages "
            "table. The CLI pm notify path classifies priority and "
            "stages digest items there; flush on milestone completion."
        ),
        removal_condition=(
            "Issue #704 closed — compatibility seam retired or "
            "isolated behind a documented migration boundary."
        ),
        is_isolated=False,
        tracked_issue="#704",
    ),
    LegacyWriter(
        name="legacy per-project state.db",
        concept=StorageConcept.TASK,
        migration_plan=(
            "Post-#1004 the canonical work DB is workspace-root only. "
            "Any pre-existing <project>/.pollypm/state.db is migrated "
            "into the workspace DB by "
            "pollypm.storage.legacy_per_project_db.migrate_legacy_per_project_dbs "
            "and archived to state.db.legacy-1004. The resolver no "
            "longer routes reads to per-project files."
        ),
        removal_condition=(
            "Issue #1004 closed — every install has run the migration "
            "and no production environment still ships per-project "
            "state.db files."
        ),
        is_isolated=True,
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def canonical_reader_for(concept: StorageConcept) -> ReadAPI:
    """Return the canonical :class:`ReadAPI` for ``concept``.

    Raises :class:`KeyError` when a concept has no canonical
    reader registered — the release gate uses this to refuse a
    tag if a new concept lands without a registered API."""
    return STORAGE_CONTRACTS[concept]


def all_concepts_have_canonical_reader() -> tuple[str, ...]:
    """Return the names of any :class:`StorageConcept` lacking an
    entry in :data:`STORAGE_CONTRACTS`.

    A clean run returns ``()``."""
    missing: list[str] = []
    for concept in StorageConcept:
        if concept not in STORAGE_CONTRACTS:
            missing.append(concept.name)
    return tuple(missing)


def audit_legacy_writers() -> tuple[str, ...]:
    """Return human-readable descriptions of every legacy writer
    that is neither isolated nor tracked under a migration issue.

    A legacy writer with no ``is_isolated`` status and no
    ``tracked_issue`` is a launch blocker because its rows can
    reach user surfaces undocumented (the audit's #820 / #271
    shape). Writers tracked under a migration issue surface
    through :func:`tracked_legacy_writers` instead — the release
    gate (#889) reports them as warnings, not blockers, while the
    referenced issue stays open."""
    out: list[str] = []
    for writer in LEGACY_WRITERS:
        if writer.is_isolated:
            continue
        if writer.tracked_issue:
            continue
        out.append(
            f"{writer.name} (shadows {writer.concept.value}): "
            f"{writer.migration_plan} "
            f"[removal condition: {writer.removal_condition}]"
        )
    return tuple(out)


def tracked_legacy_writers() -> tuple[str, ...]:
    """Return human-readable descriptions of legacy writers whose
    migration is in progress under a tracked issue.

    The release gate surfaces these as warnings — they are
    accepted-risk downgrades documented in the contract, not
    blockers. When the tracked issue closes, the entry should
    flip to ``is_isolated=True`` (or be removed entirely)."""
    out: list[str] = []
    for writer in LEGACY_WRITERS:
        if writer.is_isolated:
            continue
        if not writer.tracked_issue:
            continue
        out.append(
            f"{writer.name} (shadows {writer.concept.value}): "
            f"tracked under {writer.tracked_issue} — "
            f"{writer.migration_plan}"
        )
    return tuple(out)


def reader_module_paths() -> tuple[str, ...]:
    """Return the unique module paths for every canonical reader.

    Used by import-boundary tests so the boundary check can name
    the canonical reader modules without re-deriving the list."""
    seen: set[str] = set()
    for read in STORAGE_CONTRACTS.values():
        seen.add(read.module)
    return tuple(sorted(seen))
