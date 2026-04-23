"""Claude-specific tmux session argv assembly — issue #406.

The PollyPM launcher chooses Claude CLI flags based on the role of
the session being launched (operator-pm, heartbeat-supervisor,
worker, …). Those role-to-flag mappings are Claude-specific — they
list Claude tool names — so they live in the provider package
instead of leaking into :mod:`pollypm.onboarding`.

Public API:
    :func:`session_args` — returns the argv list to append to
    ``["claude", *user_args]`` for a session with the given role and
    permissions posture.
"""

from __future__ import annotations


_OPERATOR_TOOLS = "Read,Glob,Grep,LS,Bash,WebFetch,WebSearch,TodoWrite,Task"
_HEARTBEAT_TOOLS = "Read,Glob,Grep,LS,WebFetch,WebSearch,TodoWrite,Task"
_NO_WRITE_TOOLS = "Edit,Write,MultiEdit,NotebookEdit"
# PM delegates — never writes files itself.
_OPERATOR_DISALLOWED = "Agent,Edit,Write,MultiEdit,NotebookEdit"


def session_args(
    *,
    open_permissions: bool = True,
    role: str = "",
    model: str | None = None,
) -> list[str]:
    """Return the Claude CLI argv tail for a session of ``role``.

    Behavior:

    * Worker / unrecognised role + ``open_permissions=True`` →
      ``["--dangerously-skip-permissions"]`` so the worker pane never
      blocks on permission prompts.
    * ``heartbeat-supervisor`` → narrow read-only tool list with
      writes explicitly disallowed.
    * ``operator-pm`` → operator tool list (adds ``Bash``) with
      writes + sub-Agent delegation explicitly disallowed.
    * Control roles never get ``--dangerously-skip-permissions`` —
      the role-locked tool lists are the safer expression of intent.
    """
    args: list[str] = []
    if open_permissions and role not in {"heartbeat-supervisor", "operator-pm"}:
        args.append("--dangerously-skip-permissions")
    if role == "heartbeat-supervisor":
        args.extend(["--allowedTools", _HEARTBEAT_TOOLS])
        args.extend(["--disallowedTools", _NO_WRITE_TOOLS])
    elif role == "operator-pm":
        args.extend(["--allowedTools", _OPERATOR_TOOLS])
        args.extend(["--disallowedTools", _OPERATOR_DISALLOWED])
    if model:
        args.extend(["--model", model])
    return args


__all__ = ["session_args"]
