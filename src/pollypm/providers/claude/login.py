"""Claude login-flow orchestration.

Phase B of #397 consolidated the Claude-specific bits of the login
flow into this module. Issue #406 extends it with the small shell
fragments (``login_command``, ``logout_command``,
``login_completion_marker_seen``) that the cross-provider login loop
in :mod:`pollypm.onboarding` needs to dispatch through the provider
package instead of branching on ``ProviderKind``.

The high-level interactive flow still lives in
:mod:`pollypm.onboarding` because it threads a ``TmuxClient`` and a
window label that the Phase A Protocol does not yet model — see
:func:`run_login_flow` below.
"""

from __future__ import annotations

from pollypm.acct.model import AccountConfig


def login_command(*, interactive: bool = False) -> str:
    """Return the shell snippet that launches Claude's login flow.

    ``interactive=True`` opens the Claude REPL itself (the user signs
    in via the in-app ``/login`` flow); otherwise we kick off the
    headless ``claude auth login --claudeai`` browser flow.
    """
    if interactive:
        return "claude"
    return "claude auth login --claudeai"


def logout_command() -> str:
    """Return the shell snippet that clears Claude credentials.

    Used when the caller passes ``force_fresh_auth=True`` to the login
    flow (e.g. ``pm accounts relogin``) so the next ``claude`` launch
    starts from a clean keychain entry. Suffixed with ``|| true`` so a
    "not currently logged in" exit code does not abort the shell
    pipeline.
    """
    return "claude auth logout || true"


def login_completion_marker_seen(pane_text: str) -> bool:
    """Return True iff the Claude login pane shows the completion marker.

    The shared login loop in :mod:`pollypm.onboarding` writes a
    ``PollyPM: login window complete.`` line at the end of the login
    shell so it can detect completion without parsing provider output.
    Claude does not have a stronger marker (the welcome banner can
    appear before auth completes), so this is the only signal we
    consume.
    """
    return "PollyPM: login window complete." in pane_text


def run_login_flow(account: AccountConfig) -> None:
    """Drive the interactive Claude login.

    Phase B ships a Protocol-shape stub: the real interactive flow
    needs a ``TmuxClient`` plus a window label, neither of which the
    Phase A Protocol models. Callers should continue to use
    :func:`pollypm.accounts.add_account_via_login` or
    :func:`pollypm.accounts.relogin_account`, which already thread
    those arguments through.

    Raises:
        NotImplementedError: always, with a 3-question-rule message
            pointing at the real entry points.
    """
    del account  # unused in Phase B — see docstring
    raise NotImplementedError(
        "Phase B of #397 exposes Claude login through the provider "
        "package but does not yet widen the ProviderAdapter Protocol "
        "to carry a TmuxClient.\n\n"
        "Why: the interactive login needs a tmux window + label so the "
        "user can finish the browser flow and PollyPM can detect "
        "completion. The Protocol signature intentionally takes only "
        "an AccountConfig — Phase D adds the context object.\n\n"
        "Fix: call `pollypm.accounts.add_account_via_login(config_path, "
        "ProviderKind.CLAUDE)` or "
        "`pollypm.accounts.relogin_account(config_path, account_name)`; "
        "both thread the tmux client and window label the flow needs."
    )


__all__ = [
    "login_command",
    "login_completion_marker_seen",
    "logout_command",
    "run_login_flow",
]
