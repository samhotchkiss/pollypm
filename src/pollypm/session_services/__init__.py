"""Session service factory."""

from __future__ import annotations

from pathlib import Path

from pollypm.session_services.base import SessionHandle, SessionHealth, SessionService, TranscriptStream

__all__ = [
    "SessionHandle",
    "SessionHealth",
    "SessionService",
    "TranscriptStream",
    "attach_existing_session",
    "create_tmux_client",
    "current_session_name",
    "get_session_service",
    "probe_session",
    "switch_client_to_session",
]


def get_session_service(name: str, *, root_dir: Path | None = None, **kwargs: object) -> SessionService:
    """Resolve a session service implementation by name via the plugin host."""
    from pollypm.plugin_host import extension_host_for_root

    root = str((root_dir or Path.cwd()).resolve())
    return extension_host_for_root(root).get_session_service(name, **kwargs)


def create_tmux_client():
    """Create a standalone TmuxClient instance.

    Use this when you need raw tmux access outside of a full session service
    (e.g. login windows, usage probes, onboarding).
    """
    from pollypm.tmux.client import TmuxClient

    return TmuxClient()


# ---------------------------------------------------------------------------
# Pre-config bootstrap helpers
# ---------------------------------------------------------------------------
#
# CLI entry points need to probe / attach / switch to a candidate session
# before any config has been loaded. The plugin host needs a root_dir and a
# config, so at bootstrap time we can't resolve a full SessionService. These
# helpers are thin wrappers over the tmux primitives that the default
# SessionService would use, exposed through the session_services package so
# callers don't reach into the concrete TmuxSessionService class.
#
# Non-tmux session services (when we have them) can add their own bootstrap
# helpers alongside these.


def probe_session(name: str) -> bool:
    """Return True if a session with this name exists at the tmux layer."""
    return create_tmux_client().has_session(name)


def attach_existing_session(name: str) -> int:
    """Attach the current terminal to an existing tmux session."""
    return create_tmux_client().attach_session(name)


def switch_client_to_session(name: str) -> int:
    """Switch the current tmux client to ``name`` (when already in tmux)."""
    return create_tmux_client().switch_client(name)


def current_session_name() -> str | None:
    """Return the tmux session the caller is currently attached to, if any."""
    return create_tmux_client().current_session_name()
