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


def format_cli_error(
    summary: str,
    *,
    why: str | None = None,
    fix: str | None = None,
    details: list[str] | None = None,
) -> str:
    """Build a compact CLI error block that follows the three-question rule."""
    lines = [f"✗ {summary.strip()}"]
    if why:
        lines.append(f"  Why: {why.strip()}")
    for detail in details or []:
        if detail and detail.strip():
            lines.append(f"  {detail.strip()}")
    if fix:
        lines.append(f"  Fix: {fix.strip()}")
    return "\n".join(lines)


def render_cli_error(message: str) -> str:
    """Normalize an existing error string to the standard CLI layout."""
    lines = [line.rstrip() for line in str(message or "").strip().splitlines()]
    if not lines:
        return "✗ Command failed."

    head = lines[0].strip()
    if head.startswith("Error: "):
        head = head[len("Error: "):].strip()
    if head.startswith("✗ "):
        rendered = [head]
    else:
        rendered = [f"✗ {head}"]

    for line in lines[1:]:
        if not line.strip():
            continue
        rendered.append(f"  {line.strip()}")
    return "\n".join(rendered)


def format_task_not_found_error(
    task_id: str,
    *,
    why: str,
    fix: str,
    suggestion: str | None = None,
) -> str:
    """Return the canonical CLI error for a missing work-service task."""
    details = [f"Did you mean {suggestion}?"] if suggestion else None
    return format_cli_error(
        f"Task {task_id} not found.",
        why=why,
        fix=fix,
        details=details,
    )


def format_invalid_task_id_error(task_id: str, *, why: str) -> str:
    """Return the canonical CLI error for a malformed work-service task id."""
    return format_cli_error(
        f"Task id {task_id!r} is invalid.",
        why=why,
        fix="pass a task id like `demo/1`.",
    )


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
        "Fix: open Polly setup for first-time configuration, create a "
        "blank config from Settings, or choose a different config path if "
        "your config lives elsewhere."
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
      Settings > Accounts reconnect hint is used.
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
            "Fix: open Settings > Accounts to check login state, then "
            f"reconnect '{account_name}' if the session expired."
        )
    return "\n".join(parts)


class StoreBackendNotFound(LookupError):
    """No ``pollypm.store_backend`` entry point matches the configured name.

    Raised by :func:`pollypm.store.registry.get_store` when
    ``config.storage.backend`` names a backend that is not currently
    installed. The message follows the three-question rule (#240): what
    happened, why it matters, how to fix it — including the list of
    backends that *are* available so the user can eyeball a typo.
    """

    def __init__(self, backend: str, *, available: list[str]) -> None:
        self.backend = backend
        self.available = list(available)
        available_list = ", ".join(available) if available else "none"
        message = (
            f"Storage backend '{backend}' is not installed.\n\n"
            f"PollyPM loads its persistent-state backend via the "
            f"'pollypm.store_backend' entry-point group, and no installed "
            f"package registered a backend with that name — so the CLI "
            f"cannot open the state DB and every subsystem that persists "
            f"state will fail.\n\n"
            f"Available backends: {available_list}.\n\n"
            f"Fix: set ``[storage] backend`` in ~/.pollypm/pollypm.toml to "
            f"one of the available names, or install the package that "
            f"ships '{backend}' (e.g. `uv tool install "
            f"pollypm-store-postgres` for the Postgres backend)."
        )
        super().__init__(message)


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
