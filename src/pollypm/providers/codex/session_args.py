"""Codex-specific tmux session argv assembly — issue #406.

Mirror of :mod:`pollypm.providers.claude.session_args`. The Codex CLI
uses sandbox + approval flags rather than tool allow/deny lists, so
the role-to-flag mapping is entirely different — keeping it in the
Codex provider package means onboarding's launcher does not need to
know about either provider's flag vocabulary.
"""

from __future__ import annotations


def session_args(
    *,
    open_permissions: bool = True,
    role: str = "",
    model: str | None = None,
) -> list[str]:
    """Return the Codex CLI argv tail for a session of ``role``.

    Behavior:

    * Control roles (``heartbeat-supervisor``, ``operator-pm``) →
      ``--sandbox read-only --ask-for-approval never`` so the PM
      pane is non-mutating and runs unattended.
    * ``worker`` → ``--sandbox workspace-write --ask-for-approval never``
      so workers can edit files inside their workspace without
      blocking on prompts.
    * Anything else with ``open_permissions=True`` →
      ``--dangerously-bypass-approvals-and-sandbox`` (the historical
      default for ad-hoc launches).
    """
    if role in {"heartbeat-supervisor", "operator-pm"}:
        args = ["--sandbox", "read-only", "--ask-for-approval", "never"]
    elif role == "worker":
        args = ["--sandbox", "workspace-write", "--ask-for-approval", "never"]
    elif open_permissions:
        args = ["--dangerously-bypass-approvals-and-sandbox"]
    else:
        args = []
    if model:
        args.extend(["--model", model])
    return args


__all__ = ["session_args"]
