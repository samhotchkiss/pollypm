"""Read-side aggregates over ``work_sessions`` rows.

Presentation callers (e.g. the per-project dashboard's Tokens line)
should use these facades instead of opening the workspace SQLite file
directly. Schema and connection details stay behind
``pollypm.storage.work_session_queries``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pollypm.storage.work_session_queries import (
    aggregate_project_session_tokens as _aggregate_project_session_tokens,
)

if TYPE_CHECKING:
    from pollypm.models import PollyPMConfig


def project_session_token_totals(
    db_path: Path,
    *,
    project_key: str,
    config: "PollyPMConfig | None" = None,
) -> tuple[int, int] | None:
    """Return ``(input_tokens, output_tokens)`` summed across worker sessions.

    Returns ``None`` when the workspace DB is missing or the
    ``work_sessions`` table is absent so the caller can render an
    ``(n/a)`` fallback rather than break.

    ``config`` is forwarded to the storage facade so callers with a
    non-default ``--config`` (e.g. custom ``[storage.pg].dsn``) reach
    the right pool. Omitting ``config`` falls back to the global
    process-singleton pool (#1755).
    """
    return _aggregate_project_session_tokens(
        Path(db_path), project_key=project_key, config=config,
    )


__all__ = ["project_session_token_totals"]
