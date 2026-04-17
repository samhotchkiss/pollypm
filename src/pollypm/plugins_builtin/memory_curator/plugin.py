"""Memory curator plugin (M06 / #235).

Registers the ``memory.curate`` job handler and a daily roster entry at
``@every 24h``. The handler delegates to
:func:`pollypm.memory_curator.curate_memory` for TTL sweep, dedup,
decay, and episodic→pattern promotion. Optionally drops a one-screen
summary into the operator's inbox directory so the user can see what
the curator did without tailing the audit log.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pollypm.plugin_api.v1 import Capability, JobHandlerAPI, PollyPMPlugin, RosterAPI


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


CURATOR_LOG_FILENAME = "memory-curator.jsonl"
CURATOR_INBOX_DIRNAME = "curator"


def _load_config_and_store(payload: dict[str, Any]):
    from pollypm.config import DEFAULT_CONFIG_PATH, load_config, resolve_config_path

    override = payload.get("config_path") if isinstance(payload, dict) else None
    config_path = Path(override) if override else resolve_config_path(DEFAULT_CONFIG_PATH)
    if not config_path.exists():
        raise RuntimeError(
            f"PollyPM config not found at {config_path}; cannot run memory curator"
        )
    config = load_config(config_path)

    from pollypm.storage.state import StateStore

    store = StateStore(config.project.state_db)
    return config, store


def memory_curate_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Run one memory-curator pass for every known project root.

    Returns a summary dict suitable for the heartbeat's recent-events
    log. The per-project audit log remains the source of truth for
    what was changed.
    """
    try:
        config, _store = _load_config_and_store(payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("memory.curate: cannot load config (%s); skipping", exc)
        return {"ok": False, "reason": str(exc)}

    # Import here to avoid pulling memory modules during plugin load
    # (memory_curator imports pollypm.memory_backends which is a hot
    # path at startup).
    from pollypm.knowledge_extract import _all_project_roots
    from pollypm.memory_backends import get_memory_backend
    from pollypm.memory_curator import build_inbox_summary, curate_memory

    totals = {
        "projects_scanned": 0,
        "ttl_deleted": 0,
        "duplicates_merged": 0,
        "decayed": 0,
        "promotion_candidates": 0,
    }
    for project_root in _all_project_roots(config):
        try:
            backend = get_memory_backend(project_root, "file")
        except Exception as exc:  # noqa: BLE001
            logger.warning("memory.curate[%s]: backend unavailable (%s)", project_root, exc)
            continue
        log_path = project_root / ".pollypm-state" / CURATOR_LOG_FILENAME
        try:
            result = curate_memory(backend, log_path=log_path, now=datetime.now(UTC))
        except Exception as exc:  # noqa: BLE001
            logger.warning("memory.curate[%s]: curator raised (%s)", project_root, exc)
            continue
        totals["projects_scanned"] += 1
        totals["ttl_deleted"] += result.ttl_deleted
        totals["duplicates_merged"] += result.duplicates_merged
        totals["decayed"] += result.decayed
        totals["promotion_candidates"] += result.promotion_candidates

        summary = build_inbox_summary(result)
        if summary:
            _emit_inbox_summary(project_root, summary)

    return {"ok": True, **totals}


def _emit_inbox_summary(project_root: Path, summary: str) -> None:
    """Write today's summary under ``.pollypm-state/curator/<date>.md``.

    The inbox surfaces this via directory scan — keeping the curator's
    output self-contained means it doesn't depend on a specific inbox
    implementation (the work-service inbox, morning briefing inbox, or
    a future unified inbox all see the same file).
    """
    inbox_dir = project_root / ".pollypm-state" / CURATOR_INBOX_DIRNAME
    inbox_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    path = inbox_dir / f"{stamp}.md"
    try:
        path.write_text(summary)
    except OSError as exc:  # noqa: BLE001
        logger.warning("memory.curate: could not write inbox summary to %s (%s)", path, exc)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def _register_handlers(api: JobHandlerAPI) -> None:
    api.register_handler(
        "memory.curate",
        memory_curate_handler,
        max_attempts=1,
        timeout_seconds=300.0,
    )


def _register_roster(api: RosterAPI) -> None:
    api.register_recurring(
        "@every 24h",
        "memory.curate",
        {},
        dedupe_key="memory.curate",
    )


plugin = PollyPMPlugin(
    name="memory_curator",
    version="0.1.0",
    description=(
        "Memory curator: daily TTL sweep, near-duplicate dedup, "
        "episodic→pattern promotion candidates, and importance decay."
    ),
    capabilities=(
        Capability(kind="job_handler", name="memory.curate"),
        Capability(kind="roster_entry", name="memory.curate"),
    ),
    register_handlers=_register_handlers,
    register_roster=_register_roster,
)
