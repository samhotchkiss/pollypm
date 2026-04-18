"""Centralized error-message helpers for user-facing CLI output.

This module exists to normalize the shape of recurring error messages so
the CLI has one canonical phrasing per situation. Today the only entries
are the "config not found" helper used by seven CLIs, plus a helper for
formatting probe-failure messages that name the account and include a
"Fix:" footer. More can be added as follow-ups — the audit at
``reports/error-message-audit.md`` tracks candidates.

Style rules (keep this list short):

* Always include the subject (path / account / task id) in the message.
* Always end with ``Fix: …`` when the user must take an action.
* Keep messages to 1-2 short sentences plus the Fix line.
* Don't expose Python type names (``ValueError``, ``RuntimeError``, …).
"""

from __future__ import annotations

from pathlib import Path


def format_config_not_found_error(path: Path) -> str:
    """Return the canonical "config not found" message for ``path``.

    Seven call sites historically produced seven different phrasings
    for the same condition (see ``reports/error-message-audit.md``
    section 2c item 1). This helper is the single source of truth.

    The message names the absolute path and ends with a Fix: line that
    lists the three ways to resolve the situation.
    """
    return (
        f"No PollyPM config at {path}.\n\n"
        f"Fix: run `pm onboard` for first-time setup, `pm init` to write a "
        f"blank config, or pass `--config <path>` if your config lives "
        f"elsewhere."
    )


def format_probe_failure(
    *,
    provider: str,
    account_name: str,
    account_email: str | None,
    reason: str,
    pane_tail: str | None = None,
    fix: str | None = None,
) -> str:
    """Shape a provider-probe failure message with account context + a Fix line.

    Used by ``Supervisor._probe_controller_account`` and the Codex launch
    stabilizer. The three-block shape (what / why / fix) is what the
    audit (§3.1) recommends for user-blocking failures.

    * ``provider`` is a capitalized display string (``"Claude"`` /
      ``"Codex"``) — the caller decides the case so this helper does
      not have to branch on the ``ProviderKind`` enum.
    * ``reason`` is a one-line sentence describing what the probe saw
      (e.g. "is out of credits").
    * ``pane_tail`` is the last few lines of the provider output when
      available; it's inserted verbatim between the summary and the
      Fix: line.
    * ``fix`` is the next-action sentence. If omitted, a generic
      ``pm relogin <account>`` hint is used.
    """
    email_suffix = f" ({account_email})" if account_email else ""
    summary = (
        f"{provider} probe failed for account '{account_name}'{email_suffix}: "
        f"{reason}."
    )
    parts: list[str] = [summary]
    if pane_tail:
        trimmed = pane_tail.strip()
        if trimmed:
            parts.append("")
            parts.append("Last probe output:")
            parts.append(trimmed)
    parts.append("")
    if fix:
        parts.append(f"Fix: {fix}")
    else:
        parts.append(
            f"Fix: run `pm accounts` to check login state, then "
            f"`pm relogin {account_name}` if the session expired."
        )
    return "\n".join(parts)


def _last_lines(text: str, n: int = 5) -> str:
    """Return the last ``n`` non-empty lines of ``text``.

    Helper for ``format_probe_failure`` callers who have a raw pane
    capture and want to tail it. Keeps the "five lines" rule (audit §1
    item 1) in one place.
    """
    if not text:
        return ""
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines[-n:])
