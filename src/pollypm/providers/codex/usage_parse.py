"""Codex usage / weekly-quota parsing — Phase C of #397.

Codex does not expose a machine-readable usage endpoint; the supervisor
learns about weekly quota by scraping the ``/status`` pane that Codex
prints when it boots. This module owns the regex-level parsers that
turn that scraped text into a ``(health, summary)`` tuple.

Previously lived at ``pollypm.accounts._parse_codex_status_text``. The
function is re-exported from ``pollypm.accounts`` as a back-compat shim
so the existing call sites (state-store writers, TUI surfaces) keep
working during the Phase D migration.
"""

from __future__ import annotations

import re


def parse_codex_status_text(text: str) -> tuple[str, str]:
    """Extract ``(health, summary)`` from a Codex ``/status`` pane dump.

    Codex prints a one-line summary like::

        gpt-5.4 default · 100% left · /Users/sam/dev/pollypm

    This parser looks for the ``<n>% left`` fragment and surfaces it as
    the usage summary. When the pane mentions ``usage limit`` we
    classify the account as ``capacity-exhausted`` so the controller
    knows to failover. Otherwise the result is ``("unknown", "usage
    unavailable")`` — the caller decides what to do with it.

    The return shape is kept identical to the legacy
    ``_parse_codex_status_text`` so the back-compat shim is literal.
    """
    summary_match = re.search(r"(\d+)% left", text)
    if summary_match:
        return ("healthy", f"{summary_match.group(1)}% left")
    if "usage limit" in text.lower():
        return ("capacity-exhausted", "usage limit reached")
    return ("unknown", "usage unavailable")


__all__ = ["parse_codex_status_text"]
