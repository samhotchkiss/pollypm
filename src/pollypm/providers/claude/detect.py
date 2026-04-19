"""Claude account-state detection helpers.

Phase B of #397 moves the four Claude-specific detectors that used to
live in :mod:`pollypm.onboarding` into this module:

* :func:`detect_claude_email` — reads ``claude auth status`` and
  extracts the email (or a stable non-None sentinel for Max 2.x — see
  the ``#396`` fix preserved below).
* :func:`detect_logged_in` — a presence check driven by the same helper
  so callers don't have to care about Max-sentinel values.
* :func:`detect_email_from_pane` — pane scanner used during login flow.
  Claude never prints the email in its pane output; this always returns
  ``None`` and exists only so the shared login loop has a uniform
  signature to call.
* :func:`claude_prompt_ready` — heuristic the login wait-loop uses to
  know the CLI has reached its interactive prompt.

The legacy ``pollypm.onboarding._detect_claude_email`` /
``_claude_prompt_ready`` / ``_detect_email_from_pane`` symbols now
delegate here; the behavior is identical.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from .env import isolated_env_with_os_environ


_EMAIL_PATTERN = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
)


def detect_claude_email(home: Path) -> str | None:
    """Return the logged-in Claude email for ``home`` or ``None``.

    Contract (preserves the #396 fix):

    * ``loggedIn: false`` -> ``None``
    * ``loggedIn: true`` + non-empty ``email`` -> lowercased email
    * ``loggedIn: true`` + ``email: null`` (Claude CLI 2.x Max) ->
      stable non-None sentinel built from ``authMethod:subscriptionType``.
      Downstream callers use the return value only as a presence check;
      the real email is pinned in ``AccountConfig.email`` at registration.
    * JSON probe failed -> fall back to ``claude auth status --text`` and
      extract an email with a regex.

    Implementations must not mutate ``home`` — this is a read-only probe.
    """
    env = isolated_env_with_os_environ(home)
    json_result = subprocess.run(
        ["claude", "auth", "status", "--json"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    if json_result.returncode == 0:
        try:
            data = json.loads(json_result.stdout)
            if not data.get("loggedIn"):
                return None
            email = data.get("email")
            if isinstance(email, str) and email:
                return email.lower()
            # Claude CLI 2.x returns loggedIn:true with email:null for Max
            # subscribers. Fall back to a stable non-None sentinel so
            # _account_logged_in() correctly reads True. The real email is
            # already pinned in AccountConfig at registration; this return
            # value is used downstream only as a presence check.
            method = data.get("authMethod") or "claude"
            sub = data.get("subscriptionType") or "unknown"
            return f"{method}:{sub}".lower()
        except Exception:  # noqa: BLE001
            pass

    text_result = subprocess.run(
        ["claude", "auth", "status", "--text"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    if text_result.returncode == 0:
        match = _EMAIL_PATTERN.search(text_result.stdout)
        if match:
            return match.group(0).lower()
    return None


def detect_logged_in(home: Path) -> bool:
    """Return True iff ``home`` holds valid Claude credentials.

    Thin wrapper over :func:`detect_claude_email`: any non-None return
    from the email probe (including the Max sentinel) means the account
    is logged in.
    """
    return detect_claude_email(home) is not None


def detect_email_from_pane(pane_text: str) -> str | None:
    """Return ``None`` — Claude never prints the email in its pane.

    The multi-provider login loop in :mod:`pollypm.onboarding` calls
    ``_detect_email_from_pane(provider, pane_text)`` for every pane
    capture; this helper exists so Claude has a no-op detector that
    matches the loop's shape. Real email resolution happens via
    :func:`detect_claude_email` once login completes.
    """
    del pane_text  # intentionally unused; see docstring
    return None


def claude_prompt_ready(pane_text: str) -> bool:
    """Return True iff the Claude CLI has reached an interactive prompt.

    Used by the login wait-loop to decide when the pane is stable
    enough to accept input. Mirrors the legacy
    ``onboarding._claude_prompt_ready`` heuristic exactly.
    """
    lowered = pane_text.lower()
    if "select login method:" in lowered or "please run /login" in lowered:
        return False
    if "choose the text style that looks best with your terminal" in lowered:
        return False
    return "❯" in pane_text and (
        "welcome back" in lowered or "claude code v" in lowered
    )


__all__ = [
    "detect_claude_email",
    "detect_logged_in",
    "detect_email_from_pane",
    "claude_prompt_ready",
]
