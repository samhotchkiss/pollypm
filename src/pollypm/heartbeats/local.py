from __future__ import annotations

import logging
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pollypm.heartbeats.base import HeartbeatBackend, HeartbeatSessionContext
from pollypm.recovery.base import (
    InterventionHistoryEntry,
    SessionHealth,
    SessionSignals,
)
from pollypm.recovery.default import DefaultRecoveryPolicy

logger = logging.getLogger(__name__)


def _registered_worktree_head(project_path: Path, worktree_path: str) -> str | None:
    """Return the registered HEAD OID for ``worktree_path`` under ``project_path``.

    The heartbeat only needs read-only metadata, so it resolves the worktree
    against the project's own git worktree registry instead of running git in
    the claimed worktree directory.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(project_path), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "work-signal: git worktree list failed for %s",
            project_path, exc_info=True,
        )
        return None

    if result.returncode != 0:
        logger.debug(
            "work-signal: git worktree list exited %d for %s: %s",
            result.returncode,
            project_path,
            result.stderr.strip(),
        )
        return None

    try:
        target = Path(worktree_path).resolve()
    except OSError:
        target = Path(worktree_path)

    registered_path: Path | None = None
    registered_head: str | None = None
    for line in result.stdout.splitlines():
        if not line:
            if registered_path == target and registered_head:
                return registered_head
            registered_path = None
            registered_head = None
            continue
        if line.startswith("worktree "):
            registered = line[len("worktree "):].strip()
            try:
                registered_path = Path(registered).resolve()
            except OSError:
                registered_path = Path(registered)
            registered_head = None
            continue
        if registered_path is not None and line.startswith("HEAD "):
            registered_head = line[len("HEAD "):].strip()

    if registered_path == target and registered_head:
        return registered_head
    return None


def _last_commit_age_for_worktree(
    project_path: Path,
    worktree_path: str,
    *,
    now: datetime,
) -> int | None:
    """Return the last commit age for a registered worktree, if available."""
    head = _registered_worktree_head(project_path, worktree_path)
    if not head:
        logger.debug(
            "work-signal: skipping git log for unregistered worktree %s",
            worktree_path,
        )
        return None

    try:
        result = subprocess.run(
            ["git", "-C", str(project_path), "log", "-1", "--format=%ct", head],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "work-signal: git log failed for registered worktree %s",
            worktree_path, exc_info=True,
        )
        return None

    if result.returncode != 0 or not result.stdout.strip():
        return None

    try:
        commit_ts = int(result.stdout.strip())
    except ValueError:
        return None
    return int(now.timestamp() - commit_ts)


def _collect_work_service_signals(
    api: Any, context: HeartbeatSessionContext,
) -> dict[str, Any]:
    """Populate work-service-aware signal fields for ``context``.

    Queries (#249):
      * ``work_sessions`` — active claim (task_id + claim timestamp).
      * ``events`` — last event tied to this session.
      * ``git log -1 --format=%ct`` — last commit on the worktree.

    Returns a dict with ``active_claim_task_id``, ``claim_age_seconds``,
    ``last_event_seconds_ago``, ``last_commit_seconds_ago`` — any key may
    be absent (or value ``None``) when the underlying probe fails.
    Exceptions are swallowed — the classifier degrades to mechanical mode.
    """
    out: dict[str, Any] = {
        "active_claim_task_id": None,
        "claim_age_seconds": None,
        "last_event_seconds_ago": None,
        "last_commit_seconds_ago": None,
    }

    now = datetime.now(timezone.utc)

    # -- last event tied to this session from the unified message store --
    try:
        rows = api.supervisor.msg_store.query_messages(
            type="event",
            scope=context.session_name,
            limit=1,
        )
        if rows:
            created_at = rows[0].get("created_at")
            if created_at is None:
                raise ValueError("event row missing created_at")
            stamp = (
                created_at.isoformat()
                if hasattr(created_at, "isoformat")
                else str(created_at)
            )
            ts = datetime.fromisoformat(stamp)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            out["last_event_seconds_ago"] = int((now - ts).total_seconds())
    except Exception:  # noqa: BLE001
        logger.debug(
            "work-signal: last_event probe failed for %s",
            context.session_name, exc_info=True,
        )

    # -- active claim + commit age via the work service -----------------
    # The work service lives in ``<project_path>/.pollypm/state.db``.
    # Not every session maps to a project with a work DB; tests and
    # non-work-service setups skip through cleanly.
    try:
        config = api.supervisor.config
        session_cfg = config.sessions.get(context.session_name)
        if session_cfg is None:
            return out
        project_cfg = config.projects.get(session_cfg.project)
        if project_cfg is None:
            return out
        project_path: Path = project_cfg.path
        work_db = project_path / ".pollypm" / "state.db"
        if not work_db.exists():
            return out

        from pollypm.work import create_work_service

        worktree_path: str | None = None
        claim_started_at: str | None = None
        claim_task_id: str | None = None
        try:
            with create_work_service(
                db_path=work_db, project_path=project_path,
            ) as svc:
                sessions = svc.list_worker_sessions(
                    project=session_cfg.project, active_only=True,
                )
                # The caller's session name may be ``worker-<proj>`` while
                # ``agent_name`` is typically ``worker``. The simplest
                # reliable correlation is: an in-progress task whose
                # ``work_sessions`` row has the most recent started_at
                # wins. In practice worker sessions are per-task (see
                # #239-ish) so there's usually only one active row.
                best = None
                for row in sessions:
                    if best is None or (row.started_at or "") > (best.started_at or ""):
                        best = row
                if best is not None:
                    claim_started_at = best.started_at
                    claim_task_id = f"{best.task_project}/{best.task_number}"
                    worktree_path = best.worktree_path
        except Exception:  # noqa: BLE001
            logger.debug(
                "work-signal: work service query failed for %s",
                context.session_name, exc_info=True,
            )

        if claim_started_at and claim_task_id:
            try:
                ts = datetime.fromisoformat(claim_started_at)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                out["active_claim_task_id"] = claim_task_id
                out["claim_age_seconds"] = int((now - ts).total_seconds())
            except Exception:  # noqa: BLE001
                pass

        # Git commit timestamp on the claimed task's worktree.
        if worktree_path:
            try:
                commit_age = _last_commit_age_for_worktree(
                    project_path,
                    worktree_path,
                    now=now,
                )
                if commit_age is not None:
                    out["last_commit_seconds_ago"] = commit_age
            except Exception:  # noqa: BLE001
                logger.debug(
                    "work-signal: git log failed for %s",
                    worktree_path, exc_info=True,
                )
    except Exception:  # noqa: BLE001
        logger.debug(
            "work-signal: outer probe failed for %s",
            context.session_name, exc_info=True,
        )

    return out

_DEFAULT_POLICY = DefaultRecoveryPolicy()


def _classify_session_health(signals: SessionSignals) -> SessionHealth:
    return _DEFAULT_POLICY.classify(signals)


# #897 — persona-drift remediation now derives from the canonical
# role contract (#885) instead of maintaining its own role tables.
# The legacy module-level dicts below are kept as *derived views*
# of the canonical registry so any consumer that still imports them
# (and the legacy-table reconciliation test) keeps working without
# pinning a stale architect.md path or persona name.

from pollypm.role_contract import (
    ROLE_REGISTRY as _ROLE_REGISTRY,
    build_remediation_message as _build_canonical_remediation,
    canonical_role as _canonical_role,
)
from pollypm.signal_routing import (
    RoutingDecision as _RoutingDecision,
    SignalActionability as _SignalActionability,
    SignalAudience as _SignalAudience,
    SignalEnvelope as _SignalEnvelope,
    SignalSeverity as _SignalSeverity,
    compute_dedupe_key as _compute_dedupe_key,
    envelope_for_alert as _envelope_for_alert,
    register_routed_emitter as _register_routed_emitter,
    route_signal as _route_signal,
)


# #894 — register the heartbeat as an emitter that routes through
# SignalEnvelope. The release gate's ``signal_routing_emitters``
# check inspects ``ROUTED_EMITTERS`` for this name.
_register_routed_emitter("heartbeat")


def _emit_routed_alert(
    api: Any,
    *,
    session_name: str,
    alert_type: str,
    severity: str,
    message: str,
    subject: str | None = None,
    suggested_action: str | None = None,
    project: str | None = None,
) -> _RoutingDecision:
    """#910 — single funnel for every heartbeat alert emission.

    Constructs a :class:`SignalEnvelope`, asks
    :func:`route_signal` for the canonical surface set, and only
    then persists the legacy storage write via ``api.raise_alert``.
    The returned :class:`RoutingDecision` lets call sites that
    care about the surface set inspect it (e.g., to decide whether
    to also send a one-shot remediation message). The legacy
    persistence path is preserved so the rest of the heartbeat
    pipeline (cockpit alert reader, ``open_alerts``) keeps
    working — what changes is that no heartbeat alert reaches the
    store without first passing through the routing policy.
    """
    envelope: _SignalEnvelope = _envelope_for_alert(
        source="heartbeat",
        alert_type=alert_type,
        severity_label=severity,
        session_name=session_name,
        subject=subject or message[:80],
        body=message,
        suggested_action=suggested_action,
        project=project,
    )
    decision = _route_signal(envelope)
    api.raise_alert(session_name, alert_type, severity, message)
    return decision


def _emit_routed_event(
    api: Any,
    *,
    session_name: str,
    event_type: str,
    message: str,
    severity: _SignalSeverity = _SignalSeverity.INFO,
) -> _RoutingDecision:
    """#910 — single funnel for every heartbeat activity-feed event.

    Mirrors :func:`_emit_routed_alert` for the activity-feed
    ``record_event`` path. Activity-feed events are operational by
    nature (heartbeat ticks, sweep summaries, unmanaged-window
    notices) — they document what the heartbeat did but never
    interrupt the user. The envelope therefore carries
    :attr:`SignalAudience.OPERATOR` + :attr:`SignalActionability.OPERATIONAL`
    so :func:`route_signal` lands the signal on the Activity surface
    only, agreeing with the legacy event-store semantics.

    Construction order matches :func:`_emit_routed_alert`:
    build envelope, route, then persist via ``api.record_event`` so
    no event reaches the store without first passing through the
    routing policy.
    """
    envelope = _SignalEnvelope(
        audience=_SignalAudience.OPERATOR,
        severity=severity,
        actionability=_SignalActionability.OPERATIONAL,
        source="heartbeat",
        subject=event_type,
        body=message,
        dedupe_key=_compute_dedupe_key(
            source="heartbeat",
            kind=event_type,
            target=session_name,
        ),
        payload={"event_type": event_type, "session_name": session_name},
    )
    decision = _route_signal(envelope)
    api.record_event(session_name, event_type, message)
    return decision


def _materialize_legacy_table(field: str) -> dict[str, str]:
    """Materialize a legacy-shape ``{display_role: value}`` dict
    from the canonical role registry.

    Heartbeat call sites historically indexed these dicts by the
    display ("operator-pm") form. Deriving the dicts here keeps
    those callers working without a separate source of truth.
    Worker rows are intentionally omitted from the guide table
    because workers have no standalone profile (per-task prompt).
    """
    out: dict[str, str] = {}
    for key, contract in _ROLE_REGISTRY.items():
        display_key = key.replace("_", "-")
        if field == "persona":
            out[display_key] = contract.persona_name
        elif field == "guide" and contract.guide_path:
            out[display_key] = contract.guide_path
    return out


_ROLE_GUIDE_PATHS: dict[str, str] = _materialize_legacy_table("guide")
"""Derived view of :data:`pollypm.role_contract.ROLE_REGISTRY` for
legacy callers that still expect the old shape."""


_ROLE_PERSONA_NAMES: dict[str, str] = _materialize_legacy_table("persona")
"""Derived view — same rationale as :data:`_ROLE_GUIDE_PATHS`."""


def _build_persona_reassertion_message(
    *,
    role: str,
    drifted_to: str,
) -> str:
    """Heartbeat persona-drift remediation message.

    #897 — delegates to
    :func:`pollypm.role_contract.build_remediation_message`. The
    canonical wording (no ``<system-update>`` tag — #755), the
    canonical guide path, and the acknowledgement phrase all come
    from the role contract so future fixes land in one place.

    The heartbeat passes the role in display form
    (``"operator-pm"``); :func:`canonical_role` normalises it. An
    unknown role falls back to a generic re-anchor message rather
    than raising — the heartbeat is on the hot path and must not
    crash on a stale role string.
    """
    try:
        return _build_canonical_remediation(role, drifted_to)
    except ValueError:
        return (
            "PollyPM persona-drift correction (heartbeat-issued).\n"
            "\n"
            f"This session is configured as role={role!r}. The pane "
            f"just identified itself as {drifted_to!r}, which "
            "doesn't match.\n"
            "\n"
            "Re-anchor: stop, re-read your operating guide, and "
            "continue under the canonical role."
        )


def _select_intervention(
    health: SessionHealth,
    signals: SessionSignals,
    *,
    previous_interventions: int = 0,
):
    history = [
        InterventionHistoryEntry(action="") for _ in range(previous_interventions)
    ]
    return _DEFAULT_POLICY.select_intervention(health, signals, history)


# Characters that comprise pure visual dividers / formatting leaders. A
# transcript line composed solely of these (plus whitespace) carries no
# information for the alert reader, so the snippet selector skips it.
# #1068 — em-dash (U+2014) and en-dash (U+2013) added: Claude/Codex
# transcripts frequently emit a long em-dash run as a section divider,
# which previously survived ``_select_snippet`` and rendered as a
# content-free ``Additional work remains — ——————…`` alert body.
_DIVIDER_CHARS = frozenset("─━═—–-_=•└├")
_LEADER_STRIP_CHARS = "•└├—–-*  \t"


def _select_snippet(text: str, *, max_len: int = 120) -> str:
    """Pick the last *informative* line of ``text`` for an alert snippet.

    Walks lines from the end toward the start and returns the first one
    that is not empty, not a pure divider/box-drawing run, and not just a
    bullet leader. The chosen line has its leading bullets/leaders stripped
    and is truncated to ``max_len``. When nothing informative is found,
    returns a stable fallback string instead of leaking a bare trailing dash.
    """

    if not text:
        return "(continuing without a clear summary line)"

    for raw in reversed(text.splitlines()):
        stripped = raw.strip()
        if not stripped:
            continue
        # Drop lines composed entirely of divider / leader characters and
        # whitespace — these are visual chrome, not content.
        if all(ch in _DIVIDER_CHARS or ch.isspace() for ch in stripped):
            continue
        cleaned = stripped.lstrip(_LEADER_STRIP_CHARS).strip()
        if not cleaned:
            continue
        return cleaned[:max_len]

    return "(continuing without a clear summary line)"


class LocalHeartbeatBackend(HeartbeatBackend):
    name = "local"
    _UNMANAGED_WINDOW_ALERT_PREFIX = "unmanaged_window:"
    _WORKER_ACTIONABLE_STATUSES = frozenset({"queued", "in_progress", "blocked"})
    _MUTATING_SESSION_ROLES = frozenset(
        {
            "architect",
            "heartbeat-supervisor",
            "operator-pm",
            "review",
            "reviewer",
            "triage",
            "worker",
        }
    )
    _WORKER_MUTATING_SESSION_ROLES = frozenset({"worker"})

    _AUTH_FAILURE_PATTERNS = (
        "authentication failure",
        "not authenticated",
        "login required",
        "please login",
        "please log in",
        "invalid api key",
    )
    _WAITING_PATTERNS = (
        "let me know",
        "waiting for your",
        "please choose",
        "confirm ",
        "which would you like",
        "need your input",
        "need user input",
        "approve",
    )
    _DONE_PATTERNS = (
        "task complete",
        "completed",
        "all tests passed",
        "done.",
        "ready for review",
        "finished",
        "implemented",
        "resolved",
    )
    _FOLLOWUP_PATTERNS = (
        "next step",
        "next,",
        "remaining",
        "still need",
        "todo",
        "to do",
        "follow up",
        "continue with",
        "not finished",
        "partial",
    )

    def run(self, api, *, snapshot_lines: int = 200):
        self._process_unmanaged_windows(api)
        for context in api.list_sessions():
            try:
                self._process_session(api, context)
            except Exception as exc:  # noqa: BLE001
                # Log and continue — don't let one session abort the entire sweep
                try:
                    from pollypm.plugins_builtin.activity_feed.summaries import (
                        activity_summary,
                    )

                    # #910 — routed through _emit_routed_event so the
                    # activity-feed write goes through SignalEnvelope.
                    _emit_routed_event(
                        api,
                        session_name=context.session_name,
                        event_type="heartbeat_error",
                        message=activity_summary(
                            summary=f"Error processing session: {exc}",
                            severity="critical",
                            verb="errored",
                            subject=context.session_name,
                        ),
                        severity=_SignalSeverity.CRITICAL,
                    )
                except Exception:  # noqa: BLE001
                    pass
        from pollypm.plugins_builtin.activity_feed.summaries import activity_summary

        open_alerts = api.open_alerts()
        alerts_n = len(open_alerts)
        alert_word = "alert" if alerts_n == 1 else "alerts"
        # #910 — sweep-completion event routed through SignalEnvelope.
        _emit_routed_event(
            api,
            session_name="heartbeat",
            event_type="heartbeat",
            message=activity_summary(
                summary=f"Heartbeat sweep completed with {alerts_n} open {alert_word}",
                severity="recommendation" if open_alerts else "routine",
                verb="swept",
                subject="heartbeat",
                open_alerts=alerts_n,
            ),
        )
        return open_alerts

    def _assert_role_allows_write(
        self,
        context: HeartbeatSessionContext,
        *,
        action: str,
        allowed_roles: frozenset[str],
    ) -> None:
        if context.role in allowed_roles:
            return
        allowed = ", ".join(sorted(allowed_roles))
        raise AssertionError(
            f"heartbeat {action} is not allowed for role "
            f"{context.role!r} on {context.session_name}; allowed roles: {allowed}"
        )

    def _set_session_status(
        self,
        api,
        context: HeartbeatSessionContext,
        status: str,
        *,
        reason: str,
    ) -> None:
        self._assert_role_allows_write(
            context,
            action="set_session_status",
            allowed_roles=self._MUTATING_SESSION_ROLES,
        )
        api.set_session_status(context.session_name, status, reason=reason)

    def _recover_session(
        self,
        api,
        context: HeartbeatSessionContext,
        *,
        failure_type: str,
        message: str,
    ) -> None:
        self._assert_role_allows_write(
            context,
            action="recover_session",
            allowed_roles=self._MUTATING_SESSION_ROLES,
        )
        api.recover_session(
            context.session_name,
            failure_type=failure_type,
            message=message,
        )

    def _send_worker_message(
        self,
        api,
        context: HeartbeatSessionContext,
        text: str,
        *,
        owner: str = "heartbeat",
    ) -> None:
        self._assert_role_allows_write(
            context,
            action="send_session_message",
            allowed_roles=self._WORKER_MUTATING_SESSION_ROLES,
        )
        api.send_session_message(context.session_name, text, owner=owner)

    def _process_unmanaged_windows(self, api) -> None:
        current_alert_types: set[str] = set()
        existing_alert_types = {
            alert.alert_type
            for alert in api.open_alerts()
            if alert.session_name == "heartbeat" and alert.alert_type.startswith(self._UNMANAGED_WINDOW_ALERT_PREFIX)
        }
        for window in api.list_unmanaged_windows():
            alert_type = f"{self._UNMANAGED_WINDOW_ALERT_PREFIX}{window.tmux_session}:{window.window_name}"
            current_alert_types.add(alert_type)
            message = (
                f"Found unmanaged tmux window {window.window_name} in session {window.tmux_session} "
                f"running {window.pane_command}"
            )
            _emit_routed_alert(
                api,
                session_name="heartbeat",
                alert_type=alert_type,
                severity="warn",
                message=message,
                subject=f"Unmanaged tmux window: {window.window_name}",
            )
            if alert_type not in existing_alert_types:
                from pollypm.plugins_builtin.activity_feed.summaries import (
                    activity_summary,
                )

                # #910 — unmanaged-window event routed through SignalEnvelope.
                _emit_routed_event(
                    api,
                    session_name="heartbeat",
                    event_type="unmanaged_window",
                    message=activity_summary(
                        summary=message,
                        severity="recommendation",
                        verb="unmanaged",
                        subject=window.window_name,
                    ),
                )
        for alert_type in existing_alert_types - current_alert_types:
            api.clear_alert("heartbeat", alert_type)

    def _process_session(self, api, context: HeartbeatSessionContext) -> None:
        alerts: list[str] = []
        # Skip disabled sessions (decommissioned via pm worker-stop)
        # Also skip if the operator recently managed workers (avoid race with Polly)
        try:
            rt = api.supervisor.store.get_session_runtime(context.session_name)
            if rt and rt.status in ("disabled", "switching"):
                return
        except (AttributeError, Exception):  # noqa: BLE001
            pass  # API may not have supervisor (e.g., tests)
        mechanical_only = context.role == "heartbeat-supervisor"
        if not context.window_present:
            _emit_routed_alert(
                api,
                session_name=context.session_name,
                alert_type="missing_window",
                severity="error",
                message=(
                    f"Expected tmux window {context.window_name} in "
                    f"session {context.tmux_session}"
                ),
                subject=f"{context.session_name} missing tmux window",
                suggested_action=(
                    "Open Workers and restart the missing session."
                ),
            )
            self._set_session_status(
                api,
                context,
                "recovering",
                reason="Expected tmux window is missing",
            )
            self._recover_session(
                api,
                context,
                failure_type="missing_window",
                message="Expected tmux window is missing",
            )
            api.update_cursor(
                context.session_name,
                source_path=context.source_path,
                last_offset=context.source_bytes,
                verdict="missing_window",
                reason="Expected tmux window is missing",
            )
            return

        api.record_observation(context)
        api.clear_alert(context.session_name, "missing_window")

        if context.pane_dead:
            _emit_routed_alert(
                api,
                session_name=context.session_name,
                alert_type="pane_dead",
                severity="error",
                message=(
                    f"Pane {context.pane_id} in window "
                    f"{context.window_name} has exited"
                ),
                subject=f"{context.session_name} pane exited",
                suggested_action=(
                    "Open Workers and restart the exited session."
                ),
            )
            self._set_session_status(api, context, "recovering", reason="Pane exited")
            self._recover_session(
                api,
                context,
                failure_type="pane_dead",
                message="Pane exited",
            )
            alerts.append("pane_dead")
        else:
            api.clear_alert(context.session_name, "pane_dead")

        if (context.pane_command or "") in {"bash", "zsh", "sh", "fish"}:
            _emit_routed_alert(
                api,
                session_name=context.session_name,
                alert_type="shell_returned",
                severity="warn",
                message=(
                    f"Window {context.window_name} appears to be back at "
                    f"the shell prompt ({context.pane_command})"
                ),
                subject=f"{context.session_name} returned to shell",
                suggested_action=(
                    "Open Workers and restart the session."
                ),
            )
            alerts.append("shell_returned")
        else:
            api.clear_alert(context.session_name, "shell_returned")

        stopped_reason = "Pane process is stopped (SIGSTOP)"
        if context.pane_stopped:
            _emit_routed_alert(
                api,
                session_name=context.session_name,
                alert_type="pane_stopped",
                severity="error",
                message=(
                    f"{context.session_name} appears stuck: "
                    f"{stopped_reason}. Open Workers and resume or "
                    "restart the stopped session."
                ),
                subject=f"{context.session_name} process stopped",
                suggested_action=(
                    "Open Workers and resume or restart the stopped session."
                ),
            )
            alerts.append("pane_stopped")
        else:
            api.clear_alert(context.session_name, "pane_stopped")

        # Sessions parked at a prompt are legitimately idle — not an alert condition.
        # Only the suspected_loop detector (below) alerts on sustained identical snapshots.
        api.clear_alert(context.session_name, "idle_output")

        if not mechanical_only and context.previous_snapshot_hash and context.previous_snapshot_hash == context.snapshot_hash:
            hashes = api.recent_snapshot_hashes(context.session_name, limit=3)
            if len(hashes) == 3 and len(set(hashes)) == 1:
                # #765 — run the same-snapshot finding through the
                # stall classifier. Only ``unrecoverable_stall`` earns
                # an alert; ``legitimate_idle`` and ``transient`` stay
                # silent so the cockpit doesn't toast the user for a
                # session that's behaving correctly (architect awaiting
                # approval, reviewer idle with empty queue, etc.).
                from pollypm.heartbeats.stall_classifier import (
                    StallContext,
                    classify_stall,
                )
                from pollypm.idle_placeholders import (
                    pane_is_idle_placeholder as _pane_is_idle_placeholder,
                )

                stall_ctx = StallContext(
                    role=context.role or "",
                    session_name=context.session_name,
                    has_pending_work=self._has_pending_work(api, context),
                    pane_is_idle_placeholder=_pane_is_idle_placeholder(
                        context.pane_text or "",
                    ),
                )
                stall_class = classify_stall(stall_ctx)
                if stall_class != "unrecoverable_stall":
                    api.clear_alert(context.session_name, "suspected_loop")
                else:
                    # #760 — concrete actionable copy: name the role,
                    # say what's wrong in plain English, and keep the
                    # next step in the cockpit.
                    _emit_routed_alert(
                        api,
                        session_name=context.session_name,
                        alert_type="suspected_loop",
                        severity="warn",
                        message=(
                            f"{context.role or 'session'} "
                            f"{context.session_name} stalled — no new output "
                            f"for 3 heartbeats with queued work. "
                            "Open Workers and restart the stalled session."
                        ),
                        subject=(
                            f"{context.session_name} appears stalled"
                        ),
                        suggested_action=(
                            "Open Workers and restart the stalled session."
                        ),
                    )
                    alerts.append("suspected_loop")
                    # After 5 consecutive identical snapshots, queue a Haiku triage
                    longer_hashes = api.recent_snapshot_hashes(context.session_name, limit=5)
                    if len(longer_hashes) == 5 and len(set(longer_hashes)) == 1:
                        if context.role == "worker":
                            self._triage_stalled_worker(api, context)
            else:
                api.clear_alert(context.session_name, "suspected_loop")
        else:
            api.clear_alert(context.session_name, "suspected_loop")

        # #757 — mid-flight persona-drift detection. Kickoff-time swaps
        # are caught by supervisor._assert_session_launch_matches; this
        # catches sessions whose identity drifted AFTER kickoff (e.g. a
        # prompt-injection loop, or a session reading a wrong-role
        # control-prompts file). Conservative: only fires on strong
        # identity-claim phrasings, never on casual mentions.
        try:
            from pollypm.supervisor import detect_persona_drift
            drifted_to = detect_persona_drift(context.role, context.pane_text or "")
        except Exception:  # noqa: BLE001
            drifted_to = None
        if drifted_to:
            # #757/#815 — determine whether this is a newly-opened
            # drift alert before upserting it. ``raise_alert`` persists
            # immediately, so checking afterward would always suppress
            # the one-shot remediation message.
            try:
                drift_alert_already_open = any(
                    getattr(alert, "alert_type", None) == "persona_drift_detected"
                    for alert in api.open_alerts()
                    if getattr(alert, "session_name", None) == context.session_name
                )
            except Exception:  # noqa: BLE001
                drift_alert_already_open = True  # err on the side of NOT spamming
            # #760 — actionable copy: explain what drifted, name the
            # restart command the user can copy-paste, keep the
            # observed-identity detail present for context.
            #
            # Note: ``severity="error"`` puts this through the
            # ACTION_REQUIRED toast tier (cockpit_alerts.alert_channel
            # — #765). Drift is one of the rare cases where we DO want
            # to interrupt the user.
            # #894 — route through SignalEnvelope so the canonical
            # routing policy (audience/actionability/dedupe) is the
            # source of truth for whether this alert toasts. The
            # ``raise_alert`` call below is the existing storage
            # write; SignalEnvelope.route_signal classifies the
            # delivery surfaces — for ACTION_REQUIRED + USER the
            # decision includes Toast, which matches the legacy
            # severity="error" intent of the original site.
            _drift_subject = (
                f"{context.session_name} ({context.role}) drifted to "
                f"{drifted_to!r}"
            )
            _drift_body = (
                f"{context.session_name} ({context.role}) identified "
                f"itself as {drifted_to!r} mid-session — identity drift. "
                "Open Workers and restart the drifted session."
            )
            # #910 — consolidated through the same routed-emit helper
            # used by every other heartbeat alert. The helper is the
            # single funnel: it builds the envelope, calls
            # route_signal, and only then persists. Keeping the
            # persistence here means the cockpit alert reader and the
            # `pm alerts` listing keep working exactly as before;
            # what changes is that no alert reaches the store
            # without first passing through the routing policy.
            _emit_routed_alert(
                api,
                session_name=context.session_name,
                alert_type="persona_drift_detected",
                severity="error",
                message=_drift_body,
                subject=_drift_subject,
                suggested_action=(
                    "Open Workers and restart the drifted session."
                ),
            )
            alerts.append("persona_drift_detected")
            # #757 — reactive remediation: send a one-shot re-assertion
            # message to the drifted session so the model can correct
            # itself before the user has to intervene. Gated by the
            # alert state — only sent on the *first* heartbeat that
            # detects the drift, not on every subsequent tick the
            # alert is still open. The owner-tagged path (``persona-
            # drift-remediation``) makes the corrective message
            # distinguishable from arbitrary user input in transcript
            # scans, and avoids the ``<system-update>`` tag that
            # tripped prompt-injection defenses (#755).
            if not drift_alert_already_open:
                try:
                    api.send_session_message(
                        context.session_name,
                        _build_persona_reassertion_message(
                            role=context.role or "",
                            drifted_to=drifted_to,
                        ),
                        owner="persona-drift-remediation",
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "heartbeat: persona-drift remediation send failed for %s",
                        context.session_name,
                    )
        else:
            api.clear_alert(context.session_name, "persona_drift_detected")

        combined_text = "\n".join(part for part in [context.transcript_delta, context.pane_text] if part).lower()
        status_locked = False
        if any(pattern in combined_text for pattern in self._AUTH_FAILURE_PATTERNS):
            _emit_routed_alert(
                api,
                session_name=context.session_name,
                alert_type="auth_broken",
                severity="error",
                message=(
                    f"Window {context.window_name} reported "
                    f"authentication failure"
                ),
                subject=f"{context.session_name} authentication failure",
                suggested_action=(
                    "Open Settings to fix the account, then restart the session from Workers."
                ),
            )
            api.mark_account_auth_broken(
                context.account_name,
                context.provider,
                reason="live session reported authentication failure",
            )
            self._set_session_status(
                api,
                context,
                "auth_broken",
                reason="Authentication failure reported",
            )
            alerts.append("auth_broken")
            status_locked = True
        else:
            api.clear_alert(context.session_name, "auth_broken")

        if context.pane_dead:
            status_locked = True

        if context.pane_stopped:
            verdict, reason = ("stuck", stopped_reason)
            api.clear_alert(context.session_name, "needs_followup")
            if not status_locked:
                self._set_session_status(
                    api,
                    context,
                    "stuck",
                    reason=reason,
                )
        elif mechanical_only:
            verdict, reason = ("healthy", "Heartbeat supervisor only checks mechanical session health")
            api.clear_alert(context.session_name, "needs_followup")
            if not status_locked:
                self._set_session_status(api, context, "healthy", reason=reason)
        else:
            verdict, reason = self._classify(context)
            if verdict == "needs_followup":
                _emit_routed_alert(
                    api,
                    session_name=context.session_name,
                    alert_type="needs_followup",
                    severity="warn",
                    message=reason,
                    subject=f"{context.session_name} needs follow-up",
                )
                if not status_locked:
                    self._set_session_status(
                        api,
                        context,
                        "needs_followup",
                        reason=reason,
                    )
                # Alerts are visible in the cockpit and via `pm alerts`.
                # No need to inject messages into the operator chat —
                # the operator gets nudged only when *it* is stalled.
                alerts.append("needs_followup")
            else:
                api.clear_alert(context.session_name, "needs_followup")
                if not status_locked:
                    if verdict == "blocked":
                        self._set_session_status(
                            api,
                            context,
                            "waiting_on_user",
                            reason=reason,
                        )
                    elif verdict == "done":
                        self._set_session_status(api, context, "idle", reason=reason)
                    else:
                        self._set_session_status(
                            api,
                            context,
                            "healthy",
                            reason=reason,
                        )

        api.record_checkpoint(context, alerts=alerts)
        api.update_cursor(
            context.session_name,
            source_path=context.source_path,
            last_offset=context.source_bytes,
            snapshot_hash=context.snapshot_hash,
            verdict=verdict,
            reason=reason,
        )

        # Use the structured classification engine for intervention decisions
        if not mechanical_only:
            try:
                signals = self._context_to_signals(context, api)
                health = _classify_session_health(signals)
                runtime = api.supervisor.store.get_session_runtime(context.session_name)
                prev = runtime.recovery_attempts if runtime else 0
                intervention = _select_intervention(health, signals, previous_interventions=prev)
                # #249 — work-aware interventions. These dispatch before
                # the generic worker-triage path so the policy-chosen
                # action actually runs.
                if intervention and intervention.action == "resume_ping":
                    self._apply_resume_ping(api, context, signals, intervention)
                elif intervention and intervention.action == "prompt_pm_task_next":
                    self._apply_prompt_pm_task_next(api, context)
                elif intervention and context.role == "worker":
                    # Use Haiku to decide the right action for idle workers.
                    # The LLM reads the snapshot and classifies: push forward,
                    # nudge, do nothing, or escalate.
                    self._triage_stalled_worker(api, context)
                elif intervention and intervention.action == "escalate":
                    self._escalate(api, context, intervention.reason)
            except Exception:  # noqa: BLE001
                pass

    def _apply_resume_ping(
        self, api, context: HeartbeatSessionContext, signals: SessionSignals,
        intervention,
    ) -> None:
        """Emit a resume ping via task_assignment_notify (#249).

        Reuses the existing 30-min dedupe via ``notify()``. Best-effort —
        all failures are logged and swallowed so the sweep never aborts
        on an apply hiccup.
        """
        try:
            # #939: route through the plugin's public API rather than
            # reaching into ``handlers.sweep`` / ``resolver`` privately.
            from pollypm.plugins_builtin.task_assignment_notify.api import (
                DEDUPE_WINDOW_SECONDS,
                build_event_for_task as _build_event_for_task,
                load_runtime_services,
                notify as _notify,
            )
            from pollypm.work import create_work_service

            task_id = signals.active_claim_task_id
            if not task_id or "/" not in task_id:
                return
            project, number_s = task_id.rsplit("/", 1)
            try:
                task_number = int(number_s)
            except ValueError:
                return

            config = api.supervisor.config
            project_cfg = config.projects.get(project)
            if project_cfg is None:
                return
            work_db = project_cfg.path / ".pollypm" / "state.db"
            if not work_db.exists():
                return

            services = load_runtime_services()
            try:
                with create_work_service(
                    db_path=work_db, project_path=project_cfg.path,
                ) as svc:
                    tasks = svc.list_tasks(project=project)
                    task = next(
                        (
                            t for t in tasks
                            if t.task_number == task_number
                        ),
                        None,
                    )
                    if task is None:
                        return
                    event = _build_event_for_task(svc, task)
                if event is None:
                    return
                _notify(
                    event, services=services,
                    throttle_seconds=DEDUPE_WINDOW_SECONDS,
                )
            finally:
                # #1372 — release every owned sqlite connection. The
                # pre-fix version closed only ``services.work_service``
                # and leaked the fresh ``StateStore`` opened by
                # ``load_runtime_services`` on each resume_ping
                # evaluation (regression of #1069 — the same pattern
                # this plugin's handlers/notify.py and handlers/sweep.py
                # already fixed via ``services.close()``).
                try:
                    services.close()
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "heartbeat resume_ping: services.close raised",
                        exc_info=True,
                    )

            # Raise a low-severity alert so the cockpit surfaces it.
            try:
                api.supervisor.msg_store.upsert_alert(
                    context.session_name,
                    f"stuck_on_task:{task_id}",
                    "warning",
                    f"Stuck on {task_id}: {intervention.reason[:140]}",
                )
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            logger.debug(
                "resume_ping apply failed for %s",
                context.session_name, exc_info=True,
            )

    def _apply_prompt_pm_task_next(
        self, api, context: HeartbeatSessionContext,
    ) -> None:
        """Send `pm task next` into a silent worker session (#249).

        The send is routed through the same rate-limit path used for
        ordinary nudges so we don't spam a worker that's slow to pick up.
        """
        try:
            self._send_worker_message(
                api,
                context,
                "pm task next",
                owner="heartbeat",
            )
            try:
                from pollypm.plugins_builtin.activity_feed.summaries import (
                    activity_summary,
                )

                api.supervisor.msg_store.append_event(
                    scope=context.session_name,
                    sender=context.session_name,
                    subject="silent_worker_prompt",
                    payload={
                        "message": activity_summary(
                            summary="Sent 'pm task next' to silent worker",
                            severity="recommendation",
                            verb="prompted",
                            subject=context.session_name,
                        ),
                    },
                )
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            logger.debug(
                "prompt_pm_task_next apply failed for %s",
                context.session_name, exc_info=True,
            )

    def _escalate(self, api, context: HeartbeatSessionContext, reason: str) -> None:
        """Raise a durable alert so the user sees a stuck session in the cockpit."""
        # Dedup: don't re-escalate if we escalated this session within 10 minutes.
        # #349: escalation events now live on the unified ``messages`` table.
        try:
            from datetime import UTC, datetime
            recent = api.supervisor.msg_store.query_messages(
                type="event",
                scope=context.session_name,
                limit=20,
            )
            now = datetime.now(UTC)
            for event in recent:
                if event.get("subject") != "escalated":
                    continue
                created_at = event.get("created_at")
                if created_at is None:
                    continue
                stamp = (
                    created_at.isoformat()
                    if hasattr(created_at, "isoformat")
                    else str(created_at)
                )
                try:
                    parsed = datetime.fromisoformat(stamp)
                except ValueError:
                    continue
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                age = (now - parsed).total_seconds()
                if age < 600:
                    return
                break
        except Exception:  # noqa: BLE001
            pass

        try:
            _emit_routed_alert(
                api,
                session_name=context.session_name,
                alert_type="stuck_session",
                severity="warn",
                message=(
                    f"{context.session_name} needs attention: "
                    f"{reason[:160]}"
                ),
                subject=f"{context.session_name} needs attention",
                suggested_action=(
                    "Open Workers and restart the stalled session."
                ),
            )
            from pollypm.plugins_builtin.activity_feed.summaries import (
                activity_summary,
            )

            api.supervisor.msg_store.append_event(
                scope=context.session_name,
                sender=context.session_name,
                subject="escalated",
                payload={
                    "message": activity_summary(
                        summary=f"Raised stuck_session alert: {reason[:80]}",
                        severity="critical",
                        verb="escalated",
                        subject=context.session_name,
                    ),
                    "reason": reason,
                },
            )
        except Exception:  # noqa: BLE001
            pass

    def _context_to_signals(self, context: HeartbeatSessionContext, api) -> SessionSignals:
        """Bridge HeartbeatSessionContext to SessionSignals for the classification engine."""
        hashes = api.recent_snapshot_hashes(context.session_name, limit=5)
        repeated = 0
        if hashes:
            for h in hashes:
                if h == hashes[0]:
                    repeated += 1
                else:
                    break

        # Work-service-aware signals (#249). Populate best-effort — if
        # the work service can't be reached (tests, missing DB, etc.)
        # these stay None and the classifier falls through to the
        # pre-existing mechanical ladder.
        work_signals = _collect_work_service_signals(api, context)

        # #1010 — detect Codex rotating-placeholder / Claude empty-prompt
        # idle UI so the classifier can short-circuit alive-but-idle
        # panes to HEALTHY instead of cycling through SILENT_WORKER /
        # STUCK / LOOPING (which keeps re-raising stuck_session and
        # blocks #1008's 90s auto-clear streak).
        try:
            from pollypm.idle_placeholders import pane_is_idle_placeholder

            placeholder_idle = pane_is_idle_placeholder(context.pane_text or "")
        except Exception:  # noqa: BLE001
            placeholder_idle = False

        return SessionSignals(
            session_name=context.session_name,
            window_present=context.window_present,
            pane_dead=context.pane_dead,
            output_stale=not bool(context.transcript_delta),
            snapshot_repeated=repeated,
            auth_failure=any(
                p in (context.transcript_delta or context.pane_text or "").lower()
                for p in self._AUTH_FAILURE_PATTERNS
            ),
            has_transcript_delta=bool(context.transcript_delta),
            last_verdict=context.cursor.last_verdict if context.cursor else "",
            idle_cycles=repeated,
            session_role=context.role or "",
            turn_active=bool(context.transcript_delta),
            active_claim_task_id=work_signals.get("active_claim_task_id"),
            claim_age_seconds=work_signals.get("claim_age_seconds"),
            last_event_seconds_ago=work_signals.get("last_event_seconds_ago"),
            last_commit_seconds_ago=work_signals.get("last_commit_seconds_ago"),
            pane_is_idle_placeholder=placeholder_idle,
        )

    # Rate limit nudges: max once per session per 10 minutes
    # Rate limits for worker nudges
    _NUDGE_COOLDOWN_SECONDS = 600
    _NUDGE_ESCALATION_IDLE_CYCLES = 8
    _MAX_NUDGES_BEFORE_RECOVERY = 6

    def _repeated_snapshot_count(
        self,
        api,
        context: HeartbeatSessionContext,
        *,
        limit: int,
    ) -> int:
        hashes = api.recent_snapshot_hashes(context.session_name, limit=limit)
        repeated = 0
        if hashes:
            for value in hashes:
                if value == hashes[0]:
                    repeated += 1
                else:
                    break
        return repeated

    def _nudge_stalled_worker(self, api, context: HeartbeatSessionContext) -> None:
        """Send a targeted nudge to a stalled WORKER. Never targets the operator."""
        if context.role != "worker":
            return
        # Don't nudge workers with no pending work — they're legitimately idle
        if not self._has_pending_work(api, context):
            return
        try:
            from datetime import UTC, datetime
            # #349: events migrated to the unified ``messages`` table.
            recent = api.supervisor.msg_store.query_messages(
                type="event",
                scope=context.session_name,
                limit=200,
            )
            now = datetime.now(UTC)
            idle_cycles = self._repeated_snapshot_count(
                api,
                context,
                limit=self._NUDGE_ESCALATION_IDLE_CYCLES,
            )

            def _age_seconds(event: dict) -> float | None:
                created_at = event.get("created_at")
                if created_at is None:
                    return None
                stamp = (
                    created_at.isoformat()
                    if hasattr(created_at, "isoformat")
                    else str(created_at)
                )
                try:
                    parsed = datetime.fromisoformat(stamp)
                except ValueError:
                    return None
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                return (now - parsed).total_seconds()

            # Debounce: skip if worker received input recently.
            for event in recent:
                if event.get("subject") != "send_input":
                    continue
                age = _age_seconds(event)
                if age is not None and age < 120:
                    return
            # Rate limit + circuit breaker.
            nudge_count = 0
            most_recent_age: float | None = None
            same_episode_nudge = False
            same_episode_escalated = False
            for event in recent:
                payload = event.get("payload") or {}
                subject = event.get("subject")
                if (
                    subject == "nudge"
                    and payload.get("snapshot_hash") == context.snapshot_hash
                ):
                    same_episode_nudge = True
                if (
                    subject == "nudge_escalated"
                    and payload.get("snapshot_hash") == context.snapshot_hash
                ):
                    same_episode_escalated = True
                if subject != "nudge":
                    continue
                age = _age_seconds(event)
                if age is None:
                    continue
                if age < 3600:
                    nudge_count += 1
                if most_recent_age is None:
                    most_recent_age = age
            if same_episode_escalated:
                return
            if same_episode_nudge:
                if idle_cycles >= self._NUDGE_ESCALATION_IDLE_CYCLES:
                    _emit_routed_alert(
                        api,
                        session_name=context.session_name,
                        alert_type="stuck_session",
                        severity="warn",
                        message=(
                            f"{context.session_name} stayed stalled after "
                            f"a heartbeat nudge for {idle_cycles} "
                            f"identical heartbeats"
                        ),
                        subject=(
                            f"{context.session_name} unresponsive to nudge"
                        ),
                        suggested_action=(
                            "Open Workers and restart the stalled session."
                        ),
                    )
                    api.supervisor.msg_store.append_event(
                        scope=context.session_name,
                        sender=context.session_name,
                        subject="nudge_escalated",
                        payload={
                            "snapshot_hash": context.snapshot_hash,
                            "idle_cycles": idle_cycles,
                        },
                    )
                return
            # Circuit breaker: too many nudges → recover the worker.
            if nudge_count >= self._MAX_NUDGES_BEFORE_RECOVERY:
                self._recover_session(
                    api,
                    context,
                    failure_type="unresponsive",
                    message=f"Worker unresponsive after {nudge_count} nudges — restarting",
                )
                return
            # Rate limit.
            if most_recent_age is not None and most_recent_age < self._NUDGE_COOLDOWN_SECONDS:
                return
        except Exception:  # noqa: BLE001
            pass
        # Context-aware nudge for the worker
        snippet = (context.pane_text or "").strip().splitlines()[-1][:80] if context.pane_text else ""
        if "permission" in snippet.lower() or "approve" in snippet.lower():
            message = "You appear stuck on a permissions prompt. Accept or work around it."
        elif "error" in snippet.lower() or "failed" in snippet.lower():
            message = "You hit an error. Read it carefully, fix the root cause, and continue."
        else:
            message = "State the remaining task in one sentence, execute the next step, and report."
        self._send_worker_message(api, context, message, owner="heartbeat")
        try:
            from pollypm.plugins_builtin.activity_feed.summaries import (
                activity_summary,
            )

            api.supervisor.msg_store.append_event(
                scope=context.session_name,
                sender=context.session_name,
                subject="nudge",
                payload={
                    "message": activity_summary(
                        summary=f"Sent nudge: {message[:80]}",
                        severity="recommendation",
                        verb="nudged",
                        subject=context.session_name,
                    ),
                    "snapshot_hash": context.snapshot_hash,
                    "idle_cycles": idle_cycles,
                },
            )
        except Exception:  # noqa: BLE001
            pass

    def _has_pending_work(self, api, context: HeartbeatSessionContext) -> bool:
        """Check if a worker's project has ready/in-progress tasks.

        Delegates to :func:`pollypm.heartbeats.stall_classifier.has_pending_work_for_session`
        so this path and the supervisor-boundary path
        (the supervisor-alert update path) share one
        definition of "is there work". See #765.
        """
        from pollypm.heartbeats.stall_classifier import (
            has_pending_work_for_session,
        )

        try:
            return has_pending_work_for_session(
                api.supervisor.config, context.session_name,
            )
        except Exception:  # noqa: BLE001
            return True

    def _triage_stalled_worker(self, api, context: HeartbeatSessionContext) -> None:
        """Fast heuristic triage for stalled workers (60s path).

        No Haiku call — the 5-minute sweep handles LLM analysis.
        This is just pattern matching on the snapshot for quick decisions.
        """
        snapshot = (context.pane_text or "").strip()
        if not snapshot:
            return

        lowered = snapshot.lower()

        # Worker asking for permission → push forward
        proceed_signals = ["if you want", "shall i", "should i", "want me to", "i can do"]
        if any(sig in lowered for sig in proceed_signals):
            self._send_worker_message(
                api,
                context,
                "Yes, proceed. Do the next step you outlined.",
                owner="heartbeat",
            )
            from pollypm.plugins_builtin.activity_feed.summaries import (
                activity_summary,
            )

            api.supervisor.msg_store.append_event(
                scope=context.session_name,
                sender=context.session_name,
                subject="heuristic_triage",
                payload={
                    "message": activity_summary(
                        summary="Pushed forward: proceed signal detected",
                        severity="routine",
                        verb="triaged",
                        subject=context.session_name,
                    ),
                },
            )
            return

        # Worker has obvious next steps
        next_step_signals = ["next step", "next,", "todo", "remaining", "need to"]
        if any(sig in lowered for sig in next_step_signals):
            self._nudge_stalled_worker(api, context)
            return

        # Worker hit an error
        if "error" in lowered or "failed" in lowered or "traceback" in lowered:
            self._nudge_stalled_worker(api, context)
            return

    def _classify(self, context: HeartbeatSessionContext) -> tuple[str, str]:
        # Only classify on NEW transcript content. Falling back to the
        # full pane_text for idle sessions means old "remaining" /
        # "next" language stays matched forever, continuously firing
        # ``needs_followup`` alerts against agents that are actually
        # just waiting for the next user prompt. The heartbeat loop
        # already emits a separate ``suspected_loop`` signal for true
        # stuck sessions; classify should only speak about fresh work.
        delta = (context.transcript_delta or "").strip()
        if not delta:
            return "unclear", "No new transcript output since last heartbeat"
        # Strip ANSI escape sequences before any further processing so
        # truncated cursor-movement codes (``[2``, ``[4``) cannot leak into
        # the user-facing snippet.
        text = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", delta)
        snippet = _select_snippet(text)
        lowered = text.lower()
        # #1043 — workers polling ``pm task next -p <project>`` for new work
        # would trip the ``\bnext\b`` regex below and the classifier would
        # mislabel idle polling as ``needs_followup``. Strip the command
        # literal (and the matching idle-poll output / "before next poll"
        # idle-loop language) before pattern matching so classification
        # reflects worker state, not the command name. The original
        # ``text`` is preserved for snippet rendering.
        text_for_classify = re.sub(r"pm task next(?:\s+-p\s+\S+)?", " ", lowered)
        text_for_classify = re.sub(r"no tasks available\.?", " ", text_for_classify)
        text_for_classify = re.sub(
            r"\bbefore\s+next\s+(?:poll|tick|run|loop|check)\b",
            " ",
            text_for_classify,
        )
        if any(pattern in text_for_classify for pattern in self._WAITING_PATTERNS):
            return "blocked", f"Waiting on operator input — {snippet}"
        if any(pattern in text_for_classify for pattern in self._FOLLOWUP_PATTERNS):
            return "needs_followup", f"Additional work remains — {snippet}"
        if any(pattern in text_for_classify for pattern in self._DONE_PATTERNS):
            return "done", f"Last turn appears complete — {snippet}"
        if re.search(r"\b(next|remaining|follow-up|follow up|still need)\b", text_for_classify):
            return "needs_followup", f"Additional work remains — {snippet}"
        if lowered.endswith("?"):
            return "blocked", f"Last turn ended with a question — {snippet}"
        return "unclear", f"Could not confidently classify the last turn — {snippet}"
