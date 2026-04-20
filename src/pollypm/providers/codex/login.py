"""Codex login flow — Phase C of #397.

The interactive ``codex login`` flow is driven by
``pollypm.onboarding._run_login_window``, which threads a ``TmuxClient``
and a window label the Phase A Protocol does not yet model. Until the
Protocol carries that context (Phase D introduces the manager that
will), this module delegates to the existing onboarding helper.

Issue #406 adds the small shell-fragment helpers
(``login_command``, ``logout_command``,
``login_completion_marker_seen``) that the cross-provider login loop
needs to dispatch through the provider package instead of branching
on ``ProviderKind`` inside ``onboarding.py``.

Keeping the delegation in one place means Phase D can rewrite the
flow without a scavenger hunt: replace :func:`run_login_flow` here and
every caller that already routes through the substrate picks up the
new implementation.
"""

from __future__ import annotations

from pathlib import Path


def login_command(*, headless: bool = False) -> str:
    """Return the shell snippet that launches Codex's login flow.

    ``headless=True`` switches to ``codex login --device-auth`` for
    callers that opted into the device-flow preference (no browser on
    the host). Otherwise ``codex login`` opens the standard browser
    auth flow.
    """
    if headless:
        return "codex login --device-auth"
    return "codex login"


def logout_command() -> str:
    """Return the shell snippet that clears Codex credentials.

    Used when the caller passes ``force_fresh_auth=True`` so the next
    ``codex login`` starts from a clean ``auth.json``. Suffixed with
    ``|| true`` so a "not currently logged in" exit code does not
    abort the shell pipeline.
    """
    return "codex logout || true"


def login_completion_marker_seen(pane_text: str) -> bool:
    """Return True iff the Codex login pane shows the completion marker.

    The shared login loop writes ``PollyPM: login window complete.``
    at the tail of the login shell — Codex does not provide a more
    specific signal, so we use the same marker as Claude.
    """
    return "PollyPM: login window complete." in pane_text


def run_login_flow(home: Path, *, window_label: str | None = None) -> str:
    """Drive the interactive ``codex login`` flow for ``home``.

    Delegates to ``pollypm.onboarding._run_login_window`` with
    ``provider=ProviderKind.CODEX``. Returns the final pane-text capture
    so callers can parse the detected email from the ``Account:`` line
    without a second tmux round-trip.

    Raises :class:`pollypm.onboarding.LoginCancelled` if the user
    detaches from the login tmux session without completing auth.
    """
    # Imports are local so the module loads without pulling in tmux
    # machinery on systems that don't need it (e.g. probe-only callers).
    from pollypm.models import ProviderKind
    from pollypm.onboarding import _run_login_window
    from pollypm.session_services import create_tmux_client

    tmux = create_tmux_client()
    label = window_label or f"codex-login-{home.name}"
    return _run_login_window(
        tmux,
        provider=ProviderKind.CODEX,
        home=home,
        window_label=label,
    )


__all__ = [
    "login_command",
    "login_completion_marker_seen",
    "logout_command",
    "run_login_flow",
]
