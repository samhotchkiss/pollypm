"""Worker session lifecycle manager.

Contract:
- Inputs: task ids, agent/account names, a tmux client, a work-service
  implementation, and optional config/session-service facades.
- Outputs: ``WorkerSession`` / ``TeardownResult`` records plus persisted
  worker-session rows owned by the work service.
- Side effects: creates and removes git worktrees, opens and tears down
  tmux windows, writes prompt/transcript artifacts, and updates worker
  session bookkeeping through typed work-service methods.
- Invariants: one active worker session per task, task-scoped provision
  locks serialize competing claims, configured launches route through the
  provider/runtime adapters, and prompt/provision failures abort instead
  of silently launching an unbootstrapped worker.
- Allowed dependencies: public work-service APIs, provider/runtime
  registries, session-service facade, and project lock helpers.
- Private: prompt rendering, tmux bootstrap helpers, and transcript
  archival details.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from subprocess import CalledProcessError as _CalledProcessError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ProvisionError(RuntimeError):
    """Raised when worker provisioning truly fails.

    Surfaced to ``pm task claim`` so the user sees an actionable message
    ("claim recorded, but session provisioning failed — run ``pm task
    release`` and retry") rather than a silent log warning that leaves
    the task stuck ``in_progress`` with no session. See #243.
    """


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class WorkerSession:
    """Active binding between a task and a tmux/worktree session."""

    task_id: str
    agent_name: str
    pane_id: str
    worktree_path: Path
    branch_name: str
    started_at: datetime


@dataclass(slots=True)
class TeardownResult:
    """Result of tearing down a worker session."""

    task_id: str
    jsonl_archived: bool
    archive_path: Path | None
    total_input_tokens: int
    total_output_tokens: int
    worktree_removed: bool


def task_window_name(project: str, task_number: int) -> str:
    """Return the canonical tmux window name for a task worker.

    The launcher writes ``task-<project>-<N>`` (see ``provision``); the
    dead-claim recovery sweep needs the same shape to decide whether a
    claim's window is still alive. Centralising the construction here
    keeps launcher and recovery in sync (#807).
    """
    return f"task-{project}-{int(task_number)}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _first_matching_account(
    accounts: dict[str, object],
    *,
    provider: object,
    preferred_names: list[str],
) -> tuple[str, object] | tuple[None, None]:
    ordered: list[str] = []
    for name in preferred_names:
        if name and name not in ordered:
            ordered.append(name)
    for name in accounts:
        if name not in ordered:
            ordered.append(name)
    for name in ordered:
        account = accounts.get(name)
        if account is not None and getattr(account, "provider", None) is provider:
            return name, account
    return None, None


# ---------------------------------------------------------------------------
# SessionManager
# ---------------------------------------------------------------------------


class SessionManager:
    """Deterministic session manager that binds worker sessions to tasks.

    Coordinates git worktrees, tmux panes, and JSONL archival around
    task lifecycle transitions.
    """

    def __init__(
        self,
        tmux_client: object,
        work_service: object,
        project_path: Path,
        *,
        config: object | None = None,
        session_service: object | None = None,
        storage_closet_name: str = "pollypm-storage-closet",
    ) -> None:
        self._tmux = tmux_client
        self._config = config
        self._session_service = session_service
        self._svc = work_service
        self._project_path = project_path
        self._storage_closet_name = storage_closet_name
        # Ensure the sessions table exists. The work service owns the
        # persistence layer — previously we reached into ``_svc._conn``
        # and executed DDL directly (#105).
        self._svc.ensure_worker_session_schema()

    # ------------------------------------------------------------------
    # Provisioning
    # ------------------------------------------------------------------

    def provision_worker(self, task_id: str, agent_name: str) -> WorkerSession:
        """Create worktree + tmux window for a task. Idempotent.

        Launches an *interactive* Claude session in the named tmux window
        (via the ``SessionService`` when provided). The task prompt is
        written to ``.pollypm-task-prompt.md`` inside the worktree and
        a short kickoff string is fed as keystrokes so the worker reads
        the file and gets to work. Relies on ``teardown_worker`` to kill
        the window on approval/cancel.

        Concurrent provision calls for the same ``task_id`` are
        serialized via a per-task session lock so two racing ``claim()``
        invocations can't both run ``git worktree add`` against the
        same path.

        #1012 — when an existing ``work_sessions`` row points at a tmux
        pane that has since died (window closed by the user, host
        rebooted, supervisor crash mid-bootstrap), drop the dead row
        instead of returning it. Pre-#1012 the orphan record made
        ``provision_worker`` a silent no-op forever: the auto-claim
        sweep would re-claim the task every tick, ``release_stale_claim``
        marked the execution ``abandoned`` because no live window
        matched, and the next claim found the same dead row and bailed
        again. ``bikepath/8`` accumulated 60+ ``build vN abandoned``
        rows in ~7h with zero useful work. Validating the existing
        row's pane and tearing it down when stale lets the next
        provision run actually create the window.
        """
        project, task_number = _parse_task_id(task_id)

        # Check for existing active session — but verify the underlying
        # tmux pane is still alive before honouring the row (#1012).
        existing = self.session_for_task(task_id)
        if existing is not None and self._existing_session_is_alive(existing):
            return existing
        if existing is not None:
            self._reap_dead_existing_session(task_id, existing)

        # Derive slug from task_id for branch/path naming
        task_slug = f"{project}-{task_number}"
        branch_name = f"task/{task_slug}"
        # Same shape as ``task_window_name`` — the recovery sweep
        # depends on this exact name to decide a claim is alive (#807).

        # Serialize concurrent provisions of the same task. The lock
        # lives in the worktree's parent directory so both the lock and
        # the worktree-add race on the same filesystem target.
        worktree_parent = self._project_path / ".pollypm" / "worktrees"
        session_id = f"task-{task_slug}"
        lock_acquired = False
        try:
            from pollypm.projects import ensure_session_lock, release_session_lock

            try:
                ensure_session_lock(worktree_parent, session_id)
                lock_acquired = True
            except Exception as exc:  # noqa: BLE001
                # Another provision is in-flight or the lock is stuck.
                # Re-check: if the other provision won, just return its
                # session. Otherwise surface the lock failure.
                logger.warning(
                    "provision_worker[%s]: session lock conflict: %s",
                    task_id, exc,
                )
                existing = self.session_for_task(task_id)
                if existing is not None and self._existing_session_is_alive(existing):
                    return existing
                raise

            # Re-check after acquiring the lock in case another provision
            # completed while we were waiting.
            existing = self.session_for_task(task_id)
            if existing is not None and self._existing_session_is_alive(existing):
                return existing
            if existing is not None:
                self._reap_dead_existing_session(task_id, existing)

            worker_session = self._provision_locked(
                task_id=task_id,
                agent_name=agent_name,
                project=project,
                task_number=task_number,
                task_slug=task_slug,
                branch_name=branch_name,
            )
            return worker_session
        finally:
            if lock_acquired:
                try:
                    release_session_lock(worktree_parent, session_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "provision_worker[%s]: failed to release session lock: %s",
                        task_id, exc,
                    )

    # ------------------------------------------------------------------
    # Existing-session liveness (#1012)
    # ------------------------------------------------------------------

    def _existing_session_is_alive(self, session: "WorkerSession") -> bool:
        """Return True when the existing session row still has a live pane.

        #1012 — pre-fix, ``provision_worker`` returned the existing row
        unconditionally. When the supervisor / user closed the per-task
        tmux window without going through ``teardown_worker``, the row
        survived with ``ended_at IS NULL`` and ``provision_worker``
        became a silent no-op. The auto-claim sweep then accumulated
        endless ``build vN abandoned`` executions because each cycle
        ``release_stale_claim`` marked the active visit abandoned but
        no new window was ever spawned to take its place.

        The check is best-effort: if the tmux client raises (e.g. tmux
        server gone) we treat the session as alive and keep the legacy
        idempotent behaviour, since spawning a fresh window with no
        tmux server would fail anyway.
        """
        pane_id = session.pane_id
        if not pane_id:
            return False
        is_alive = getattr(self._tmux, "is_pane_alive", None)
        if not callable(is_alive):
            return True
        try:
            return bool(is_alive(pane_id))
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "provision_worker[%s]: is_pane_alive(%s) raised: %s; "
                "treating session as alive",
                session.task_id, pane_id, exc,
            )
            return True

    def _reap_dead_existing_session(
        self, task_id: str, session: "WorkerSession",
    ) -> None:
        """End the stale ``work_sessions`` row so re-provision can run.

        #1012 — stamps ``ended_at`` on the orphaned row without
        attempting JSONL archival or worktree removal. Those phases
        belong to the canonical ``teardown_worker`` path; for orphan
        recovery we only need to drop the active-row marker so the
        next ``session_for_task`` call returns ``None`` and the
        provision can proceed.

        #1014 (Bug B) — uses ``mark_worker_session_ended`` so token
        counters are preserved. Pre-#1014 the reap called
        ``end_worker_session(..., total_input_tokens=0,
        total_output_tokens=0, ...)`` which silently clobbered any
        partial-archive token totals from prior cycles; the user-visible
        consequence on ``bikepath/8`` was ``pm task get`` permanently
        showing ``in=0 out=0`` despite real token spend across multiple
        build / review-handoff visits.

        Any DB error is logged and swallowed — the next provision
        will hit the same orphan and retry the reap. Far better than
        raising and aborting the claim entirely.
        """
        try:
            project, task_number = _parse_task_id(task_id)
        except ValueError:
            return
        logger.warning(
            "provision_worker[%s]: reaping dead worker_session row "
            "(pane_id=%r) before re-provisioning (#1012)",
            task_id, session.pane_id,
        )
        try:
            mark_ended = getattr(self._svc, "mark_worker_session_ended", None)
            if callable(mark_ended):
                mark_ended(
                    task_project=project,
                    task_number=task_number,
                    ended_at=_now(),
                )
            else:
                # Pre-#1014 fallback for work-service stubs that don't
                # implement the token-preserving helper. We pass the
                # existing token totals back through ``end_worker_session``
                # so we still don't zero them out.
                self._svc.end_worker_session(
                    task_project=project,
                    task_number=task_number,
                    ended_at=_now(),
                    total_input_tokens=self._existing_session_token_in(
                        project, task_number,
                    ),
                    total_output_tokens=self._existing_session_token_out(
                        project, task_number,
                    ),
                    archive_path=None,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "provision_worker[%s]: failed to end stale worker_session: %s",
                task_id, exc,
            )

    def _existing_session_token_in(
        self, project: str, task_number: int,
    ) -> int:
        """Best-effort read of the current row's input-token counter.

        Used by the legacy fallback in :meth:`_reap_dead_existing_session`
        on work-service stubs that pre-date #1014's
        ``mark_worker_session_ended``. Returns 0 on any error so the
        reap still proceeds.
        """
        try:
            record = self._svc.get_worker_session(
                task_project=project, task_number=task_number,
            )
        except Exception:  # noqa: BLE001
            return 0
        if record is None:
            return 0
        return int(getattr(record, "total_input_tokens", 0) or 0)

    def _existing_session_token_out(
        self, project: str, task_number: int,
    ) -> int:
        """Best-effort read of the current row's output-token counter."""
        try:
            record = self._svc.get_worker_session(
                task_project=project, task_number=task_number,
            )
        except Exception:  # noqa: BLE001
            return 0
        if record is None:
            return 0
        return int(getattr(record, "total_output_tokens", 0) or 0)

    def _provision_locked(
        self,
        *,
        task_id: str,
        agent_name: str,
        project: str,
        task_number: int,
        task_slug: str,
        branch_name: str,
    ) -> WorkerSession:
        """Inner provision body — runs under the per-task session lock."""
        # Create worktree
        worktree_path = self._create_worktree(task_id, task_slug, self._project_path)

        # Build task prompt for the worker
        task_prompt = self._build_task_prompt(task_id, worktree_path)
        prompt_path = worktree_path / ".pollypm-task-prompt.md"
        try:
            prompt_path.parent.mkdir(parents=True, exist_ok=True)
            prompt_path.write_text(task_prompt)
        except OSError as exc:
            worktree_removed = self._remove_worktree(worktree_path)
            raise ProvisionError(
                f"Could not write the worker prompt at {prompt_path}: {exc}. "
                f"Why it matters: the worker launch depends on that file, so "
                f"starting the session would boot an uninitialized worker. "
                f"Fix: free up the filesystem or repair the worktree path, "
                f"then retry the claim. Cleanup "
                f"{'succeeded' if worktree_removed else 'failed'} for "
                f"{worktree_path}."
            )

        window_name = f"task-{task_slug}"
        session_name = self._storage_closet_name

        # Kickoff string sent after stabilization so the worker reads the
        # prompt file and signals done when finished.
        kickoff = (
            f"Read .pollypm-task-prompt.md, follow the instructions, "
            f"commit your work, then run `pm task done {task_id}`."
        )

        pane_id, provider_name, provider_home = self._launch_worker_window(
            session_name=session_name,
            window_name=window_name,
            worktree_path=worktree_path,
            agent_name=agent_name,
            project=project,
            kickoff=kickoff,
        )

        now = _now_dt()

        # Store binding through the work service. Upsert so a re-claim
        # after cancel reuses the row (teardown stamps ended_at but
        # doesn't delete) instead of hitting the PK constraint.
        # ``provider`` + ``provider_home`` (#809) let teardown locate
        # the right transcript tree without depending on the
        # supervisor process's ambient env.
        self._svc.upsert_worker_session(
            task_project=project,
            task_number=task_number,
            agent_name=agent_name,
            pane_id=pane_id,
            worktree_path=str(worktree_path),
            branch_name=branch_name,
            started_at=now.isoformat(),
            provider=provider_name,
            provider_home=str(provider_home) if provider_home else None,
        )

        return WorkerSession(
            task_id=task_id,
            agent_name=agent_name,
            pane_id=pane_id,
            worktree_path=worktree_path,
            branch_name=branch_name,
            started_at=now,
        )

    # ------------------------------------------------------------------
    # Worker launch helpers
    # ------------------------------------------------------------------

    def _worker_launch_bundle(
        self,
        *,
        worktree_path: Path,
        agent_name: str,
        window_name: str,
        project: str,
    ) -> tuple[str, str, str, Path | None, Path | None]:
        """Return ``(command, provider_name, account_name, fresh_marker, provider_home)``.

        ``provider_home`` is the account's isolated config root (e.g.
        ``CLAUDE_CONFIG_DIR`` or ``CODEX_HOME``). Persisted on the
        worker session row so per-task transcript archival can find
        the right tree at teardown without depending on the supervisor
        process's ambient env (#809). ``None`` means the account has
        no isolated home and the legacy ambient-env fallback applies.

        When config is available we route through the configured
        provider/runtime adapter pair so worker launch obeys the same
        account, runtime, and permissions policy as the main planner.
        Tests that construct ``SessionManager`` without config keep the
        historical Claude fallback.
        """
        if self._config is None:
            legacy_cmd = (
                f"cd {_shell_quote(str(worktree_path))} && "
                f"claude --dangerously-skip-permissions"
            )
            # Pre-#809 fallback: no account → no provider_home, so
            # archival reads ambient env (legacy behaviour).
            return legacy_cmd, "claude", agent_name, None, None

        from pollypm.models import SessionConfig
        from pollypm.onboarding import default_session_args
        from pollypm.providers import get_provider
        from pollypm.runtimes import get_runtime
        from pollypm.role_routing import resolved_provider_kind, resolve_role_assignment

        config = self._config
        accounts = getattr(config, "accounts", {}) or {}
        routed_assignment = resolve_role_assignment("worker", project, config=config)
        try:
            routed_provider = resolved_provider_kind(routed_assignment)
        except ValueError as exc:
            raise ProvisionError(
                f"Worker routing for project {project} resolved an unknown provider "
                f"{routed_assignment.provider!r}: {exc}"
            ) from exc
        preferred_names = [
            agent_name if agent_name in accounts else "",
            getattr(getattr(config, "pollypm", None), "controller_account", ""),
        ]
        if routed_assignment.source == "fallback":
            legacy_name = preferred_names[0] or preferred_names[1]
            account_name = legacy_name
            account = accounts.get(account_name)
            if account is None and accounts:
                account_name, account = next(iter(accounts.items()))
        else:
            account_name, account = _first_matching_account(
                accounts,
                provider=routed_provider,
                preferred_names=preferred_names,
            )
        if account is None:
            if accounts:
                raise ProvisionError(
                    "Could not provision a worker because no configured account "
                    f"is available for routed provider {routed_provider.value}. "
                    "Why it matters: the worker session has no compatible "
                    "provider/runtime to start under. "
                    f"Fix: add at least one {routed_provider.value} account to "
                    "PollyPM config, then retry the claim."
                )
            raise ProvisionError(
                "Could not provision a worker because no configured account "
                "is available for provider/runtime launch planning. "
                "Why it matters: the worker session has no provider or "
                "runtime to start under. "
                "Fix: add at least one account to PollyPM config, then "
                "retry the claim."
            )
        session_provider = account.provider
        session_model: str | None = None
        if account.provider is routed_provider:
            session_provider = routed_provider
            session_model = routed_assignment.model
            logger.info(
                "Role routing resolved worker for %s to %s/%s from %s.",
                project,
                routed_assignment.provider,
                routed_assignment.model,
                routed_assignment.source,
            )

        session = SessionConfig(
            name=window_name,
            role="worker",
            provider=session_provider,
            account=account_name,
            cwd=worktree_path,
            project=project,
            window_name=window_name,
            args=default_session_args(
                session_provider,
                open_permissions=config.pollypm.open_permissions_by_default,
                role="worker",
                model=session_model,
            ),
        )
        provider = get_provider(session_provider, root_dir=config.project.root_dir)
        runtime = get_runtime(account.runtime, root_dir=config.project.root_dir)
        launch = provider.build_launch_command(session, account)
        command = runtime.wrap_command(launch, account, config.project)
        provider_home = getattr(account, "home", None)
        provider_home_path = Path(provider_home) if provider_home else None
        return (
            command,
            account.provider.value,
            account_name,
            launch.fresh_launch_marker,
            provider_home_path,
        )

    def _launch_worker_window(
        self,
        *,
        session_name: str,
        window_name: str,
        worktree_path: Path,
        agent_name: str,
        project: str,
        kickoff: str,
    ) -> tuple[str, str | None, Path | None]:
        """Launch an interactive Claude window.

        Returns ``(pane_id, provider_name, provider_home)`` so the
        caller can persist the provider context on the work_session
        row for #809-aware teardown.

        When a ``SessionService`` is configured, route through it so the
        window picks up stabilization, initial-input verification and
        other robustness we already implemented there. Otherwise fall
        back to raw tmux operations (used by unit tests that inject a
        mock TmuxClient).
        """
        (
            worker_cmd,
            provider_name,
            account_name,
            fresh_launch_marker,
            provider_home,
        ) = self._worker_launch_bundle(
            worktree_path=worktree_path,
            agent_name=agent_name,
            window_name=window_name,
            project=project,
        )

        # Clear any stale window of the same name (e.g. a dead pane from
        # a previous run that lingered because remain-on-exit=on). Without
        # this, create_window is a no-op when the name already exists.
        self._kill_stale_task_window(session_name, window_name)

        if self._session_service is not None:
            from pollypm.project_guides import worker_guide_reference

            # Use a fresh_launch_marker so SessionService.create() knows
            # to send the kickoff as initial input.
            marker_dir = self._project_path / ".pollypm" / "worker-markers"
            marker_dir.mkdir(parents=True, exist_ok=True)
            marker_path = marker_dir / f"{window_name}.fresh"
            marker_create_status = "ok"
            marker_create_error: str | None = None
            try:
                marker_path.write_text(_now())
            except OSError as marker_exc:
                logger.warning("Could not write fresh_launch_marker for %s", window_name)
                marker_create_status = "error"
                marker_create_error = str(marker_exc)
            # Audit hook (#savethenovel) — markers are the spine of the
            # cockpit's worker affordance; an orphan marker without a
            # matching ``work_tasks`` row is exactly the symptom we
            # need to detect post-wipe. Emit both the success and the
            # failure path so a leaked-marker condition is visible.
            try:
                from pollypm.audit import emit as _audit_emit
                from pollypm.audit.log import (
                    EVENT_MARKER_CREATED,
                    EVENT_MARKER_CREATE_FAILED,
                )

                _audit_emit(
                    event=(
                        EVENT_MARKER_CREATED
                        if marker_create_status == "ok"
                        else EVENT_MARKER_CREATE_FAILED
                    ),
                    project=project,
                    subject=str(marker_path),
                    actor=agent_name or "system",
                    status=marker_create_status,
                    metadata={
                        "window_name": window_name,
                        "marker_kind": "fresh",
                        "error": marker_create_error,
                    },
                    project_path=self._project_path,
                )
            except Exception:  # noqa: BLE001 — audit must never block launch
                pass
            fresh_launch_marker = fresh_launch_marker or marker_path

            handle = self._session_service.create(
                name=window_name,
                provider=provider_name,
                account=account_name,
                cwd=worktree_path,
                command=worker_cmd,
                window_name=window_name,
                tmux_session=session_name,
                stabilize=True,
                initial_input=kickoff,
                fresh_launch_marker=fresh_launch_marker,
                session_role="worker",
                guide_reference=worker_guide_reference(self._project_path),
            )
            return handle.pane_id or "", provider_name, provider_home

        # Fallback: raw tmux client path. Used by tests that construct
        # SessionManager without a session_service.
        #
        # Robustness: if the storage-closet session doesn't appear to
        # exist, try ``create_session`` first — but if that fails because
        # the session was in fact already present (race, inconsistent
        # ``has_session`` result, or exotic tmux state), fall back to
        # ``create_window`` rather than raising. This is the #243 fix:
        # ``pm task claim`` was hard-erroring on a benign "session
        # already exists" condition.
        if not self._tmux.has_session(session_name):
            try:
                self._tmux.create_session(session_name, window_name, worker_cmd)
            except _CalledProcessError as exc:
                # Most common reason: someone else just created the
                # session. Verify and fall back to new-window.
                if self._tmux.has_session(session_name):
                    logger.info(
                        "provision_worker: new-session raced for %r; "
                        "falling back to new-window",
                        session_name,
                    )
                    self._tmux.create_window(
                        session_name, window_name, worker_cmd, detached=True,
                    )
                else:
                    # Genuinely couldn't create the session. Re-raise
                    # with a more actionable error — caller (sqlite
                    # service's claim() path) will surface this to the
                    # user instead of silently logging.
                    raise ProvisionError(
                        f"Could not create tmux session '{session_name}': "
                        f"{exc}. "
                        f"Check that tmux is installed and running, then "
                        f"run `pm task release <id>` and retry."
                    ) from exc
            pane_id = self._resolve_worker_pane_id(
                session_name=session_name,
                window_name=window_name,
                windows=self._tmux.list_windows(session_name),
            )
        else:
            self._tmux.create_window(session_name, window_name, worker_cmd, detached=True)
            pane_id = self._resolve_worker_pane_id(
                session_name=session_name,
                window_name=window_name,
                windows=self._tmux.list_windows(session_name),
            )
        return pane_id, provider_name, provider_home

    def _resolve_worker_pane_id(self, *, session_name: str, window_name: str, windows) -> str:
        """Resolve the pane id for a freshly created worker window.

        If tmux cannot report any windows after create-session/create-window,
        fail closed so the caller can retry instead of persisting a phantom
        ``%0`` pane binding.
        """

        for window in windows:
            if window.name == window_name and getattr(window, "pane_id", ""):
                return window.pane_id
        for window in windows:
            pane_id = getattr(window, "pane_id", "")
            if pane_id:
                return pane_id
        raise ProvisionError(
            f"tmux did not report a worker pane for session '{session_name}' "
            f"window '{window_name}'. Retry `pm task claim <id>`."
        )

    def _kill_stale_task_window(self, session_name: str, window_name: str) -> None:
        """Kill a stale ``task-<slug>`` window before re-provisioning.

        Because the storage-closet session is created with
        ``remain-on-exit=on``, a prior worker's dead pane can linger in
        the window and cause ``create_window`` to no-op. We defensively
        clear it here.
        """
        try:
            if not self._tmux.has_session(session_name):
                return
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "_kill_stale_task_window: has_session(%s) failed: %s",
                session_name,
                exc,
            )
            return

        try:
            windows = self._tmux.list_windows(session_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "_kill_stale_task_window: list_windows(%s) failed: %s",
                session_name,
                exc,
            )
            return

        for w in windows:
            if getattr(w, "name", None) == window_name:
                target = f"{session_name}:{window_name}"
                try:
                    self._tmux.kill_window(target)
                    logger.debug("Cleared stale task window %s", target)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Failed to clear stale task window %s: %s", target, exc,
                    )
                return

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def teardown_worker(self, task_id: str) -> TeardownResult:
        """Archive JSONL, record tokens, kill session, clean worktree.

        Idempotent. Each phase (archive, kill window, remove worktree)
        is isolated so a failure in one doesn't prevent the others from
        running. ``ended_at`` is only stamped if the tmux window was
        confirmed killed (or there was no pane to begin with); that way
        a future pass can detect and re-attempt teardown of a session
        whose pane we failed to kill.
        """
        project, task_number = _parse_task_id(task_id)

        record = self._svc.get_worker_session(
            task_project=project, task_number=task_number,
        )
        if record is None or record.ended_at is not None:
            # Already torn down or never existed
            return TeardownResult(
                task_id=task_id,
                jsonl_archived=False,
                archive_path=None,
                total_input_tokens=0,
                total_output_tokens=0,
                worktree_removed=False,
            )

        pane_id = record.pane_id
        worktree_path = Path(record.worktree_path) if record.worktree_path else None

        # Phase 1: Archive JSONL. Don't let a copy/parse failure block
        # the rest of teardown.
        archive_path: Path | None = None
        input_tokens = 0
        output_tokens = 0
        jsonl_archived = False
        if worktree_path is not None:
            try:
                archive_path, input_tokens, output_tokens = self._archive_jsonl(
                    task_id,
                    worktree_path,
                    provider=record.provider,
                    provider_home=Path(record.provider_home)
                    if record.provider_home else None,
                )
                jsonl_archived = archive_path is not None
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "teardown_worker[%s]: archive phase failed: %s",
                    task_id, exc,
                )

        # Phase 2: Kill tmux window (not just the pane). With
        # remain-on-exit=on on the storage-closet session, kill_pane
        # alone leaves a dead pane in a named window that blocks
        # subsequent create_window calls.
        task_slug = f"{project}-{task_number}"
        window_name = f"task-{task_slug}"
        window_target = f"{self._storage_closet_name}:{window_name}"
        pane_killed = False
        if not pane_id:
            # Nothing to kill — treat as already dead.
            pane_killed = True
        else:
            try:
                self._tmux.kill_window(window_target)
                pane_killed = True
            except _CalledProcessError:
                # tmux returned non-zero, typically "window not found"
                # which means it's already gone.
                logger.debug(
                    "Window %s already gone during teardown", window_target,
                )
                pane_killed = True
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "teardown_worker[%s]: kill_window phase failed for %s: %s",
                    task_id, window_target, exc,
                )

        # Phase 3: Remove worktree.
        worktree_removed = False
        if worktree_path is not None:
            try:
                worktree_removed = self._remove_worktree(worktree_path)
                if not worktree_removed:
                    logger.warning(
                        "teardown_worker[%s]: worktree removal returned "
                        "False for %s (see earlier warnings)",
                        task_id, worktree_path,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "teardown_worker[%s]: remove_worktree phase failed "
                    "for %s: %s",
                    task_id, worktree_path, exc,
                )

        # Only stamp ended_at when the pane was confirmed killed. If the
        # kill phase failed, leave ended_at=NULL so a future pass can
        # retry — but still record the archive results for observability.
        archive_str = str(archive_path) if archive_path else None
        if pane_killed:
            self._svc.end_worker_session(
                task_project=project,
                task_number=task_number,
                ended_at=_now(),
                total_input_tokens=input_tokens,
                total_output_tokens=output_tokens,
                archive_path=archive_str,
            )
        else:
            self._svc.update_worker_session_tokens(
                task_project=project,
                task_number=task_number,
                total_input_tokens=input_tokens,
                total_output_tokens=output_tokens,
                archive_path=archive_str,
            )

        return TeardownResult(
            task_id=task_id,
            jsonl_archived=jsonl_archived,
            archive_path=archive_path,
            total_input_tokens=input_tokens,
            total_output_tokens=output_tokens,
            worktree_removed=worktree_removed,
        )

    # ------------------------------------------------------------------
    # Rejection
    # ------------------------------------------------------------------

    def notify_rejection(self, task_id: str, reason: str) -> bool:
        """Send rejection feedback to the worker's live pane.

        With interactive-launch workers the pane is still alive after a
        rejection, so we simply feed the feedback as keystrokes. No more
        spawning a fresh ``claude -p`` session — that workaround existed
        only because headless workers exited before we could talk to
        them.

        Returns True if feedback was delivered.
        """
        session = self.session_for_task(task_id)
        if session is None:
            logger.warning(
                "notify_rejection: no active worker session for %s", task_id,
            )
            return False

        if not self._tmux.is_pane_alive(session.pane_id):
            logger.warning(
                "notify_rejection: pane %s for task %s is not alive; "
                "cannot deliver feedback",
                session.pane_id,
                task_id,
            )
            return False

        message = (
            f"Your work on this task was rejected by the reviewer. "
            f"Reason: {reason}. "
            f"Please address the feedback and signal done when ready."
        )
        try:
            self._tmux.send_keys(session.pane_id, message)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "notify_rejection: failed to send rejection to pane %s for %s: %s",
                session.pane_id,
                task_id,
                exc,
            )
            return False

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def active_sessions(self, project: str | None = None) -> list[WorkerSession]:
        """List all active worker sessions, optionally filtered by project."""
        records = self._svc.list_worker_sessions(project=project, active_only=True)
        return [self._record_to_session(r) for r in records]

    def session_for_task(self, task_id: str) -> WorkerSession | None:
        """Get the worker session bound to a task, or None."""
        project, task_number = _parse_task_id(task_id)
        record = self._svc.get_worker_session(
            task_project=project, task_number=task_number, active_only=True,
        )
        if record is None:
            return None
        return self._record_to_session(record)

    # ------------------------------------------------------------------
    # JSONL archival
    # ------------------------------------------------------------------

    def _archive_jsonl(
        self,
        task_id: str,
        worktree_path: Path,
        *,
        provider: str | None = None,
        provider_home: Path | None = None,
    ) -> tuple[Path | None, int, int]:
        """Copy provider JSONL transcripts to archive location.

        Claude Code writes transcripts to
        ``$CLAUDE_CONFIG_DIR/projects/<encoded-cwd>/*.jsonl`` (default
        ``~/.claude/projects/...``). When the worker was launched with
        an isolated account home, that path is the worker's
        ``provider_home``, which the supervisor process running
        teardown does not have in its ambient env. Pre-#809 the
        archive scan used ``os.environ`` directly, so isolated Claude
        accounts and Codex workers landed on the wrong tree (or no
        tree at all) and the archive came back empty.

        Use the provider/home that the launcher persisted on the
        worker session row when available; fall back to the legacy
        ambient-env path so old session rows still archive.

        Returns (archive_path, input_tokens, output_tokens).
        """
        jsonl_files = _find_provider_jsonl_files(
            worktree_path,
            provider=provider,
            provider_home=provider_home,
        )
        if not jsonl_files:
            return None, 0, 0

        # Create archive directory
        archive_dir = (
            self._project_path / ".pollypm" / "transcripts" / "tasks" / task_id
        )
        archive_dir.mkdir(parents=True, exist_ok=True)

        total_input = 0
        total_output = 0

        for src in jsonl_files:
            dst = archive_dir / src.name
            try:
                shutil.copy2(src, dst)
            except OSError as exc:
                logger.warning("Failed to archive %s: %s", src, exc)
                continue

            # Parse token counts
            input_t, output_t = _parse_token_usage(src)
            total_input += input_t
            total_output += output_t

        return archive_dir, total_input, total_output

    # ------------------------------------------------------------------
    # Worktree management
    # ------------------------------------------------------------------

    def _create_worktree(
        self, task_id: str, task_slug: str, project_path: Path
    ) -> Path:
        """``git worktree add``. Returns the worktree path. Idempotent.

        Verifies the path is actually registered with git before the
        exists() fast-path returns — a crash on a previous run can leave
        a dangling directory that isn't a real worktree. All subprocess
        calls have timeouts so a hung git op can't wedge claim().
        """
        worktree_path = project_path / ".pollypm" / "worktrees" / task_slug
        branch_name = f"task/{task_slug}"

        if worktree_path.exists():
            if _worktree_is_registered(project_path, worktree_path):
                return worktree_path
            # Dangling directory from a crashed run. Prune stale metadata
            # and remove the directory so `git worktree add` can succeed.
            logger.warning(
                "Worktree path %s exists but is not registered; pruning "
                "and re-adding",
                worktree_path,
            )
            subprocess.run(
                ["git", "-C", str(project_path), "worktree", "prune"],
                check=False,
                text=True,
                capture_output=True,
                timeout=60,
            )
            if worktree_path.exists():
                # Directory is still there after prune — nuke it.
                try:
                    shutil.rmtree(worktree_path)
                except OSError as exc:
                    raise RuntimeError(
                        f"Could not remove dangling worktree dir "
                        f"{worktree_path}: {exc}"
                    ) from exc

        worktree_path.parent.mkdir(parents=True, exist_ok=True)

        result = subprocess.run(
            [
                "git", "-C", str(project_path),
                "worktree", "add",
                str(worktree_path),
                "-b", branch_name,
            ],
            check=False,
            text=True,
            capture_output=True,
            timeout=300,
        )
        if result.returncode != 0:
            # Branch may already exist; try without -b
            result = subprocess.run(
                [
                    "git", "-C", str(project_path),
                    "worktree", "add",
                    str(worktree_path),
                    branch_name,
                ],
                check=False,
                text=True,
                capture_output=True,
                timeout=300,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"git worktree add failed: {result.stderr.strip()}"
                )

        return worktree_path

    def _remove_worktree(self, worktree_path: Path) -> bool:
        """``git worktree remove``. Returns True if removed. Idempotent."""
        if not worktree_path.exists():
            return True

        result = subprocess.run(
            [
                "git", "-C", str(self._project_path),
                "worktree", "remove", "--force",
                str(worktree_path),
            ],
            check=False,
            text=True,
            capture_output=True,
            timeout=300,
        )
        if result.returncode != 0:
            logger.warning(
                "Failed to remove worktree %s: %s",
                worktree_path,
                result.stderr.strip(),
            )
            return False

        # Prune stale worktree metadata
        subprocess.run(
            ["git", "-C", str(self._project_path), "worktree", "prune"],
            check=False,
            text=True,
            capture_output=True,
            timeout=60,
        )
        return True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_task_prompt(self, task_id: str, worktree_path: Path) -> str:
        """Build a clear operating prompt for a per-task worker session."""
        from pollypm.project_guides import worker_guide_reference

        # Get task details from the work service
        task_info = ""
        try:
            task = self._svc.get(task_id)
            task_info = (
                f"## Your Assignment\n\n"
                f"**Task {task.task_id}**: {task.title}\n\n"
            )
            if task.description:
                task_info += f"**Description:**\n{task.description}\n\n"
            if task.acceptance_criteria:
                task_info += f"**Acceptance Criteria:**\n{task.acceptance_criteria}\n\n"
        except Exception:
            task_info = f"## Your Assignment\n\nTask ID: {task_id}\n\n"

        guide_reference = worker_guide_reference(self._project_path)

        # When there's no project-local guide, point the worker at the
        # `pm help worker` meta-command — it prints the playbook from a
        # packaged resource and works in any worktree, even one without
        # docs/ on disk (savethenovel fix). When a project has its own
        # forked guide, name that file directly so the worker reads
        # the local override.
        if guide_reference == "docs/worker-guide.md":
            guide_instruction = (
                "Run `pm help worker` first for the worker playbook. "
                "(`pm help worker` prints the guide from PollyPM's "
                "packaged resources, so it works in any worktree.)"
            )
        else:
            guide_instruction = (
                f"Read `{guide_reference}` first for the worker playbook "
                f"(project-local override). If that file is missing, run "
                f"`pm help worker` for the built-in guide."
            )

        return (
            f"You are a PollyPM task worker. You have one job: complete the task below.\n\n"
            f"You are working in a git worktree at: {worktree_path}\n"
            f"This is an isolated branch — your changes won't affect other workers.\n\n"
            f"{task_info}"
            f"## Worker Guide\n\n"
            f"{guide_instruction}\n\n"
            f"## How to Work\n\n"
            f"1. Read the task description carefully\n"
            f"2. Implement the work: read code, write code, run tests, commit\n"
            f"3. When done, signal completion:\n"
            f"   ```\n"
            f"   pm task done {task_id} -o '{{"
            f'"type": "code_change", '
            f'"summary": "what you did", '
            f'"artifacts": [{{"kind": "commit", "ref": "<hash>", "description": "..."}}]'
            f"}}'\n"
            f"   ```\n\n"
            f"## Work Output Types\n\n"
            f"- **type**: code_change | action | document | mixed\n"
            f"- **artifacts**: list of concrete outputs\n"
            f"  - commit: `{{\"kind\": \"commit\", \"ref\": \"<hash>\", \"description\": \"...\"}}`\n"
            f"  - file_change: `{{\"kind\": \"file_change\", \"path\": \"...\", \"description\": \"...\"}}`\n"
            f"  - action: `{{\"kind\": \"action\", \"description\": \"...\"}}`\n\n"
            f"## Rules\n\n"
            f"- Stay focused on this task only\n"
            f"- Commit your work before signaling done\n"
            f"- The quality bar is high — test before shipping\n"
            f"- If blocked, add a context note: `pm task context {task_id} \"blocked on X\"`\n\n"
            f"## Common Review Failures (avoid these)\n\n"
            f"- pyproject.toml: always use `build-backend = \"setuptools.build_meta\"` "
            f"(NOT setuptools.backends._legacy:_Backend)\n"
            f"- pyproject.toml: if using setuptools, add "
            f"`[tool.setuptools.packages.find]` with `exclude = [\"issues*\", \"docs*\", \"tests*\"]` "
            f"to prevent the issues/ directory from being treated as a package\n"
            f"- Verify your code actually runs (`uv run python -m <package>`) before signaling done\n"
            f"- Check every acceptance criterion individually\n"
            f"- Include proper error handling for CLI tools\n"
        )

    @staticmethod
    def _record_to_session(record) -> WorkerSession:
        return WorkerSession(
            task_id=f"{record.task_project}/{record.task_number}",
            agent_name=record.agent_name,
            pane_id=record.pane_id or "",
            worktree_path=Path(record.worktree_path) if record.worktree_path else Path(),
            branch_name=record.branch_name or "",
            started_at=datetime.fromisoformat(record.started_at),
        )


# ---------------------------------------------------------------------------
# Free helpers
# ---------------------------------------------------------------------------


def _parse_task_id(task_id: str) -> tuple[str, int]:
    """Parse ``'project/number'`` into (project, task_number)."""
    parts = task_id.rsplit("/", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid task_id: {task_id}")
    return parts[0], int(parts[1])


def _shell_quote(s: str) -> str:
    """Shell-quote a string for embedding in a tmux command."""
    import shlex
    return shlex.quote(s)


def _worktree_is_registered(project_path: Path, worktree_path: Path) -> bool:
    """Return True if ``worktree_path`` is a registered git worktree of
    ``project_path``.

    Uses ``git worktree list --porcelain`` and compares resolved paths so
    a directory left over from a crashed run (present but unregistered)
    can be detected and cleaned up before we try to ``git worktree add``.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(project_path), "worktree", "list", "--porcelain"],
            check=False,
            text=True,
            capture_output=True,
            timeout=60,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning(
            "git worktree list failed for %s: %s", project_path, exc,
        )
        return False

    if result.returncode != 0:
        logger.warning(
            "git worktree list exit %d for %s: %s",
            result.returncode, project_path, result.stderr.strip(),
        )
        return False

    try:
        target = worktree_path.resolve()
    except OSError:
        target = worktree_path

    for line in result.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        registered = line[len("worktree "):].strip()
        try:
            registered_resolved = Path(registered).resolve()
        except OSError:
            registered_resolved = Path(registered)
        if registered_resolved == target:
            return True
    return False


def _encode_claude_cwd(cwd: Path) -> str:
    """Encode a cwd path the way Claude Code does for project JSONL dirs.

    Claude replaces every ``/`` and ``.`` in the absolute cwd with ``-``,
    e.g. ``/Users/sam/dev/foo/.pollypm/worktrees/foo-1`` becomes
    ``-Users-sam-dev-foo--pollypm-worktrees-foo-1``.
    """
    s = str(cwd)
    return s.replace("/", "-").replace(".", "-")


def _claude_config_dir(home: Path | None = None) -> Path:
    """Resolve the Claude config directory.

    Honors an explicit ``home`` (the account-isolated home persisted
    on the worker session row, #809), then ``$CLAUDE_CONFIG_DIR`` (set
    per-account by the runtime), then falls back to ``~/.claude``.
    """
    if home is not None:
        # Per ``providers/claude/env.py``, isolated env points
        # ``CLAUDE_CONFIG_DIR`` at ``<home>/.claude``.
        return Path(home) / ".claude"
    env_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if env_dir:
        return Path(env_dir)
    return Path.home() / ".claude"


def _codex_home_dir(home: Path | None = None) -> Path:
    """Resolve the Codex home directory.

    Mirrors :func:`_claude_config_dir`: account-isolated home wins,
    then ``$CODEX_HOME``, then ``~/.codex``.
    """
    if home is not None:
        return Path(home) / ".codex"
    env_dir = os.environ.get("CODEX_HOME")
    if env_dir:
        return Path(env_dir)
    return Path.home() / ".codex"


def _find_claude_jsonl_files(
    worktree_path: Path, *, account_home: Path | None = None,
) -> list[Path]:
    """Return JSONL files Claude wrote for a worktree cwd.

    Looks in ``<config_dir>/projects/<encoded-cwd>/*.jsonl``. When
    ``account_home`` is supplied it identifies the account-isolated
    config root (#809); otherwise the resolver falls back to ambient
    env then ``~/.claude``.
    """
    projects_root = _claude_config_dir(home=account_home) / "projects"
    if not projects_root.exists():
        return []

    encoded = _encode_claude_cwd(worktree_path.resolve())
    project_dir = projects_root / encoded
    if not project_dir.exists():
        return []

    return sorted(project_dir.glob("*.jsonl"))


def _codex_rollout_cwd(jsonl_path: Path) -> str | None:
    """Return the cwd a Codex rollout was recorded under, if any.

    Codex writes ``session_meta`` (and per-turn ``turn_context``) rows
    that carry ``payload.cwd`` — the cwd the rollout actually
    executed in. The transcript-ledger / transcript-ingest paths
    already parse this shape; the archival finder needs the same
    contract so a task's per-task archive only includes that task's
    rollout (#812). Returns the first cwd seen, or ``None`` if the
    rollout has no recognisable cwd row.
    """
    try:
        with jsonl_path.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                kind = obj.get("type")
                if kind not in ("session_meta", "turn_context"):
                    continue
                payload = obj.get("payload") or {}
                if not isinstance(payload, dict):
                    continue
                cwd = payload.get("cwd")
                if isinstance(cwd, str) and cwd:
                    return cwd
    except OSError:
        return None
    return None


def _find_codex_jsonl_files(
    worktree_path: Path, *, account_home: Path | None = None,
) -> list[Path]:
    """Return JSONL files Codex wrote for *this* worker run.

    Codex writes session transcripts to
    ``<CODEX_HOME>/sessions/<YYYY>/<MM>/<DD>/rollout-*.jsonl``. The
    layout isn't cwd-bucketed, so a single account home holds
    rollouts for the operator, the architect, and every task that
    has ever run under that account. Pre-#812 the finder returned
    every rollout, which contaminated per-task archives with
    unrelated transcripts and would over-count tokens once the
    Codex token parser fixed in #813 lands.

    Filter by reading each rollout's ``session_meta`` /
    ``turn_context`` cwd and keeping only the rollouts whose cwd
    matches ``worktree_path.resolve()``. Rollouts that have no
    parseable cwd row are dropped — without it we can't prove the
    rollout belongs to this task. Best-effort: missing tree returns
    an empty list.
    """
    sessions_root = _codex_home_dir(home=account_home) / "sessions"
    if not sessions_root.exists():
        return []
    # ``rglob`` walks Codex's nested year/month/day layout without
    # this module having to know the exact shape.
    candidates = sorted(sessions_root.rglob("*.jsonl"))
    target = str(worktree_path.resolve())
    return [path for path in candidates if _codex_rollout_cwd(path) == target]


def _find_provider_jsonl_files(
    worktree_path: Path,
    *,
    provider: str | None,
    provider_home: Path | None,
) -> list[Path]:
    """Return the JSONL transcript files for a worker.

    Dispatches by ``provider`` (#809):
    * ``claude`` → :func:`_find_claude_jsonl_files`.
    * ``codex`` → :func:`_find_codex_jsonl_files`.
    * unknown / missing → fall back to the Claude scan against
      ambient env, matching pre-#809 behaviour.
    """
    if provider == "codex":
        return _find_codex_jsonl_files(
            worktree_path, account_home=provider_home,
        )
    return _find_claude_jsonl_files(
        worktree_path, account_home=provider_home,
    )


def _parse_token_usage(jsonl_path: Path) -> tuple[int, int]:
    """Parse token usage from a provider JSONL transcript.

    Returns (input_tokens, output_tokens).

    Claude shape: rows with ``type=="token_usage"``, or a top-level
    ``usage`` dict, or ``message.usage``. Counters are
    ``input_tokens`` / ``output_tokens``.

    Codex shape (#813): ``event_msg`` rows whose payload is
    ``type=="token_count"`` with token data under
    ``payload.info.last_token_usage`` (per-turn) or
    ``total_token_usage`` (cumulative). Codex emits a fresh
    ``last_token_usage`` per turn, so summing those preserves the
    Claude-shaped per-turn-accumulation contract. ``input_tokens``
    is summed with ``cached_input_tokens``; ``output_tokens`` is
    summed with ``reasoning_output_tokens`` so reasoning-heavy
    Codex turns don't undercount cost.
    """
    input_tokens = 0
    output_tokens = 0

    try:
        with open(jsonl_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(data, dict):
                    continue

                inc_in, inc_out = _extract_token_usage(data)
                input_tokens += inc_in
                output_tokens += inc_out
    except OSError as exc:
        logger.warning(
            "Could not read JSONL file %s for token parsing: %s",
            jsonl_path, exc,
        )

    return input_tokens, output_tokens


def _extract_token_usage(data: dict) -> tuple[int, int]:
    """Pull (input_tokens, output_tokens) from one transcript row.

    Tolerates both Claude- and Codex-shaped rows. Returns ``(0, 0)``
    for rows with no recognisable usage data.

    Codex ``event_msg`` / ``token_count`` rows can land in any of
    these shapes (#814):

    * ``info.last_token_usage`` with split counters (``input_tokens``,
      ``cached_input_tokens``, ``output_tokens``,
      ``reasoning_output_tokens``) — per-turn delta, the common case.
    * ``info.last_token_usage`` with only ``total_tokens`` — no
      split available.
    * ``info.total_token_usage`` only (no ``last_token_usage``) —
      cumulative snapshot, fallback shape.

    The transcript_ledger / transcript_ingest readers already use
    ``last_usage = info.last_token_usage or info.total_token_usage``
    and accept ``total_tokens`` when split fields are absent. The
    archival parser now follows the same contract so all three
    shapes contribute to ``work_sessions.total_input_tokens`` /
    ``total_output_tokens`` instead of being silently dropped.

    When a row only carries ``total_tokens`` (no input/output split)
    we attribute the value to ``output_tokens``: Codex rollouts are
    output-heavy once reasoning is included, and dropping the value
    would leave the per-task token totals at zero — under-reporting
    is the worse failure for cost dashboards. ``work_sessions``
    schema only stores input + output today (#809); a richer split
    can land later without changing this contract.
    """
    # Codex ``event_msg`` rows carry token data under
    # ``payload.info.last_token_usage`` (per-turn) or
    # ``payload.info.total_token_usage`` (cumulative).
    if data.get("type") == "event_msg":
        payload = data.get("payload") or {}
        if isinstance(payload, dict) and payload.get("type") == "token_count":
            info = payload.get("info") or {}
            if isinstance(info, dict):
                last_usage = info.get("last_token_usage")
                total_usage = info.get("total_token_usage")
                # Mirror transcript_ledger: prefer per-turn ``last``;
                # fall back to ``total_token_usage`` so cumulative-only
                # rows don't silently drop to (0, 0).
                usage = last_usage if isinstance(last_usage, dict) else total_usage
                if isinstance(usage, dict):
                    has_split = any(
                        key in usage
                        for key in (
                            "input_tokens",
                            "cached_input_tokens",
                            "output_tokens",
                            "reasoning_output_tokens",
                        )
                    )
                    if has_split:
                        inp = int(usage.get("input_tokens", 0) or 0)
                        inp += int(usage.get("cached_input_tokens", 0) or 0)
                        out = int(usage.get("output_tokens", 0) or 0)
                        out += int(usage.get("reasoning_output_tokens", 0) or 0)
                        return inp, out
                    total = usage.get("total_tokens")
                    if total is not None:
                        # No split available. Bucket the whole total
                        # into output so the row still contributes —
                        # see docstring for rationale.
                        return 0, int(total or 0)
            return 0, 0

    # Claude shapes — direct ``token_usage`` event, top-level ``usage``,
    # or ``message.usage``.
    if data.get("type") == "token_usage":
        usage = data
    elif "usage" in data:
        usage = data["usage"]
    elif "message" in data and isinstance(data["message"], dict):
        usage = data["message"].get("usage")
    else:
        usage = None
    if isinstance(usage, dict):
        return (
            int(usage.get("input_tokens", 0) or 0),
            int(usage.get("output_tokens", 0) or 0),
        )
    return 0, 0
