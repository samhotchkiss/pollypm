"""Pure helpers for PollyPM's SQLite FTS5 query rewriting.

Contract:
- Inputs: free-text recall queries from callers that are *not* valid
  FTS5 syntax and may contain punctuation or operators.
- Outputs: a safe MATCH expression string ready for SQLite FTS5.
- Side effects: none.
- Invariants: malformed or empty input never raises; queries with no
  usable tokens collapse to a sentinel that matches nothing.
"""

from __future__ import annotations

import re

NO_MATCH_SENTINEL = '"__pollypm_no_match_sentinel__"'


def normalize_fts_query(query: str) -> str:
    """Convert free text into an FTS5-safe ``MATCH`` expression."""
    # Keep identifiers like ``state_store`` whole while stripping FTS
    # operators/punctuation down to alphanumeric token runs.
    tokens = re.findall(r"[\w]+", query.lower(), flags=re.UNICODE)
    tokens = [token for token in tokens if len(token) >= 2]
    if not tokens:
        return NO_MATCH_SENTINEL
    return " OR ".join(f'"{token}"' for token in tokens)
