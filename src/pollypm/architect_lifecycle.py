"""Architect-session lifecycle: idle-close + resume.

PollyPM spawns one ``architect-<project>`` session per project that
holds plan context across the project's lifetime. Each architect
inherits the full ``--dangerously-skip-permissions`` posture and a
warm Claude/Codex context — so they're heavy. A project that's been
quiet for 2+ hours doesn't need its architect resident; we'd rather
free the RAM and warm-resume on demand.

This module owns three responsibilities:

1. **Idleness measurement** — :func:`architect_idle_for` reads the
   heartbeat snapshot history and returns how long an architect's
   pane has been producing identical snapshots.
2. **Close-with-token capture** — :func:`close_idle_architect` asks
   the provider for the latest session UUID, persists it in the
   ``architect_resume_tokens`` table, then kills the tmux window.
3. **Resume-aware launch argv** — :func:`resolve_launch_argv` looks
   up a stored token and routes through ``provider.resume_launch_cmd``
   when present, falling back to a fresh ``worker_launch_cmd``.

The 2-hour threshold is the default per Sam's spec; tests / future
config layers can override it via the ``idle_threshold`` argument.

Decision: idle is measured from the **architect's pane snapshot**,
not project-wide activity. The architect receives Polly's prompts
via ``tmux send-keys`` so its pane updates whenever the project is
moving; a truly-idle pane is a reliable proxy for a truly-idle
project. A project-wide check (commits / task transitions / inbox)
can be layered later if false positives surface.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pollypm.acct.protocol import ProviderAdapter
    from pollypm.acct.model import AccountConfig
    from pollypm.storage.state import StateStore

logger = logging.getLogger(__name__)

DEFAULT_IDLE_THRESHOLD = timedelta(hours=2)


def _is_architect_role(role: str) -> bool:
    """Return True iff ``role`` matches an architect launch.

    PollyPM uses bare ``"architect"`` for first-class architect
    sessions, but tolerates the prefix form ``"architect-<project>"``
    that older configs sometimes set.
    """
    return role == "architect" or role.startswith("architect-")


def architect_idle_for(
    store: "StateStore",
    session_name: str,
    *,
    now: datetime | None = None,
    min_consistent_heartbeats: int = 3,
) -> timedelta | None:
    """Return how long ``session_name``'s pane has been idle.

    "Idle" = the most recent ``min_consistent_heartbeats`` heartbeats
    all carry the same ``snapshot_hash``. The returned duration is
    the wall-clock gap between the **oldest** of those heartbeats and
    ``now``.

    Returns ``None`` when:
    - fewer than ``min_consistent_heartbeats`` heartbeats exist
    - the recent hashes diverge (so the pane is still producing output)

    The min-consistent guard is what prevents a single missed render
    tick from triggering close. Three heartbeats matches the existing
    ``suspected_loop`` heuristic so we share intuition with the
    cockpit's idle alerts.
    """
    history = store.recent_heartbeats(session_name, limit=min_consistent_heartbeats)
    if len(history) < min_consistent_heartbeats:
        return None
    hashes = {h.snapshot_hash for h in history}
    if len(hashes) > 1:
        return None
    # ``recent_heartbeats`` returns newest-first; the oldest of the
    # consistent run is the last item.
    oldest = history[-1]
    try:
        oldest_dt = datetime.fromisoformat(oldest.created_at)
    except (ValueError, AttributeError):
        return None
    if oldest_dt.tzinfo is None:
        oldest_dt = oldest_dt.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    return current - oldest_dt


def should_close_architect(
    store: "StateStore",
    session_name: str,
    role: str,
    *,
    threshold: timedelta = DEFAULT_IDLE_THRESHOLD,
    now: datetime | None = None,
) -> bool:
    """Return True iff ``session_name`` is an architect that's idled past ``threshold``.

    Cheap-side filter so callers can skip non-architects without
    paying for the heartbeat lookup.
    """
    if not _is_architect_role(role):
        return False
    idle = architect_idle_for(store, session_name, now=now)
    if idle is None:
        return False
    return idle >= threshold


def close_idle_architect(
    *,
    store: "StateStore",
    provider: "ProviderAdapter",
    account: "AccountConfig",
    project_key: str,
    cwd: Path | None,
    tmux_kill_window: callable,
    window_target: str,
    last_active_at: str,
) -> str | None:
    """Capture resume token, persist it, then kill the architect window.

    Returns the captured ``session_id`` (None when the provider had
    no session to resume — in which case we still kill the window
    but skip token persistence).

    Idempotent: if the window is already gone, ``tmux_kill_window``
    is a no-op (existing implementations swallow "no such window").

    ``tmux_kill_window`` is passed in (rather than imported) so this
    function stays decoupled from the supervisor's concrete tmux
    client; the caller threads its own client through.
    """
    session_id = provider.latest_session_id(account, cwd)
    if session_id is not None:
        store.upsert_architect_resume_token(
            project_key=project_key,
            provider=provider.name,
            session_id=session_id,
            last_active_at=last_active_at,
        )
        logger.info(
            "Captured resume token for architect %s (provider=%s, session=%s)",
            project_key, provider.name, session_id,
        )
    else:
        logger.warning(
            "No %s session found for project %s — closing without resume token",
            provider.name, project_key,
        )
    try:
        tmux_kill_window(window_target)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Failed to kill architect window %s after token capture: %s",
            window_target, exc,
        )
    return session_id


def resolve_launch_argv(
    *,
    store: "StateStore",
    provider: "ProviderAdapter",
    account: "AccountConfig",
    project_key: str,
    fresh_args: list[str],
) -> tuple[list[str], bool]:
    """Build the argv for spawning an architect, resuming if a token exists.

    Returns ``(argv, resumed)`` where ``resumed`` is True iff the
    returned argv came from ``provider.resume_launch_cmd`` (caller
    can log the warm-start, fire telemetry, etc.).

    On resume, the stored token is **left in place** until the
    architect either successfully starts (caller's responsibility to
    confirm and clear) or it explicitly fails. Leaving the token
    means a crash partway through resume doesn't lose the warm
    context — the next attempt picks up from the same UUID.
    """
    record = store.get_architect_resume_token(project_key)
    if record is None or record.provider != provider.name:
        # No token, or token belongs to a different provider (e.g.
        # account was switched). Always fall back to fresh launch.
        return provider.worker_launch_cmd(account, fresh_args), False
    argv = provider.resume_launch_cmd(account, record.session_id, fresh_args)
    return argv, True


def clear_resume_token(store: "StateStore", project_key: str) -> None:
    """Drop a stored resume token (call after a successful resume)."""
    store.clear_architect_resume_token(project_key)


__all__ = [
    "DEFAULT_IDLE_THRESHOLD",
    "architect_idle_for",
    "should_close_architect",
    "close_idle_architect",
    "resolve_launch_argv",
    "clear_resume_token",
]
