"""In-session ``<system-update>`` notice injection (#718).

When PollyPM is upgraded while sessions are live, ``claude --resume`` /
``codex --resume`` preserve the original conversation — the system
prompt from turn 1 stays baked into history and new system prompts are
NOT merged. Killing + relaunching sessions would work but drops
in-flight context.

This module solves that by injecting a user-turn notice into each live
session telling the model to re-read its role guide from disk (which
IS the new, post-upgrade version). The model can then converge on the
new instructions at its next turn boundary without losing any
conversation state.

Consumed by ``pm upgrade`` (#716) after a successful install.

Limitations:

* Models aren't perfectly adherent to "disregard prior instructions"
  framing. The 90% case converges cleanly; the 10% case benefits from
  the hard-recycle escape hatch in #720.
* Sessions that are mid-tool-call see the notice as a pending user
  message — Claude Code / Codex render it at the next turn break.
  Don't inject when a session is actively emitting tokens.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable


logger = logging.getLogger(__name__)


# Role → guide path (relative to the pollypm install root).
# Keeps the mapping small and explicit; new roles default to the worker
# guide unless listed here.
_ROLE_GUIDES: dict[str, str] = {
    "worker": "docs/worker-guide.md",
    "operator-pm": (
        "src/pollypm/plugins_builtin/core_agent_profiles/profiles/"
        "polly-operator-guide.md"
    ),
    "reviewer": (
        "src/pollypm/plugins_builtin/core_agent_profiles/profiles/russell.md"
    ),
    "architect": (
        "src/pollypm/plugins_builtin/core_agent_profiles/profiles/architect.md"
    ),
}

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


def _guide_path_for_role(role: str) -> str | None:
    if role in _SKIP_ROLES:
        return None
    return _ROLE_GUIDES.get(role, _ROLE_GUIDES.get("worker"))


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
) -> list[NoticeResult]:
    """Deliver the ``<system-update>`` notice to every live session.

    Returns one :class:`NoticeResult` per session attempted (including
    skipped ones) so the caller can render a summary (#720 uses this
    for the "3 sessions notified" count).

    Supervisor can be passed in for tests; production call from
    ``pm upgrade`` constructs it via :class:`PollyPMService`.
    """
    if supervisor is None:
        supervisor = _load_supervisor(config_path)
        if supervisor is None:
            return []

    tmux = getattr(supervisor, "tmux", None)
    results: list[NoticeResult] = []
    for launch in _iter_launches(supervisor):
        session = getattr(launch, "session", None)
        if session is None:
            continue
        role = getattr(session, "role", "") or ""
        name = getattr(session, "name", "") or "unknown"
        guide = _guide_path_for_role(role)
        if guide is None:
            results.append(NoticeResult(
                session_name=name, role=role, delivered=False,
                reason=f"skipped: {role or 'no role'}",
            ))
            continue
        target = _target_for_launch(supervisor, launch)
        notice = build_notice(old_version, new_version, guide)
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
