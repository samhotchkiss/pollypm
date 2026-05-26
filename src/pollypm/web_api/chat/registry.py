"""Enumerate every chat surface PollyPM currently exposes.

Four surface types, all addressed by a unique ``session_name`` per
spec §1:

- **Operator** (``role="operator-pm"`` or ``name="operator"``) — the
  user-facing Polly persona. One per workspace.
- **Architect** (``role="architect"``) — per-project planner persona.
- **Advisor** (``role="advisor"``) — per-project strategic advisor.
- **Worker** (per-task, ``session_name = task-{project}-{N}``) — one
  per active task. Enumerated from the work-service ``WorkerSession``
  records, not from ``config.sessions`` (workers spawn on demand).

This module produces :class:`ChatSurface` dataclasses; the P2 router
serializes them into the spec §2.1 JSON shape. The registry is pure
read-only: never spawns sessions, never edits config, never touches
the database.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from pollypm.models import PollyPMConfig, SessionConfig
from pollypm.projects import project_transcripts_dir
from pollypm.session_health import storage_session_name
from pollypm.work.task_state import parse_task_window_name
from pollypm.work.session_manager import task_window_name
from pollypm.web_api.chat.transcripts import (
    build_session_index,
    lookup_transcript_path,
)

logger = logging.getLogger(__name__)


# ``SessionConfig.role`` values that the operator persona uses. We
# accept the legacy ``operator-pm`` role marker AND the convention of
# naming the session ``"operator"`` (older configs predate the
# explicit role taxonomy). Either match qualifies.
_OPERATOR_ROLES = frozenset({"operator-pm", "operator"})
_OPERATOR_NAMES = frozenset({"operator", "polly"})
_ARCHITECT_ROLES = frozenset({"architect"})
_ADVISOR_ROLES = frozenset({"advisor"})
_TMUX_DISCOVERY_TIMEOUT_SECONDS = 1


class SurfaceType(StrEnum):
    """The four chat surface types per spec §1."""

    OPERATOR = "operator"
    ARCHITECT = "architect"
    ADVISOR = "advisor"
    WORKER = "worker"


@dataclass(slots=True)
class TmuxWindowState:
    """Snapshot of the tmux window backing a surface.

    ``present`` is ``False`` when the configured window doesn't exist
    in tmux (e.g. session never started, or was killed). The GET
    endpoint still works against the on-disk transcript per spec §4.9;
    the POST endpoint returns ``503 window_missing``.
    """

    tmux_session: str
    window_name: str
    present: bool = False
    pane_id: str | None = None
    pane_dead: bool = False


@dataclass(slots=True)
class ChatSurface:
    """One chat surface descriptor.

    Fields:

    ``session_name`` — the canonical surface address (spec §1). For
    config sessions it's the ``config.sessions`` key; for workers it's
    :func:`task_window_name`.

    ``surface_type`` — :class:`SurfaceType`.

    ``persona`` — display name (e.g. ``"Polly"``, ``"Archie"``, the
    architect's persona) or ``None`` for workers.

    ``project`` — project key or ``None`` for the operator (the
    operator is workspace-wide, not project-bound).

    ``task_id`` — only set for worker surfaces.

    ``window`` — :class:`TmuxWindowState` describing the tmux backing.

    ``transcript_path`` — absolute path to the ``events.jsonl``
    archive, or ``None`` when no transcript has been ingested yet
    (brand-new surface; spec §4.8 says GET returns ``messages: []``).

    ``cwd`` — working directory the agent runs in. Used by the
    transcript resolver to pick the right archive when multiple
    provider-session UUIDs live in the same project root.

    ``provider`` — ``"claude"`` / ``"codex"`` / etc. Drives the
    capture-fallback decision per spec §4.12.

    ``auth_token_present`` — mirrors :attr:`SessionConfig.auth_token`
    so the spec §2.1 response can render the ``auth_token_present``
    field without leaking the token itself.

    ``worktree_path`` — only set for worker surfaces; the per-task
    worktree where the worker runs.
    """

    session_name: str
    surface_type: SurfaceType
    persona: str | None
    project: str | None
    window: TmuxWindowState
    transcript_path: Path | None = None
    task_id: int | None = None
    cwd: Path | None = None
    provider: str = ""
    auth_token_present: bool = False
    worktree_path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize for the spec §2.1 JSON response.

        Mirrors the shape in the spec verbatim; the P2 router can
        return this directly as the ``sessions[]`` entry.
        """
        return {
            "session_name": self.session_name,
            "surface_type": str(self.surface_type),
            "persona": self.persona,
            "project": self.project,
            "task_id": self.task_id,
            "window": {
                "tmux_session": self.window.tmux_session,
                "window_name": self.window.window_name,
                "present": self.window.present,
                "pane_id": self.window.pane_id,
                "pane_dead": self.window.pane_dead,
            },
            "transcript": {
                "source": "jsonl" if self.transcript_path else None,
                "path": str(self.transcript_path) if self.transcript_path else None,
            },
            "cwd": str(self.cwd) if self.cwd else None,
            "provider": self.provider,
            "auth_token_present": self.auth_token_present,
            "worktree_path": (
                str(self.worktree_path) if self.worktree_path else None
            ),
        }


def enumerate_chat_surfaces(
    config: PollyPMConfig,
    *,
    work_service: Any | None = None,
    tmux_client: Any | None = None,
    include_transcripts: bool = True,
) -> list[ChatSurface]:
    """Return every chat surface (operator/architect/advisor/worker).

    ``work_service`` is the :class:`pollypm.work.service.WorkService`
    used to enumerate active worker sessions. When ``None``, worker
    surfaces are skipped (useful for tests that only care about
    configured surfaces).

    ``tmux_client`` is used to populate :attr:`TmuxWindowState.present`
    and ``pane_id``. When ``None`` we don't probe tmux — the response
    still returns the configured ``window_name`` and the caller can
    treat ``present`` as "unknown" (defaults to ``False``).

    The returned list is ordered: operator first, then architects,
    then advisors, then workers (sorted by ``project`` then ``task_id``).
    Stable ordering keeps the JSON response deterministic for tests
    and lets clients render a stable sidebar.
    """
    tmux_state_cache = _build_tmux_state_cache(config, tmux_client)
    # Build the session-index ONCE per request — keyed by project root.
    # Codex review blocker #2: previously each surface re-scanned the
    # transcripts root inside ``resolve_transcript_path``. We now scan
    # each project's root at most once via :func:`_build_session_index`
    # and look up per surface (#2044).
    session_index_cache: dict[Path, list] = {}
    surfaces: list[ChatSurface] = []
    surfaces.extend(enumerate_config_surfaces(
        config,
        tmux_state_cache=tmux_state_cache,
        session_index_cache=session_index_cache,
        include_transcripts=include_transcripts,
    ))
    if work_service is not None:
        surfaces.extend(enumerate_worker_surfaces(
            config, work_service,
            tmux_state_cache=tmux_state_cache,
            session_index_cache=session_index_cache,
            include_transcripts=include_transcripts,
        ))
    surfaces.sort(key=_surface_sort_key)
    return surfaces


def _build_session_index(
    project_root: Path,
    cache: dict[Path, list] | None = None,
) -> list:
    """Return (and memoize) the session index for ``project_root``.

    Keyed by the resolved transcripts root so multiple surfaces in the
    same project share one scan, even when called via the two distinct
    enumerate-*-surfaces helpers in the same request. The cache is
    request-scoped (passed in by :func:`enumerate_chat_surfaces`); no
    module-level state.
    """
    transcripts_root = project_transcripts_dir(project_root)
    if cache is not None and transcripts_root in cache:
        return cache[transcripts_root]
    index = build_session_index(transcripts_root)
    if cache is not None:
        cache[transcripts_root] = index
    return index


def enumerate_config_surfaces(
    config: PollyPMConfig,
    *,
    tmux_state_cache: dict[str, TmuxWindowState] | None = None,
    session_index_cache: dict[Path, list] | None = None,
    include_transcripts: bool = True,
) -> list[ChatSurface]:
    """Enumerate operator/architect/advisor surfaces from ``config.sessions``.

    Workers are NOT included (they live in the work-service, not the
    config). Disabled sessions are also skipped — they aren't running
    so they can't be chatted with.

    ``session_index_cache`` memoizes the per-project session-index scan
    so the same project's transcripts root is read at most once per
    request even when multiple surfaces share it.
    """
    surfaces: list[ChatSurface] = []
    tmux_session = storage_session_name(config.project.tmux_session)
    for session_name, session in (config.sessions or {}).items():
        surface = _config_surface(
            config,
            session_name,
            session,
            tmux_session=tmux_session,
            tmux_state_cache=tmux_state_cache,
            session_index_cache=session_index_cache,
            include_transcripts=include_transcripts,
        )
        if surface is not None:
            surfaces.append(surface)
    return surfaces


def enumerate_worker_surfaces(
    config: PollyPMConfig,
    work_service: Any,
    *,
    tmux_state_cache: dict[str, TmuxWindowState] | None = None,
    session_index_cache: dict[Path, list] | None = None,
    include_transcripts: bool = True,
) -> list[ChatSurface]:
    """Enumerate per-task worker surfaces from the work-service.

    Reads active :class:`WorkerSessionRecord`s and computes the
    canonical ``task-{project}-{N}`` session_name for each.

    ``session_index_cache`` memoizes the per-project session-index scan
    so multiple workers (and any config surface) under the same project
    share one filesystem walk.
    """
    list_fn = getattr(work_service, "list_worker_sessions", None)
    if not callable(list_fn):
        logger.debug(
            "chat.registry: work_service has no list_worker_sessions; "
            "skipping worker surfaces",
        )
        return []
    try:
        records = list_fn(active_only=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "chat.registry: list_worker_sessions failed: %s", exc,
        )
        return []
    surfaces: list[ChatSurface] = []
    tmux_session = storage_session_name(config.project.tmux_session)
    for record in records or []:
        surface = _worker_surface(
            config,
            record,
            tmux_session=tmux_session,
            tmux_state_cache=tmux_state_cache,
            session_index_cache=session_index_cache,
            include_transcripts=include_transcripts,
        )
        if surface is not None:
            surfaces.append(surface)
    return surfaces


def find_chat_surface(
    config: PollyPMConfig,
    session_name: str,
    *,
    work_service: Any | None = None,
    tmux_client: Any | None = None,
    include_transcripts: bool = True,
) -> ChatSurface | None:
    """Resolve one chat surface without enumerating the whole workspace.

    ``GET /chat/{name}/messages`` is a single-session hot path. The
    broad discovery helper intentionally scans all configured sessions
    and worker rows so the sidebar can render everything, but doing that
    before every transcript read makes message latency proportional to
    workspace size. This resolver builds the same ``ChatSurface`` shape
    for only the requested session.
    """
    tmux_state_cache = _build_tmux_state_cache(config, tmux_client)
    session_index_cache: dict[Path, list] = {}

    session = (config.sessions or {}).get(session_name)
    if session is not None:
        return _config_surface(
            config,
            session_name,
            session,
            tmux_session=storage_session_name(config.project.tmux_session),
            tmux_state_cache=tmux_state_cache,
            session_index_cache=session_index_cache,
            include_transcripts=include_transcripts,
        )

    parsed = parse_task_window_name(session_name)
    if parsed is None or work_service is None:
        return None
    project, task_number = parsed
    list_fn = getattr(work_service, "list_worker_sessions", None)
    if not callable(list_fn):
        return None
    try:
        try:
            records = list_fn(project=project, active_only=True)
        except TypeError:
            records = list_fn(active_only=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("chat.registry: list_worker_sessions failed: %s", exc)
        return None
    for record in records or []:
        if (
            getattr(record, "task_project", "") == project
            and int(getattr(record, "task_number", 0) or 0) == task_number
        ):
            return _worker_surface(
                config,
                record,
                tmux_session=storage_session_name(config.project.tmux_session),
                tmux_state_cache=tmux_state_cache,
                session_index_cache=session_index_cache,
                include_transcripts=include_transcripts,
            )
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config_surface(
    config: PollyPMConfig,
    session_name: str,
    session: SessionConfig,
    *,
    tmux_session: str,
    tmux_state_cache: dict[str, TmuxWindowState] | None,
    session_index_cache: dict[Path, list] | None,
    include_transcripts: bool,
) -> ChatSurface | None:
    if not session.enabled:
        return None
    surface_type = _classify_session(session)
    if surface_type is None:
        return None
    project_key = _project_key_for_session(config, session, surface_type)
    project_root = _project_root_for_key(config, project_key)
    persona = _persona_for_session(config, session, surface_type)
    cwd = session.cwd if isinstance(session.cwd, Path) else Path(session.cwd or ".")
    transcript_path: Path | None = None
    if include_transcripts:
        index = _build_session_index(project_root, session_index_cache)
        transcript_path = lookup_transcript_path(
            index,
            cwd=str(cwd),
            account_name=session.account,
            provider=str(session.provider),
        )
    window_name = session.window_name or session_name
    if tmux_state_cache and window_name in tmux_state_cache:
        window_state = tmux_state_cache[window_name]
    else:
        window_state = TmuxWindowState(
            tmux_session=tmux_session,
            window_name=window_name,
            present=False,
        )
    return ChatSurface(
        session_name=session_name,
        surface_type=surface_type,
        persona=persona,
        project=project_key if surface_type != SurfaceType.OPERATOR else None,
        window=window_state,
        transcript_path=transcript_path,
        cwd=cwd,
        provider=str(session.provider),
        auth_token_present=bool(session.auth_token),
    )


def _worker_surface(
    config: PollyPMConfig,
    record: Any,
    *,
    tmux_session: str,
    tmux_state_cache: dict[str, TmuxWindowState] | None,
    session_index_cache: dict[Path, list] | None,
    include_transcripts: bool,
) -> ChatSurface | None:
    project = getattr(record, "task_project", "") or ""
    task_number = getattr(record, "task_number", 0)
    if not project or not task_number:
        return None
    session_name = task_window_name(project, task_number)
    project_root = _project_root_for_key(config, project)
    worktree_path = (
        Path(record.worktree_path) if record.worktree_path else None
    )
    cwd_for_lookup = str(worktree_path) if worktree_path else None
    provider_value = getattr(record, "provider", "") or ""
    transcript_path: Path | None = None
    if include_transcripts:
        index = _build_session_index(project_root, session_index_cache)
        transcript_path = lookup_transcript_path(
            index,
            cwd=cwd_for_lookup,
            account_name=None,  # WorkerSessionRecord doesn't carry account
            provider=provider_value or None,
        )
    window_name = session_name
    if tmux_state_cache and window_name in tmux_state_cache:
        window_state = tmux_state_cache[window_name]
    else:
        window_state = TmuxWindowState(
            tmux_session=tmux_session,
            window_name=window_name,
            present=False,
            pane_id=getattr(record, "pane_id", None),
        )
    return ChatSurface(
        session_name=session_name,
        surface_type=SurfaceType.WORKER,
        persona=None,
        project=project,
        window=window_state,
        transcript_path=transcript_path,
        task_id=int(task_number),
        cwd=worktree_path,
        provider=provider_value,
        auth_token_present=False,
        worktree_path=worktree_path,
    )


def _classify_session(session: SessionConfig) -> SurfaceType | None:
    """Pick the surface type for a ``SessionConfig`` or return ``None``.

    Returns ``None`` for roles that aren't chat-surface-shaped
    (heartbeat-supervisor, reviewer, triage, etc.).
    """
    role = (session.role or "").lower().strip()
    name = (session.name or "").lower().strip()
    if role in _OPERATOR_ROLES or name in _OPERATOR_NAMES:
        return SurfaceType.OPERATOR
    if role in _ARCHITECT_ROLES or name.startswith("architect"):
        return SurfaceType.ARCHITECT
    if role in _ADVISOR_ROLES or name.startswith("advisor"):
        return SurfaceType.ADVISOR
    return None


def _project_key_for_session(
    config: PollyPMConfig,
    session: SessionConfig,
    surface_type: SurfaceType,
) -> str:
    """Pick the project key the session is bound to.

    Operators are workspace-wide so we return the workspace's default
    project (``config.project.name``). Architects/advisors are
    explicitly per-project via ``SessionConfig.project``.
    """
    if surface_type == SurfaceType.OPERATOR:
        return session.project or config.project.name
    return session.project or config.project.name


def _persona_for_session(
    config: PollyPMConfig,
    session: SessionConfig,
    surface_type: SurfaceType,
) -> str | None:
    """Compute the display persona for a surface.

    For operators we hard-code ``"Polly"`` (the global PM persona).
    For architects and advisors we read the project's
    :attr:`KnownProject.persona_name` so per-project personas show up
    in the response.
    """
    if surface_type == SurfaceType.OPERATOR:
        return "Polly"
    project_key = session.project or config.project.name
    project = (config.projects or {}).get(project_key)
    if project and getattr(project, "persona_name", None):
        return project.persona_name
    # Fall back to the role-derived display name so the response is
    # never None for configured persona surfaces.
    if surface_type == SurfaceType.ARCHITECT:
        return "Archie"
    if surface_type == SurfaceType.ADVISOR:
        return "Advisor"
    return None


def _project_root_for_key(config: PollyPMConfig, project_key: str) -> Path:
    project = (config.projects or {}).get(project_key)
    if project is not None:
        return project.path
    return config.project.root_dir


def _build_tmux_state_cache(
    config: PollyPMConfig,
    tmux_client: Any | None,
) -> dict[str, TmuxWindowState]:
    """Probe tmux once and index ``TmuxWindowState`` by window name.

    Calling ``list_windows`` once is cheaper than calling
    ``has_session`` per surface, and lets us populate ``pane_id`` /
    ``pane_dead`` in the same pass. Returns an empty dict when
    ``tmux_client`` is ``None`` or the probe fails.
    """
    cache: dict[str, TmuxWindowState] = {}
    if tmux_client is None:
        return cache
    list_windows = getattr(tmux_client, "list_windows", None)
    if not callable(list_windows):
        return cache
    base = getattr(config.project, "tmux_session", "") or ""
    target = storage_session_name(base)
    try:
        try:
            windows = list_windows(target, timeout=_TMUX_DISCOVERY_TIMEOUT_SECONDS)
        except TypeError:
            # Test doubles and older TmuxClient-compatible adapters may
            # not accept the timeout keyword. Keep the registry protocol
            # permissive while the real client uses the bounded probe.
            windows = list_windows(target)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "chat.registry: tmux list_windows(%s) failed: %s",
            target, exc,
        )
        return cache
    for window in windows or []:
        window_name = getattr(window, "name", "") or ""
        if not window_name:
            continue
        cache[window_name] = TmuxWindowState(
            tmux_session=getattr(window, "session", target),
            window_name=window_name,
            present=True,
            pane_id=getattr(window, "pane_id", None),
            pane_dead=bool(getattr(window, "pane_dead", False)),
        )
    return cache


def _surface_sort_key(surface: ChatSurface) -> tuple[int, str, int]:
    """Stable ordering — operator first, then alphabetical project."""
    type_order = {
        SurfaceType.OPERATOR: 0,
        SurfaceType.ARCHITECT: 1,
        SurfaceType.ADVISOR: 2,
        SurfaceType.WORKER: 3,
    }
    return (
        type_order.get(surface.surface_type, 99),
        surface.project or "",
        surface.task_id or 0,
    )


__all__ = [
    "ChatSurface",
    "SurfaceType",
    "TmuxWindowState",
    "enumerate_chat_surfaces",
    "enumerate_config_surfaces",
    "enumerate_worker_surfaces",
    "find_chat_surface",
]
