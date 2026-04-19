"""Parse Claude ``/usage`` pane output into structured summaries.

Phase B of #397 consolidates the two copies of this parser that lived
in ``pollypm.accounts._parse_claude_usage_text`` and
``pollypm.providers.claude.ClaudeAdapter._parse_usage_text``. Both
call sites now go through :func:`parse_claude_usage_text` /
:func:`parse_claude_usage_snapshot`; the legacy helpers are kept as
thin shims for back-compat with existing imports.

The parsed output covers the "Current week (all models)" block Claude
prints on the ``/usage`` screen. The block looks like::

    Current week (all models)
    ██████████▌                                        21% used
    Resets Apr 10 at 1am (America/Denver)

We recognize the weekly bucket only — monthly / hard-cap buckets are
not emitted by the current CLI and will show up when the server starts
sending them. The parser is deliberately tolerant: on any match failure
it returns a ``("unknown", "usage unavailable")`` pair so probe callers
can surface the raw pane text to the user instead of crashing.
"""

from __future__ import annotations

import re

from pollypm.provider_sdk import ProviderUsageSnapshot


_WEEKLY_PATTERN = re.compile(
    r"Current week \(all models\).*?(\d+)% used.*?Resets ([^\n]+)",
    re.IGNORECASE | re.DOTALL,
)


def parse_claude_usage_text(text: str) -> tuple[str, str]:
    """Return ``(health, summary)`` for a Claude ``/usage`` pane dump.

    ``health`` is one of ``"healthy"`` / ``"unknown"`` (the tmux-probe
    path in :mod:`pollypm.providers.claude.probe` widens the range to
    ``"near-limit"`` / ``"exhausted"``; callers that only need the
    usage-list summary use the narrower return here).

    ``summary`` is a short human-readable description. When parsing
    fails the pair is ``("unknown", "usage unavailable")`` — the same
    shape the legacy ``_parse_claude_usage_text`` helper returned.
    """
    match = _WEEKLY_PATTERN.search(text)
    if not match:
        return ("unknown", "usage unavailable")
    used = int(match.group(1))
    remaining = max(0, 100 - used)
    reset = match.group(2).strip()
    return ("healthy", f"{remaining}% left this week · resets {reset}")


def parse_claude_usage_snapshot(text: str) -> ProviderUsageSnapshot:
    """Return a :class:`ProviderUsageSnapshot` for a ``/usage`` pane dump.

    This is the richer shape used by the tmux-probe path — it derives
    a tri-state health (``"healthy"`` / ``"near-limit"`` / ``"exhausted"``)
    from the percent-used value and carries the raw pane text so
    downstream callers can inspect it.
    """
    match = _WEEKLY_PATTERN.search(text)
    if not match:
        return ProviderUsageSnapshot(raw_text=text)
    used = int(match.group(1))
    reset = " ".join(match.group(2).split())
    left = max(0, 100 - used)
    if used >= 95:
        health = "exhausted"
    elif used >= 80:
        health = "near-limit"
    else:
        health = "healthy"
    return ProviderUsageSnapshot(
        health=health,
        summary=f"{left}% left this week · resets {reset}",
        raw_text=text,
    )


__all__ = ["parse_claude_usage_text", "parse_claude_usage_snapshot"]
