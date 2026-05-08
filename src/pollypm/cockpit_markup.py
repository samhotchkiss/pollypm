"""Shared Rich-markup escaping helpers for cockpit UI surfaces."""

from __future__ import annotations


def _escape(s: str) -> str:
    """Escape Rich markup brackets in a short span of text."""
    if not s:
        return ""
    return str(s).replace("[", r"\[")


def _escape_body(s: str) -> str:
    """Escape Rich brackets for body text while preserving newlines.

    ``_md_to_rich`` re-adds its own markup; we only need to neutralise
    user-typed brackets so they render as literal characters.
    """
    if not s:
        return ""
    return str(s).replace("[", r"\[")
