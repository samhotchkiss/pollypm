"""Claude login-flow orchestration.

Phase B of #397 consolidates the Claude-specific bits of the login flow
into this module. The *full* login flow still lives in
:mod:`pollypm.onboarding` because it threads a tmux window label, a
preferences object, and the cross-provider completion detector — none
of which belong to Claude alone. What moves here:

* :func:`run_login_flow` — the Protocol-shaped entry point. Phase B
  keeps the implementation thin: it documents that the real interactive
  flow requires a tmux context not available on the Protocol and
  points callers at the existing top-level helpers. Phase D will widen
  the Protocol with a context object; until then the Phase A NotImplementedError
  phrasing is preserved here behind the Claude adapter.

Callers that want the detection + env helpers in isolation should
import them directly from :mod:`pollypm.providers.claude.detect` and
:mod:`pollypm.providers.claude.env`.
"""

from __future__ import annotations

from pollypm.models import AccountConfig


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


__all__ = ["run_login_flow"]
