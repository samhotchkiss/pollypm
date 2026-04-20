"""Resume-token helpers for the Codex provider.

Codex stores session transcripts under
``~/.codex/sessions/YYYY/MM/DD/rollout-<iso-timestamp>-<uuid>.jsonl``
(it does *not* bucket by working directory the way Claude Code does).
The trailing UUID is the session ID Codex accepts via the ``resume``
subcommand.

Two responsibilities mirror the Claude variant:

1. :func:`latest_session_id` — newest UUID across all rollouts under
   the account ``home``.
2. :func:`resume_argv` — argv for ``codex resume <id>`` under the
   PollyPM ``--dangerously-skip-permissions`` posture.

The lack of cwd-bucketing means PollyPM relies on **per-account-home
isolation** to keep architects from grabbing each other's sessions:
each architect runs in an isolated account home so its
``~/.codex/sessions/`` only contains its own rollouts.
"""

from __future__ import annotations

from pathlib import Path

# A Codex rollout filename ends with a UUIDv7-shaped tail:
# 8-4-4-4-12 hex digits joined by '-'. Splitting the basename on '-'
# and joining the last five segments reconstructs the UUID. This is
# the same parse the Codex CLI itself uses for its ``--last`` lookup.
_UUID_GROUPS = 5


def _parse_uuid(stem: str) -> str | None:
    """Extract the trailing UUID from a ``rollout-…-<uuid>`` stem."""
    parts = stem.split("-")
    if len(parts) < _UUID_GROUPS:
        return None
    return "-".join(parts[-_UUID_GROUPS:])


def latest_session_id(home: Path) -> str | None:
    """Return the newest Codex session UUID under ``home``.

    Walks ``home/.codex/sessions/`` recursively for ``rollout-*.jsonl``
    files and picks the one with the highest mtime. Returns ``None``
    when the directory doesn't exist or contains no rollouts.
    """
    sessions_root = home / ".codex" / "sessions"
    if not sessions_root.is_dir():
        return None
    newest_path: Path | None = None
    newest_mtime = -1.0
    for jsonl in sessions_root.rglob("rollout-*.jsonl"):
        try:
            mtime = jsonl.stat().st_mtime
        except OSError:
            continue
        if mtime > newest_mtime:
            newest_mtime = mtime
            newest_path = jsonl
    if newest_path is None:
        return None
    return _parse_uuid(newest_path.stem)


def resume_argv(session_id: str, extra_args: list[str] | None = None) -> list[str]:
    """Return ``codex`` argv for resuming ``session_id`` interactively.

    ``--dangerously-skip-permissions`` is a top-level Codex flag —
    it must precede the ``resume`` subcommand. The session UUID is
    the first positional argument to ``resume``.
    """
    extra = list(extra_args) if extra_args else []
    return [
        "codex",
        "--dangerously-skip-permissions",
        "resume",
        session_id,
        *extra,
    ]


__all__ = ["latest_session_id", "resume_argv"]
