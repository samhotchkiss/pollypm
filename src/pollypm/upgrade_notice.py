"""In-session ``<system-update>`` notice injection (#718).

**Default-disabled as of #755/#763.** Models treat a ``<system-update>``
tag delivered via user-turn input as a prompt-injection attack pattern
(correctly — the tag has no provenance the model can verify). So the
default ``pm upgrade`` path no longer calls into this module; post-
upgrade behavior is driven by the sentinel flag at
``~/.pollypm/post-upgrade.flag`` and the cockpit restart-nudge from #719.

The module remains available for explicit opt-in use (e.g. a future
``pm upgrade --force-notify`` flag or debug scripts). When it IS
invoked, it now resolves role-guide paths to **absolute** locations
inside the target project's ``.pollypm/`` so sessions can actually
resolve them regardless of their working directory (#762/#763), and it
rejects calls with test-fixture version pairs (#756) unless explicitly
allowed.

Resolution priority for each session's role-guide path:
1. Project-local fork at ``<project>/.pollypm/project-guides/<role>.md``
   (from #733).
2. Built-in source from ``pollypm.project_guides.built_in_guide_source_path``.

Consumed by ``pm upgrade`` (#716) only when explicit opt-in is set.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable


logger = logging.getLogger(__name__)


class FixtureLeakError(RuntimeError):
    """Raised when ``inject_system_update_notice`` is called with version
    strings that match the canonical test fixtures.

    See #756 — a production path was hitting live supervisors with
    ``("0.1.0", "0.2.0")``, the exact fixture pair from
    ``tests/test_upgrade_notice.py``. That caller has been removed, but
    the guard stays to catch future leaks loudly instead of silently.
    """


# Fixture values from tests/test_upgrade_notice.py. Any call matching
# this pair is presumed to be a leak from a test or scratch script.
_FIXTURE_VERSION_PAIR: tuple[str, str] = ("0.1.0", "0.2.0")


# Roles that don't participate in the notice — they're control / infra
# sessions without an LLM in the loop.
_SKIP_ROLES: frozenset[str] = frozenset({
    "heartbeat-supervisor",
    "heartbeat",
})


@dataclass(slots=True)
class NoticeResult:
    session_name: str
    role: str
    delivered: bool
    reason: str  # "sent" | "skipped: <role>" | "no guide" | "send failed: <err>"


def _resolve_role_guide_path(role: str, project_path: Path | None) -> Path | None:
    """Return the absolute path of ``role``'s guide for a project, or None.

    Delegates to :func:`pollypm.project_paths.role_guide_path` (#763) —
    the single source of truth for "where does role X's guide live
    for project Y." Skip-roles (heartbeat / heartbeat-supervisor)
    return None so the caller can bucket them correctly.

    When ``project_path`` is None (no project context), falls back to
    the built-in operator guide for operator-pm and the shipped
    russell.md for reviewer; other roles return None because there's
    no sensible target absent a project directory.
    """
    if role in _SKIP_ROLES:
        return None

    try:
        from pollypm.project_paths import role_guide_path
    except ImportError:
        return None

    if project_path is None:
        # No project context (fake supervisor in tests, or a session
        # without a project field set). Fall back to the shipped
        # absolute path from the package — the same target
        # role_guide_path would resolve to once materialization runs.
        return _built_in_guide_path_for_role(role)

    try:
        resolved = role_guide_path(project_path, role)
    except Exception:  # noqa: BLE001
        return None
    # role_guide_path always returns a Path; we only want to return
    # one the file actually exists at — callers build a notice body
    # around this and shouldn't reference a non-existent file.
    if resolved.exists():
        return resolved.resolve()

    # For the reviewer, built_in_guide_source_path returns defaults.py
    # (the module that builds russell_prompt()) which isn't a useful
    # reread target. role_guide_path normalizes to the same path, so
    # the fallback below only kicks in when BOTH the project-local
    # fork and the shipped russell.md are missing — pathological but
    # we handle it explicitly.
    if role == "reviewer":
        try:
            base = Path(__file__).resolve().parent
            russell_md = (
                base
                / "plugins_builtin"
                / "core_agent_profiles"
                / "profiles"
                / "russell.md"
            )
            if russell_md.exists():
                return russell_md
        except Exception:  # noqa: BLE001
            pass
    return None


def _built_in_guide_path_for_role(role: str) -> Path | None:
    """Return the absolute shipped-with-package path for ``role``.

    Used as the fallback when no project context is available. Maps
    unknown roles to the worker guide (same behavior as
    :func:`pollypm.project_paths.role_guide_path`).
    """
    base = Path(__file__).resolve().parent
    if role == "operator-pm":
        candidate = (
            base
            / "plugins_builtin"
            / "core_agent_profiles"
            / "profiles"
            / "polly-operator-guide.md"
        )
        return candidate if candidate.exists() else None

    if role == "reviewer":
        candidate = (
            base
            / "plugins_builtin"
            / "core_agent_profiles"
            / "profiles"
            / "russell.md"
        )
        return candidate if candidate.exists() else None

    # architect/worker/unknown: route through project_guides for the
    # shipped path. Architect's built-in lives under project_planning;
    # worker's built-in lives at repo-root docs/worker-guide.md.
    lookup = role if role in {"architect", "worker"} else "worker"
    try:
        from pollypm.project_guides import built_in_guide_source_path
        path = built_in_guide_source_path(lookup)
    except Exception:  # noqa: BLE001
        return None
    if path is not None and path.exists():
        return path.resolve()
    return None


def _guide_path_for_role(role: str, project_path: Path | None = None) -> Path | None:
    """Legacy-name shim; forwards to the absolute-path resolver."""
    return _resolve_role_guide_path(role, project_path)


def build_notice(old_version: str, new_version: str, guide_path: str) -> str:
    """Render the canonical ``<system-update>`` notice text.

    Structure is load-bearing: named version bump + exact guide path +
    explicit "supersedes prior instructions" framing + "pause on
    conflict" instruction. This is the text prior prompt-engineering
    rounds settled on — models are measurably more compliant with it
    than with a casual "we updated, fyi" note.
    """
    return (
        "<system-update>\n"
        f"PollyPM was upgraded from v{old_version} → v{new_version} while "
        "this session was running.\n"
        f"Before your next action, re-read your operating guide at {guide_path}.\n"
        "It supersedes any prior operating instructions in this conversation.\n"
        "If anything in the new guide conflicts with what you were about to "
        "do, pause and re-plan from the updated instructions.\n"
        "</system-update>"
    )


def _send_to_session(
    tmux: Any,
    *,
    target: str,
    text: str,
    send_keys: Callable[..., Any] | None = None,
) -> tuple[bool, str]:
    """Send ``text`` to ``target`` via the supervisor's tmux client.

    Returns ``(success, detail)``. Failures are logged at DEBUG and
    surfaced in the detail string so the caller can record which
    sessions were unreachable.
    """
    sender = send_keys
    if sender is None:
        sender = getattr(tmux, "send_keys", None)
    if sender is None:
        return (False, "tmux client has no send_keys")
    try:
        sender(target, text, press_enter=True)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "upgrade-notice: send_keys failed for %s: %s", target, exc,
            exc_info=True,
        )
        return (False, f"send failed: {type(exc).__name__}")
    return (True, "sent")


def _iter_launches(supervisor: Any) -> Iterable[Any]:
    """Best-effort iteration over the supervisor's live session specs.

    Supports both real :class:`Supervisor` instances (plan_launches())
    and the duck-typed fakes used in tests.
    """
    plan = getattr(supervisor, "plan_launches", None)
    if callable(plan):
        try:
            return list(plan())
        except Exception:  # noqa: BLE001
            return []
    launches = getattr(supervisor, "launches", None)
    if launches is not None:
        try:
            return list(launches)
        except TypeError:
            return []
    return []


def _target_for_launch(supervisor: Any, launch: Any) -> str:
    """Return the tmux ``session:window`` target for ``launch``.

    Falls back gracefully when the supervisor doesn't expose a resolver
    — worst case we send to ``<session-name>:<window-name>`` which is
    the canonical format tmux accepts.
    """
    session = getattr(launch, "session", None)
    window_name = getattr(launch, "window_name", None) or getattr(session, "window_name", None)
    resolver = getattr(supervisor, "_tmux_session_for_session", None)
    if callable(resolver) and session is not None:
        try:
            tmux_session = resolver(session.name)
        except Exception:  # noqa: BLE001
            tmux_session = None
    else:
        tmux_session = None
    if not tmux_session:
        tmux_session = getattr(
            getattr(supervisor, "config", None), "project", None,
        )
        tmux_session = getattr(tmux_session, "tmux_session", None) or "pollypm"
    return f"{tmux_session}:{window_name or session.name if session else 'polly'}"


def inject_system_update_notice(
    old_version: str,
    new_version: str,
    *,
    supervisor: Any | None = None,
    config_path: Path | None = None,
    send_keys: Callable[..., Any] | None = None,
    allow_fixture_versions: bool = False,
) -> list[NoticeResult]:
    """Deliver the ``<system-update>`` notice to every live session.

    Returns one :class:`NoticeResult` per session attempted (including
    skipped ones) so the caller can render a summary (#720 uses this
    for the "3 sessions notified" count).

    Supervisor can be passed in for tests; production call from
    ``pm upgrade`` constructs it via :class:`PollyPMService`. Guide
    paths are resolved to **absolute** locations (project-local fork if
    present, else built-in) so sessions resolve them regardless of CWD.

    Raises :class:`FixtureLeakError` if called with the canonical test
    fixture version pair (``"0.1.0" → "0.2.0"``) and
    ``allow_fixture_versions`` is not explicitly set. This prevents a
    test harness from accidentally spamming live supervisors (#756).
    """
    if (
        (old_version, new_version) == _FIXTURE_VERSION_PAIR
        and not allow_fixture_versions
    ):
        raise FixtureLeakError(
            f"Refusing to inject_system_update_notice with fixture version "
            f"pair {_FIXTURE_VERSION_PAIR!r}. Pass allow_fixture_versions=True "
            "explicitly if this is intentional (only tests should)."
        )

    logger.info(
        "inject_system_update_notice called: old=%s new=%s supervisor_provided=%s",
        old_version,
        new_version,
        supervisor is not None,
    )

    if supervisor is None:
        supervisor = _load_supervisor(config_path)
        if supervisor is None:
            return []

    config = getattr(supervisor, "config", None)
    projects = getattr(config, "projects", {}) if config is not None else {}

    tmux = getattr(supervisor, "tmux", None)
    results: list[NoticeResult] = []
    for launch in _iter_launches(supervisor):
        session = getattr(launch, "session", None)
        if session is None:
            continue
        role = getattr(session, "role", "") or ""
        name = getattr(session, "name", "") or "unknown"
        project_key = getattr(session, "project", None)
        project_path: Path | None = None
        if project_key and isinstance(projects, dict):
            project = projects.get(project_key)
            candidate = getattr(project, "path", None)
            if candidate is not None:
                project_path = Path(candidate)
        guide_path = _resolve_role_guide_path(role, project_path)
        if guide_path is None:
            results.append(NoticeResult(
                session_name=name, role=role, delivered=False,
                reason=f"skipped: {role or 'no role'}",
            ))
            continue
        target = _target_for_launch(supervisor, launch)
        notice = build_notice(old_version, new_version, str(guide_path))
        ok, detail = _send_to_session(
            tmux, target=target, text=notice, send_keys=send_keys,
        )
        results.append(NoticeResult(
            session_name=name, role=role, delivered=ok, reason=detail,
        ))
    return results


def _load_supervisor(config_path: Path | None) -> Any | None:
    """Best-effort supervisor load. Swallows failures so ``pm upgrade``
    doesn't abort its whole flow when we can't reach the session layer
    (e.g. user isn't in a tmux session right now)."""
    try:
        from pollypm.config import DEFAULT_CONFIG_PATH
        from pollypm.service_api import PollyPMService
    except ImportError:
        return None
    path = config_path or DEFAULT_CONFIG_PATH
    if not path.exists():
        return None
    try:
        service = PollyPMService(path)
        return service.load_supervisor()
    except Exception:  # noqa: BLE001
        return None


def summarize(results: list[NoticeResult]) -> tuple[int, int, int]:
    """Return ``(notified, skipped, failed)`` counts for the rail
    summary in #720."""
    notified = sum(1 for r in results if r.delivered)
    skipped = sum(1 for r in results if not r.delivered and r.reason.startswith("skipped"))
    failed = sum(1 for r in results if not r.delivered and not r.reason.startswith("skipped"))
    return notified, skipped, failed
