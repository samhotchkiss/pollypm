"""Tier-4 authority section loader (#1553).

Reads :file:`tier4_authority.md` from this package and renders it
inline so the dispatcher can splice it into the body of the inbox
task that hands tier-4 work to Polly. Caches the file contents in
memory because the file rarely changes inside a single process and
the dispatcher path is hot during cascading failures.

The contents of :file:`tier4_authority.md` is the *authority list*
only — integrity rules, in-bounds project-scoped actions, in-bounds
system-scoped actions (with the louder-audit + push-notification
expectation), off-limits actions, and the audit-event names Polly
must emit. The cognitive prompt content (how to reason at tier-4) is
deferred to a follow-up issue per the spec; do not add it here.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path


logger = logging.getLogger(__name__)


_TIER4_AUTHORITY_FILENAME = "tier4_authority.md"


def _authority_path() -> Path:
    """Return the absolute path to :file:`tier4_authority.md`."""
    return Path(__file__).parent / _TIER4_AUTHORITY_FILENAME


@lru_cache(maxsize=1)
def _read_authority_file() -> str:
    """Read the markdown authority file once per process.

    Returns the empty string on read failure so a misbehaving
    deployment (file missing, perms wrong) can't break the dispatch
    path entirely — the dispatcher logs and ships a degraded prompt.
    """
    path = _authority_path()
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "tier4_authority: failed to read %s: %s", path, exc,
        )
        return ""


def render_tier4_authority_section(
    *,
    root_cause_hash_value: str,
    promotion_path: str,
    budget_seconds: int,
    auto_promote_threshold: int,
    auto_promote_window_seconds: int,
) -> str:
    """Return the tier-4 authority section as a string.

    The renderer prepends a small header block with the runtime
    constants (budget, threshold, promotion-path) so Polly sees the
    *current* tunable values inline rather than relying on the static
    markdown copy to stay in sync. The body of the markdown file is
    appended verbatim.

    Args:
        root_cause_hash_value: the 16-char hash that gates this
            promotion. Surfaced in the prompt so Polly can quote it
            back when emitting tier-4 action events.
        promotion_path: ``"watchdog"`` (auto-promoted) or ``"self"``
            (self-promoted via ``pm tier4 self-promote``).
        budget_seconds: the wall-clock budget in seconds (typically
            7200 = 2h).
        auto_promote_threshold: K — the dispatch-count threshold.
        auto_promote_window_seconds: W — the sliding-window seconds.
    """
    minutes = max(1, int(budget_seconds) // 60)
    hours = round(int(budget_seconds) / 3600.0, 1)
    window_hours = round(int(auto_promote_window_seconds) / 3600.0, 1)
    header = (
        "<tier4_runtime>\n"
        f"root_cause_hash: {root_cause_hash_value}\n"
        f"promotion_path: {promotion_path}\n"
        f"budget: {budget_seconds} seconds ({minutes} min / {hours}h)\n"
        f"auto_promote_threshold: {auto_promote_threshold} dispatches "
        f"in {window_hours}h\n"
        "</tier4_runtime>\n\n"
    )
    return header + _read_authority_file()


def reload_tier4_authority_cache() -> None:
    """Drop the file-contents cache. Used by tests and `pm` reload paths."""
    _read_authority_file.cache_clear()


__all__ = [
    "render_tier4_authority_section",
    "reload_tier4_authority_cache",
]
