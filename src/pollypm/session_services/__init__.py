"""Session service factory."""

from __future__ import annotations

from pathlib import Path

from pollypm.session_services.base import SessionHandle, SessionHealth, SessionService, TranscriptStream

__all__ = [
    "SessionHandle",
    "SessionHealth",
    "SessionService",
    "TranscriptStream",
    "create_tmux_client",
    "get_session_service",
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
