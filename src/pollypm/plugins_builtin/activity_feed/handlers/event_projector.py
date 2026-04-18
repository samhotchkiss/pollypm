"""Event projector — unifies disparate event sources into FeedEntry stream.

The activity feed is a *projection* of the underlying event tables, not a
new write path. This module owns the read-side SQL + Python filtering
that turns raw rows into :class:`FeedEntry` records rendered by the
cockpit panel (lf03) and the ``pm activity`` CLI (lf05).

Sources consumed
----------------

1. **Global state store** — ``config.project.state_db``
   - ``events`` table: every ``StateStore.record_event`` call (sessions,
     alerts, heartbeats, transcripts, recoveries, workers).
   - We install an ``activity_events`` SQL view over this table that
     exposes the unified ``id, timestamp, project, kind, actor, subject,
     verb, summary, severity, payload_json`` shape. The view is created
     idempotently by :func:`ensure_activity_events_view`.

2. **Per-project work DBs** — ``<project.path>/.pollypm/state.db``
   - ``work_transitions`` table — task state changes.
   - ``work_context_entries`` table — append-only per-task notes.
   These projects-scoped rows are read directly (no cross-DB join) and
   merged with the global stream at projection time.

Filtered-out noise
------------------

Per spec §2, a small allow-list of event types is considered
"not human-relevant" and never shown in the feed:

* ``tick`` with no ``decision`` field (heartbeat tick with nothing done),
* ``health_poll`` / ``poll`` with no ``changed`` field,
* ``heartbeat`` rows whose ``message`` matches the no-op snapshot text.

Everything else is projected; lf02 will fill in the structured summary
field on emission sites so rendering doesn't have to parse free-text
messages.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# FeedEntry — the unified projection shape consumed by cockpit + CLI.
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class FeedEntry:
    """A single activity entry surfaced in the live feed.

    ``id`` is a source-qualified string (``"evt:123"``, ``"wt:demo/5:4"``)
    so entries from different tables never collide. ``timestamp`` is an
    ISO-8601 UTC string. ``severity`` drives visual weight in the panel
    (``critical`` / ``recommendation`` / ``routine``). ``payload`` is a
    dict decoded from the source row's JSON column (when present) so
    downstream detail views can render it without re-querying.
    """

    id: str
    timestamp: str
    project: str | None
    kind: str
    actor: str
    subject: str | None
    verb: str
    summary: str
    severity: str
    payload: dict[str, Any] = field(default_factory=dict)
    source: str = "events"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "project": self.project,
            "kind": self.kind,
            "actor": self.actor,
            "subject": self.subject,
            "verb": self.verb,
            "summary": self.summary,
            "severity": self.severity,
            "payload": self.payload,
            "source": self.source,
        }


# ---------------------------------------------------------------------------
# Activity events view — a read-only projection of `events` into the
# unified shape. Created once per DB in :func:`ensure_activity_events_view`
# and consumed by the projector.
# ---------------------------------------------------------------------------

ACTIVITY_EVENTS_VIEW_SQL = """
CREATE VIEW IF NOT EXISTS activity_events AS
SELECT
    id                                                AS id,
    created_at                                        AS timestamp,
    NULL                                              AS project,
    event_type                                        AS kind,
    session_name                                      AS actor,
    session_name                                      AS subject,
    event_type                                        AS verb,
    message                                           AS summary,
    CASE
        WHEN event_type = 'alert'    THEN 'recommendation'
        WHEN event_type = 'recovery' THEN 'recommendation'
        WHEN event_type = 'error'    THEN 'critical'
        WHEN event_type = 'stuck'    THEN 'critical'
        ELSE 'routine'
    END                                               AS severity,
    json_object(
        'session', session_name,
        'event_type', event_type,
        'message', message
    )                                                 AS payload_json
FROM events;
"""


def ensure_activity_events_view(conn: sqlite3.Connection) -> None:
    """Create the ``activity_events`` view on ``conn`` if missing.

    Idempotent — safe to call on every projector invocation. Uses
    ``CREATE VIEW IF NOT EXISTS`` so concurrent installers can race
    without conflicting.
    """
    try:
        conn.executescript(ACTIVITY_EVENTS_VIEW_SQL)
        conn.commit()
    except sqlite3.DatabaseError:
        logger.exception(
            "activity_feed: failed to install activity_events view on %s",
            getattr(conn, "database", "<unknown>"),
        )


# ---------------------------------------------------------------------------
# Noise filter — drop rows that aren't human-relevant.
# ---------------------------------------------------------------------------

_NOISY_KINDS: frozenset[str] = frozenset(
    {
        # Ticks with no decision are pure bookkeeping.
        "tick_noop",
        "poll_unchanged",
        "health_poll",
    }
)

_NOOP_MESSAGES: frozenset[str] = frozenset(
    {
        "Recorded heartbeat snapshot",
    }
)


def _is_noise(kind: str, summary: str, payload: dict[str, Any]) -> bool:
    """Return True when this row should be filtered out of the feed.

    Per spec §2: ticks with no decision and health polls with no change
    live only in raw logs.
    """
    if kind in _NOISY_KINDS:
        return True
    if kind == "tick" and not payload.get("decision"):
        return True
    if kind == "poll" and not payload.get("changed"):
        return True
    if kind == "heartbeat" and summary in _NOOP_MESSAGES:
        return True
    return False


# ---------------------------------------------------------------------------
# Payload parsing — record_event stores `message` as a free-form string
# today (lf02 promotes it to a structured JSON blob with summary +
# severity keys). We accept both: if the message parses as JSON with the
# expected keys, use those; otherwise fall back to kind + actor rendering.
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class _Structured:
    summary: str | None = None
    severity: str | None = None
    verb: str | None = None
    subject: str | None = None
    project: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _parse_message(message: str) -> _Structured:
    """Parse a ``record_event`` message into structured fields if possible.

    lf02 will migrate emission sites to pass JSON with at least
    ``summary`` + ``severity``. Until then, plain-string messages are
    accepted — ``_Structured.summary`` stays None and the caller falls
    back to ``<kind>: <message>`` rendering.
    """
    if not message:
        return _Structured()
    stripped = message.strip()
    if not stripped.startswith("{"):
        return _Structured()
    try:
        raw = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return _Structured()
    if not isinstance(raw, dict):
        return _Structured()
    return _Structured(
        summary=(raw.get("summary") if isinstance(raw.get("summary"), str) else None),
        severity=(raw.get("severity") if isinstance(raw.get("severity"), str) else None),
        verb=(raw.get("verb") if isinstance(raw.get("verb"), str) else None),
        subject=(raw.get("subject") if isinstance(raw.get("subject"), str) else None),
        project=(raw.get("project") if isinstance(raw.get("project"), str) else None),
        extra={k: v for k, v in raw.items() if k not in {"summary", "severity", "verb", "subject", "project"}},
    )


# ---------------------------------------------------------------------------
# Event projector — the public read API.
# ---------------------------------------------------------------------------


class EventProjector:
    """Reads raw event rows, returns :class:`FeedEntry` lists.

    Callers construct one projector bound to a state-store path (the
    global ``state.db``). Per-project work DBs are passed in at query
    time so the projector doesn't need to know config layout.

    Usage::

        proj = EventProjector(state_db_path)
        entries = proj.project(limit=50)
        for entry in entries: ...
    """

    __slots__ = ("_state_db", "_work_dbs")

    def __init__(
        self,
        state_db_path: Path,
        work_db_paths: Iterable[tuple[str, Path]] = (),
    ) -> None:
        self._state_db = Path(state_db_path)
        # list of (project_key, work_db_path) for per-project work stores.
        self._work_dbs: list[tuple[str, Path]] = [
            (key, Path(path)) for key, path in work_db_paths
        ]

    # ------------------------------------------------------------------
    # Per-source projectors.
    # ------------------------------------------------------------------

    def _project_from_state_store(
        self,
        *,
        since_id: int | None,
        since_ts: str | None,
        limit: int,
    ) -> list[FeedEntry]:
        """Project ``messages`` rows into :class:`FeedEntry` values.

        Supervisor + heartbeat writers all land on the unified
        ``messages`` table (#349 + #342). We issue a single
        :meth:`Store.query_messages` call with ``type in ('event',
        'notify', 'alert')`` and reshape each row into a
        :class:`FeedEntry`.
        """
        if not self._state_db.exists():
            return []
        # The legacy ``activity_events`` view is still installed so that
        # callers who bypass this method (e.g. ad-hoc SQL in ops
        # runbooks) keep working. We don't query it here — the bridge
        # reads ``events`` directly.
        _install_view(self._state_db)

        try:
            from pollypm.store import SQLAlchemyStore
        except Exception:  # noqa: BLE001
            return []

        try:
            store = SQLAlchemyStore(f"sqlite:///{self._state_db}")
        except Exception:  # noqa: BLE001
            logger.exception("activity_feed: failed to open Store")
            return []

        try:
            filters: dict[str, Any] = {
                "type": ["event", "notify", "alert"],
                "limit": int(limit),
            }
            if since_ts is not None:
                try:
                    filters["since"] = datetime.fromisoformat(since_ts)
                except (TypeError, ValueError):
                    pass
            try:
                rows = store.query_messages(**filters)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "activity_feed: query_messages failed"
                )
                rows = []
        finally:
            try:
                store.close()
            except Exception:  # noqa: BLE001
                pass

        entries: list[FeedEntry] = []
        for row in rows:
            if since_id is not None:
                try:
                    row_id = int(row.get("id") or 0)
                except (TypeError, ValueError):
                    row_id = 0
                if row_id and row_id <= since_id:
                    continue
            payload = row.get("payload") or {}
            message = row.get("body") or row.get("subject") or ""
            kind = _kind_from_message_row(row)
            actor = row.get("scope") or row.get("sender") or "system"
            parsed = _parse_message(message)
            summary = parsed.summary or _fallback_summary(
                kind, message, actor,
            )
            severity = parsed.severity or _severity_from_message_row(row)
            project = parsed.project or (payload.get("project") if isinstance(payload, dict) else None)
            verb = parsed.verb or kind
            subject = parsed.subject or (row.get("sender") or None)
            if parsed.extra:
                payload = {**payload, **parsed.extra}
            if _is_noise(kind, message, payload):
                continue
            entries.append(
                FeedEntry(
                    id=_entry_id_from_row(row),
                    timestamp=str(row.get("created_at") or ""),
                    project=project,
                    kind=kind,
                    actor=actor,
                    subject=subject,
                    verb=verb,
                    summary=summary,
                    severity=severity,
                    payload=payload if isinstance(payload, dict) else {},
                    source="events",
                )
            )
        return entries

    def _project_from_work_db(
        self,
        project_key: str,
        work_db: Path,
        *,
        since_ts: str | None,
        limit: int,
    ) -> list[FeedEntry]:
        if not work_db.exists():
            return []
        conn = sqlite3.connect(
            f"file:{work_db}?mode=ro", uri=True, check_same_thread=False,
        )
        try:
            conn.row_factory = sqlite3.Row
            params: list[Any] = []
            where = ""
            if since_ts is not None:
                where = "WHERE created_at >= ?"
                params.append(since_ts)
            rows = conn.execute(
                f"SELECT id, task_project, task_number, from_state, to_state, "
                f"actor, reason, created_at FROM work_transitions {where} "
                f"ORDER BY id DESC LIMIT ?",
                (*params, int(limit)),
            ).fetchall()
        except sqlite3.DatabaseError:
            logger.exception(
                "activity_feed: work-db projection failed for %s (%s)",
                project_key, work_db,
            )
            rows = []
        finally:
            conn.close()

        entries: list[FeedEntry] = []
        for row in rows:
            task_id = f"{row['task_project']}/{row['task_number']}"
            summary = f"task {task_id}: {row['from_state']} \u2192 {row['to_state']}"
            if row["reason"]:
                summary += f" ({row['reason']})"
            severity = "recommendation" if row["to_state"] in {"blocked", "cancelled"} else "routine"
            entries.append(
                FeedEntry(
                    id=f"wt:{row['task_project']}/{row['task_number']}:{row['id']}",
                    timestamp=row["created_at"],
                    project=row["task_project"] or project_key,
                    kind="task_transition",
                    actor=row["actor"] or "worker",
                    subject=task_id,
                    verb=f"{row['from_state']}->{row['to_state']}",
                    summary=summary,
                    severity=severity,
                    payload={
                        "from_state": row["from_state"],
                        "to_state": row["to_state"],
                        "reason": row["reason"],
                        "task_project": row["task_project"],
                        "task_number": row["task_number"],
                    },
                    source="work_transitions",
                )
            )
        return entries

    # ------------------------------------------------------------------
    # Public API.
    # ------------------------------------------------------------------

    def project(
        self,
        *,
        since_id: int | None = None,
        since_ts: str | None = None,
        since: timedelta | None = None,
        limit: int = 50,
        projects: Iterable[str] | None = None,
        kinds: Iterable[str] | None = None,
        actors: Iterable[str] | None = None,
    ) -> list[FeedEntry]:
        """Project the live event stream into ``FeedEntry`` instances.

        ``since`` (a ``timedelta``) is a convenience for the CLI's
        ``--since 1h`` flag — converted to ``since_ts`` internally.
        """
        if since is not None and since_ts is None:
            since_ts = (datetime.now(UTC) - since).isoformat()

        entries: list[FeedEntry] = []
        entries.extend(
            self._project_from_state_store(
                since_id=since_id, since_ts=since_ts, limit=limit,
            )
        )
        for project_key, work_db in self._work_dbs:
            entries.extend(
                self._project_from_work_db(
                    project_key, work_db, since_ts=since_ts, limit=limit,
                )
            )

        # Filter post-projection so callers can constrain without having
        # to wire each filter into every source.
        project_filter = set(projects) if projects else None
        kind_filter = set(kinds) if kinds else None
        actor_filter = set(actors) if actors else None

        def _keep(entry: FeedEntry) -> bool:
            if project_filter is not None and entry.project not in project_filter:
                return False
            if kind_filter is not None and entry.kind not in kind_filter:
                return False
            if actor_filter is not None and entry.actor not in actor_filter:
                return False
            return True

        filtered = [entry for entry in entries if _keep(entry)]
        filtered.sort(key=lambda e: e.timestamp, reverse=True)
        return filtered[:limit]


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _install_view(path: Path) -> None:
    """Install the ``activity_events`` view on the state DB.

    SQLite views are persistent schema objects, so a ``mode=ro`` URI
    connection can't create one. We open a short writable connection,
    run the ``CREATE VIEW`` script, and close it. After that, all read
    paths can use the view through read-only connections.
    """
    if not path.exists():
        return
    try:
        conn = sqlite3.connect(str(path), check_same_thread=False)
    except sqlite3.OperationalError:
        logger.debug("activity_feed: state DB locked; skipping view install", exc_info=True)
        return
    try:
        ensure_activity_events_view(conn)
    finally:
        conn.close()


def _decode_payload(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return {}
        try:
            decoded = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            return {}
        if isinstance(decoded, dict):
            return decoded
    return {}


def _fallback_summary(kind: str, message: str | None, actor: str | None) -> str:
    """Render a minimal summary for events that haven't been migrated to
    the structured format yet. Matches spec §4 back-compat contract:
    "existing events without summary render as 'kind + actor' fallbacks".
    """
    text = (message or "").strip()
    if text:
        return text
    if actor:
        return f"{kind} on {actor}"
    return kind


# ---------------------------------------------------------------------------
# Row-shape helpers — project unified ``messages`` rows into FeedEntry fields.
# ---------------------------------------------------------------------------


def _kind_from_message_row(row: dict[str, Any]) -> str:
    """Pick the ``kind`` label for a messages row.

    Messages rows use ``type`` (notify/alert/inbox_task/event) — we
    prefer the payload's ``event_type`` when present so renamed events
    stay recognizable.
    """
    payload = row.get("payload") or {}
    if isinstance(payload, dict):
        for key in ("event_type", "kind"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    msg_type = row.get("type") or ""
    if msg_type == "event":
        return row.get("subject") or "event"
    return msg_type or "event"


def _severity_from_message_row(row: dict[str, Any]) -> str:
    """Map a message row to the feed's severity vocabulary.

    Alerts carry a ``payload.severity`` set by
    :meth:`Store.upsert_alert`. Fallback: infer severity from kind
    (alert/recovery -> recommendation, error/stuck -> critical, else
    routine).
    """
    payload = row.get("payload") or {}
    if isinstance(payload, dict):
        sev = payload.get("severity")
        if isinstance(sev, str) and sev:
            # Normalize store 'warn'/'error' -> feed vocabulary.
            if sev in {"critical", "error", "stuck"}:
                return "critical"
            if sev in {"warn", "warning"}:
                return "recommendation"
    kind = _kind_from_message_row(row)
    if kind in {"alert", "recovery"}:
        return "recommendation"
    if kind in {"error", "stuck"}:
        return "critical"
    return "routine"


def _entry_id_from_row(row: dict[str, Any]) -> str:
    """Stable entry id for a ``messages`` row (``msg:<id>``)."""
    raw_id = row.get("id") or 0
    return f"msg:{int(raw_id)}"
