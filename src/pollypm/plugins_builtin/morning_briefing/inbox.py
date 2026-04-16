"""Briefing inbox — persistence, pinning, auto-close.

Briefings are informational artifacts, not work-service tasks. They
live under ``<base_dir>/briefings/`` as one file per day:

* ``<date>.md``   — the body the user reads.
* ``<date>.json`` — sidecar metadata: kind, pinned flag, timestamps,
  structured data used by the CLI.

The briefing-log (``<base_dir>/briefing-log.jsonl``, owned by mb03) is
the append-only audit trail; this module owns the per-day "inbox"
surface that supports pin / auto-close / listing.

Public surface:

* :func:`emit_briefing` — write the draft as a briefing inbox entry.
* :func:`list_briefings` — read all entries, newest first. Supports
  filtering by status (``open`` / ``closed`` / ``all``).
* :func:`pin_briefing` / :func:`unpin_briefing` — user affordances.
* :func:`auto_close_expired` — called by the sweep job handler on
  ``@every 6h``. Closes un-pinned briefings older than 24 h.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from pollypm.atomic_io import atomic_write_text
from pollypm.plugins_builtin.morning_briefing.handlers.synthesize import BriefingDraft


logger = logging.getLogger(__name__)


BRIEFINGS_DIRNAME = "briefings"
BRIEFING_KIND = "morning_briefing"
DEFAULT_AUTO_CLOSE_HOURS = 24.0


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class BriefingEntry:
    """One day's briefing as it sits in the inbox.

    ``status`` values:
    * ``"open"`` — visible in the inbox.
    * ``"closed"`` — auto-closed or user-archived. Kept on disk for
      history but filtered out of the default listing.
    """

    date_local: str                 # YYYY-MM-DD
    kind: str = BRIEFING_KIND
    mode: str = ""                  # synthesized | fallback | quiet-mode
    status: str = "open"
    pinned: bool = False
    created_at: str = ""            # UTC ISO
    closed_at: str = ""             # UTC ISO when closed; "" otherwise
    body_path: str = ""             # path to the .md file, relative to base_dir
    yesterday: str = ""
    priorities: list[dict] = field(default_factory=list)
    watch: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "date_local": self.date_local,
            "kind": self.kind,
            "mode": self.mode,
            "status": self.status,
            "pinned": self.pinned,
            "created_at": self.created_at,
            "closed_at": self.closed_at,
            "body_path": self.body_path,
            "yesterday": self.yesterday,
            "priorities": list(self.priorities),
            "watch": list(self.watch),
            "meta": dict(self.meta),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BriefingEntry":
        return cls(
            date_local=str(data.get("date_local") or ""),
            kind=str(data.get("kind") or BRIEFING_KIND),
            mode=str(data.get("mode") or ""),
            status=str(data.get("status") or "open"),
            pinned=bool(data.get("pinned", False)),
            created_at=str(data.get("created_at") or ""),
            closed_at=str(data.get("closed_at") or ""),
            body_path=str(data.get("body_path") or ""),
            yesterday=str(data.get("yesterday") or ""),
            priorities=list(data.get("priorities") or []),
            watch=list(data.get("watch") or []),
            meta=dict(data.get("meta") or {}),
        )


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def briefings_dir(base_dir: Path) -> Path:
    return Path(base_dir) / BRIEFINGS_DIRNAME


def _md_path(base_dir: Path, date_local: str) -> Path:
    return briefings_dir(base_dir) / f"{date_local}.md"


def _meta_path(base_dir: Path, date_local: str) -> Path:
    return briefings_dir(base_dir) / f"{date_local}.json"


def _load_entry(base_dir: Path, date_local: str) -> BriefingEntry | None:
    path = _meta_path(base_dir, date_local)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return BriefingEntry.from_dict(data)


def _save_entry(base_dir: Path, entry: BriefingEntry) -> None:
    briefings_dir(base_dir).mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        _meta_path(base_dir, entry.date_local),
        json.dumps(entry.to_dict(), indent=2, sort_keys=True) + "\n",
    )


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Emit
# ---------------------------------------------------------------------------


def emit_briefing(
    base_dir: Path,
    draft: BriefingDraft,
    *,
    now_utc: datetime | None = None,
) -> BriefingEntry:
    """Write the draft as a briefing-inbox entry. Idempotent per date.

    If a briefing for ``draft.date_local`` already exists, the body +
    metadata are overwritten (preserving ``pinned`` and ``created_at``
    from the prior entry). The next-day sweep will still close it after
    24 h from the original creation unless the user pinned it.
    """
    base_dir = Path(base_dir)
    briefings_dir(base_dir).mkdir(parents=True, exist_ok=True)

    # Write the markdown body.
    md_path = _md_path(base_dir, draft.date_local)
    header = f"# Morning Briefing — {draft.date_local}\n\n"
    if draft.mode and draft.mode != "synthesized":
        header += f"_Mode: {draft.mode}_\n\n"
    atomic_write_text(md_path, header + draft.markdown + "\n")

    prior = _load_entry(base_dir, draft.date_local)
    created_at = (
        prior.created_at if prior and prior.created_at
        else (now_utc.astimezone(UTC).isoformat() if now_utc else _utc_now_iso())
    )
    pinned = prior.pinned if prior else False

    entry = BriefingEntry(
        date_local=draft.date_local,
        kind=BRIEFING_KIND,
        mode=draft.mode,
        status="open",
        pinned=pinned,
        created_at=created_at,
        closed_at="",
        body_path=str(md_path.relative_to(base_dir)),
        yesterday=draft.yesterday,
        priorities=[
            {"title": p.title, "project": p.project, "why": p.why}
            for p in draft.priorities
        ],
        watch=list(draft.watch),
        meta=dict(draft.meta),
    )
    _save_entry(base_dir, entry)
    return entry


# ---------------------------------------------------------------------------
# List / read
# ---------------------------------------------------------------------------


def list_briefings(
    base_dir: Path,
    *,
    status: str = "open",
    limit: int | None = None,
) -> list[BriefingEntry]:
    """Return briefings, newest first. ``status`` is ``open``/``closed``/``all``."""
    root = briefings_dir(base_dir)
    if not root.exists():
        return []
    entries: list[BriefingEntry] = []
    for meta_file in sorted(root.glob("*.json"), reverse=True):
        try:
            data = json.loads(meta_file.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        entry = BriefingEntry.from_dict(data)
        if status != "all" and entry.status != status:
            continue
        entries.append(entry)
        if limit is not None and len(entries) >= limit:
            break
    return entries


def read_briefing(base_dir: Path, date_local: str) -> tuple[BriefingEntry, str] | None:
    """Return (entry, markdown) or None if missing."""
    entry = _load_entry(base_dir, date_local)
    if entry is None:
        return None
    md_path = _md_path(base_dir, date_local)
    body = md_path.read_text() if md_path.exists() else ""
    return entry, body


# ---------------------------------------------------------------------------
# Pin / unpin
# ---------------------------------------------------------------------------


def pin_briefing(base_dir: Path, date_local: str) -> BriefingEntry:
    """Pin a briefing so the sweep leaves it alone."""
    entry = _load_entry(base_dir, date_local)
    if entry is None:
        raise FileNotFoundError(f"No briefing for {date_local}")
    entry.pinned = True
    # Pinning reopens a closed briefing — the user wants it visible again.
    if entry.status == "closed":
        entry.status = "open"
        entry.closed_at = ""
    _save_entry(base_dir, entry)
    return entry


def unpin_briefing(base_dir: Path, date_local: str) -> BriefingEntry:
    entry = _load_entry(base_dir, date_local)
    if entry is None:
        raise FileNotFoundError(f"No briefing for {date_local}")
    entry.pinned = False
    _save_entry(base_dir, entry)
    return entry


# ---------------------------------------------------------------------------
# Auto-close sweep
# ---------------------------------------------------------------------------


def auto_close_expired(
    base_dir: Path,
    *,
    now_utc: datetime | None = None,
    age_hours: float = DEFAULT_AUTO_CLOSE_HOURS,
) -> list[BriefingEntry]:
    """Close un-pinned open briefings older than ``age_hours``.

    Returns the list of entries that were just closed.
    """
    now = now_utc or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    closed: list[BriefingEntry] = []
    for entry in list_briefings(base_dir, status="open"):
        if entry.pinned:
            continue
        created = _parse_iso_utc(entry.created_at)
        if created is None:
            continue
        age = (now - created).total_seconds() / 3600.0
        if age < age_hours:
            continue
        entry.status = "closed"
        entry.closed_at = now.astimezone(UTC).isoformat()
        _save_entry(base_dir, entry)
        closed.append(entry)
    return closed


def _parse_iso_utc(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


# ---------------------------------------------------------------------------
# Sweep handler (registered as briefing.sweep on @every 6h)
# ---------------------------------------------------------------------------


def briefing_sweep_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Job handler for ``briefing.sweep`` — call ``auto_close_expired``."""
    from pollypm.config import DEFAULT_CONFIG_PATH, load_config, resolve_config_path

    config_path_override = payload.get("config_path") if isinstance(payload, dict) else None
    config_path = (
        Path(config_path_override) if config_path_override else resolve_config_path(DEFAULT_CONFIG_PATH)
    )
    if not config_path.exists():
        return {"closed": 0, "reason": "no-config"}
    try:
        config = load_config(config_path)
    except Exception as exc:  # noqa: BLE001
        logger.debug("briefing sweep: config load failed: %s", exc)
        return {"closed": 0, "reason": "config-error", "error": str(exc)}

    base_dir = config.project.base_dir
    now_iso = payload.get("now_utc") if isinstance(payload, dict) else None
    now_utc: datetime | None = None
    if isinstance(now_iso, str) and now_iso:
        try:
            now_utc = datetime.fromisoformat(now_iso)
        except ValueError:
            now_utc = None

    closed = auto_close_expired(base_dir, now_utc=now_utc)
    return {
        "closed": len(closed),
        "dates_closed": [e.date_local for e in closed],
    }
