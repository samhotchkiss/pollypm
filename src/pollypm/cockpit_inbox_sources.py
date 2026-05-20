"""Leaf helpers for enumerating inbox source DBs + classifying rows.

Extracted from :mod:`pollypm.cockpit_inbox` to break a circular import
with :mod:`pollypm.cockpit_inbox_items` (refs #1367). Both modules pulled
``_inbox_db_sources`` / ``_row_is_dev_channel`` from each other; hoisting
them to a leaf module lets both sides import from here without cycling.

Contract:
- Inputs: a cockpit ``Config``-like object (for ``_inbox_db_sources``) or
  a raw labels column value (for ``_row_is_dev_channel``).
- Outputs: a list of ``(project_key, db_path, project_path)`` triples
  covering every inbox SQLite source, and a boolean for whether a row's
  ``labels`` mark it as a dev-channel message.
- Side effects: ``_inbox_db_sources`` calls ``Path.resolve()`` on each
  candidate DB path; nothing writes.
- Invariants: order is "every registered project, then the workspace
  root" with duplicates removed by resolved path. Non-tracked projects
  are intentionally included — see the docstring on ``_inbox_db_sources``
  for why the cockpit inbox + rail badge differ from the recovery /
  briefing surfaces (cycles 85/86/87, see ``project_tracked_filter_asymmetry``).
"""

from __future__ import annotations

import json as _json
from pathlib import Path
from pollypm.projects import project_state_db_path


def _inbox_db_sources(config) -> list[tuple[str | None, Path, Path]]:
    """Return ``(project_key, db_path, project_path)`` for every inbox source.

    Includes the per-project ``.pollypm/state.db`` for every registered
    project **and** the workspace-root ``<workspace_root>/.pollypm/state.db``
    — the latter is where ``pm notify`` (with defaults) lands items that
    don't belong to any one project (#271). Duplicates are dropped so a
    project whose path happens to equal the workspace root is scanned once.

    Note: this helper INCLUDES non-tracked projects on purpose — the
    inbox cockpit panel and rail badge surface all registered projects
    so a user can triage messages even before running ``pm init-tracker``.
    The recovery prompt + morning-briefing surfaces filter to tracked
    only; they have a different invariant (cycles 85/86/87).

    ``project_key`` is ``None`` for the workspace-root source; callers that
    need a project filter treat ``None`` as "no filter".
    """
    sources: list[tuple[str | None, Path, Path]] = []
    seen: set[Path] = set()
    for project_key, project in getattr(config, "projects", {}).items():
        project_path = Path(project.path)
        db_path = project_state_db_path(project_path)
        resolved = db_path.resolve() if db_path.exists() else db_path
        if resolved in seen:
            continue
        seen.add(resolved)
        sources.append((project_key, db_path, project_path))

    workspace_root = getattr(getattr(config, "project", None), "workspace_root", None)
    if workspace_root is not None:
        ws_path = Path(workspace_root)
        ws_db = project_state_db_path(ws_path)
        resolved = ws_db.resolve() if ws_db.exists() else ws_db
        if resolved not in seen:
            seen.add(resolved)
            sources.append((None, ws_db, ws_path))
    return sources


def _row_is_dev_channel(labels_raw: object) -> bool:
    """Return True if the message's labels list contains ``channel:dev``.

    Accepts the raw column value (JSON string or list) since
    ``Store.query_messages`` may surface either depending on the
    engine. Any other channel (or no explicit channel) is treated as
    user-facing. See #754.
    """
    labels: list[str] = []
    if isinstance(labels_raw, list):
        labels = [str(x) for x in labels_raw]
    elif isinstance(labels_raw, str) and labels_raw:
        try:
            parsed = _json.loads(labels_raw)
            if isinstance(parsed, list):
                labels = [str(x) for x in parsed]
        except ValueError:
            labels = []
    return "channel:dev" in labels
