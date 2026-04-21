"""Resume-token helpers for the Claude provider.

Two responsibilities:

1. **Latest session discovery** — given an account ``home`` and a project
   ``cwd``, find the UUID of the most recent Claude Code session for
   that working directory. Claude Code stores session transcripts at
   ``~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl`` where
   ``encoded-cwd`` is the absolute cwd with ``/`` replaced by ``-``.

2. **Resume argv construction** — given a session UUID, build the
   ``claude`` argv that resumes it interactively under the same
   ``--dangerously-skip-permissions`` posture PollyPM uses for fresh
   architect launches.

Designed for the architect 2-hour idle-close flow: the supervisor
captures the latest UUID, kills the tmux window, persists the UUID
in ``architect_resume_tokens``, and uses :func:`resume_argv` to
relaunch when the project becomes active again.
"""

from __future__ import annotations

from pathlib import Path


def _encoded_cwd(cwd: Path) -> str:
    """Return the directory name Claude Code uses to bucket sessions for ``cwd``.

    Claude Code encodes absolute paths by replacing ``/`` with ``-`` —
    e.g. ``/private/tmp/passgen`` → ``-private-tmp-passgen``. We resolve
    the path first so symlink games can't desync the bucket name.
    """
    resolved = str(cwd.resolve())
    return resolved.replace("/", "-")


def latest_session_id(home: Path, cwd: Path) -> str | None:
    """Return the newest Claude session UUID under ``home`` for project ``cwd``.

    ``home`` is the account home dir (the ``CLAUDE_CONFIG_DIR``
    parent). Returns ``None`` when:

    - the encoded-cwd bucket directory doesn't exist (no sessions yet)
    - the bucket exists but contains no ``*.jsonl`` files

    The newest file by mtime wins; its stem (filename without
    ``.jsonl``) is the session UUID Claude Code accepts via
    ``--resume``.
    """
    ids = session_ids(home, cwd)
    if not ids:
        return None
    return ids[0]


def session_ids(home: Path, cwd: Path) -> list[str]:
    """Return Claude transcript UUIDs for ``cwd`` newest-first.

    The UUIDs are the ``.stem`` values of transcript ``*.jsonl`` files
    under Claude's encoded-cwd bucket. Newest ``mtime`` wins so callers
    can compare a pre-launch snapshot against the post-launch bucket and
    detect the fresh transcript a new tmux window created.
    """
    bucket = home / ".claude" / "projects" / _encoded_cwd(cwd)
    if not bucket.is_dir():
        return []
    candidates = [p for p in bucket.iterdir() if p.suffix == ".jsonl"]
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [candidate.stem for candidate in candidates]


def recorded_session_id(marker: Path) -> str | None:
    """Read a previously-recorded Claude session UUID from ``marker``.

    The marker is PollyPM-owned state under ``.pollypm/session-markers``.
    When it contains a non-empty line, that line is the exact Claude
    transcript UUID we should pass to ``claude --resume``.
    """
    try:
        raw = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return raw or None


def resume_argv(session_id: str, extra_args: list[str] | None = None) -> list[str]:
    """Return ``claude`` argv for resuming ``session_id`` interactively.

    Mirrors the fresh-launch posture PollyPM uses for architects: the
    permission prompts are pre-bypassed via
    ``--dangerously-skip-permissions`` (the architect runs in an
    isolated worktree and is sandboxed at the worktree level).

    ``extra_args`` is appended verbatim after the resume marker so
    callers can pin the agent profile, effort level, etc.
    """
    extra = list(extra_args) if extra_args else []
    return [
        "claude",
        "--dangerously-skip-permissions",
        "--resume",
        session_id,
        *extra,
    ]

__all__ = ["latest_session_id", "recorded_session_id", "resume_argv", "session_ids"]
