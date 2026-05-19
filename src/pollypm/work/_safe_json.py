"""Defensive JSON-column decode helpers for the work database.

Contract:
- Inputs: raw values read from SQLite JSON columns (typically ``str``
  payloads serialised by producers, but defensively any object).
- Outputs: a guaranteed-dict (`_safe_json_dict`) or guaranteed-list
  (`_safe_json_list`) result, with corrupt / wrong-shape payloads
  coerced to the appropriate empty container.
- Side effects: none — pure decoders.
- Invariants: never raises on malformed input. Producers always write
  the expected shape, but a hand-edited or legacy DB row could land an
  empty string, ``null``, scalar, or wrong container; downstream
  callers do ``parsed.get(...)`` / ``for x in parsed`` and would crash
  on those shapes. These helpers degrade a single corrupt row
  gracefully rather than propagating the crash out of the caller's
  loop.

Leaf module: this file deliberately has no internal imports beyond
``json`` so it can be imported from any work-service submodule
(including ``sqlite_service`` and the boundary-owned ``service_*``
helpers) without forming an import cycle (#1367 wedge).
"""

from __future__ import annotations

import json


def _safe_json_dict(raw: object) -> dict:
    """Decode a JSON column expected to be a dict, defensively.

    Producers always serialise dicts (``json.dumps(template.roles)`` etc.),
    but a hand-edited or legacy DB row could land an empty string,
    null, list, or scalar. Downstream callers do ``parsed.get(...)``
    and would AttributeError on those shapes — propagating the crash
    out of the caller's loop. Coerce non-dict shapes to ``{}`` so a
    single corrupt row degrades gracefully.

    Mirrors the ``_safe_payload`` / ``_safe_tags`` helpers in
    ``pollypm.storage.state`` (cycles 107-109 corrupt-payload defenses).
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _safe_json_list(raw: object) -> list:
    """Decode a JSON column expected to be a list, defensively.

    Companion to ``_safe_json_dict`` for ``labels`` / ``relevant_files``
    / ``gates`` columns. Coerces non-list shapes (dict/string/null/int)
    back to ``[]`` so consumers iterating the result don't iterate the
    wrong thing (e.g. a string would yield characters).
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []
