"""Event projector — unifies disparate event sources into FeedEntry stream.

The activity feed is a *projection* of the underlying event tables, not a
new write path. This module owns the read-side SQL + Python filtering
that turns raw rows into :class:`FeedEntry` records rendered by the
cockpit panel (lf03) and the ``pm activity`` CLI (lf05).

Sources consumed
----------------

1. **Global state store** — ``config.project.state_db``
   - ``messages`` table rows with ``type in ('event', 'notify', 'alert')``
     cover session, alert, heartbeat, transcript, recovery, and worker
     activity. The projector reads those rows directly through the
     unified store API.

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
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

# Best-effort project inference for events whose alert/notify payload
# omits an explicit ``project`` key but whose body names a task by
# ``<project>/<N>`` reference (task_assignment alerts, supervisor
# noise, etc.). The activity feed's Project column otherwise reads
# ``—`` for those rows even though the project context is right
# there in the message body.
#
# Project keys can include hyphens (``blackjack-trainer``) as well as
# underscores (``polly_remote``), so the captured group must accept
# both. Without ``-`` the regex used to truncate ``blackjack-trainer/3``
# to ``trainer`` because ``\b`` matched after the hyphen — see #929.
# We anchor with a leading non-word/non-hyphen lookbehind so the match
# always starts at the true project boundary.
_PROJECT_REF_IN_TEXT_RE = re.compile(
    r"(?<![\w-])([a-zA-Z][a-zA-Z0-9_-]*)/\d+\b"
)

# Per-project agent sessions follow ``<role>_<project_key>`` or
# ``<role>-<project_key>`` naming — ``worker_pollypm``,
# ``architect_polly_remote``, ``worker-blackjack-trainer``, etc. When
# the message body has no task ref and the payload omits ``project``,
# the actor name is the next-best signal. Anchor on known agent role
# prefixes so we don't accidentally claim ``task_assignment`` (a
# generic supervisor sender) is the project ``assignment``.
_ROLE_ACTOR_BASES = (
    "worker",
    "architect",
    "reviewer",
    "operator_pm",
)
# Build prefix variants for both ``_`` and ``-`` separators so an
# actor like ``worker-blackjack-trainer`` resolves to the canonical
# project key ``blackjack-trainer``.
_ROLE_ACTOR_PREFIXES = tuple(
    f"{base}{sep}" for base in _ROLE_ACTOR_BASES for sep in ("_", "-")
)


def _project_from_text(text: str | None) -> str | None:
    if not text:
        return None
    match = _PROJECT_REF_IN_TEXT_RE.search(text)
    return match.group(1) if match else None


def _project_from_actor(actor: str | None) -> str | None:
    if not actor:
        return None
    for prefix in _ROLE_ACTOR_PREFIXES:
        if actor.startswith(prefix) and len(actor) > len(prefix):
            return actor[len(prefix):]
    return None

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
    pinned: bool = False
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
            "pinned": self.pinned,
            "source": self.source,
        }


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

_HISTORIC_FIXTURE_ERROR_SUBSTRINGS: frozenset[str] = frozenset(
    {
        "github sync: failed to create issue for proj/1: gh not found",
        "heartbeatrail tick failed; continuing",
        "plugin bad register_roster hook failed: plugin exploded",
        "rail item workflows.activity badge_provider raised",
        "rail item workflows.activity visibility predicate raised",
        "session listener '_boom' raised for x",
        "sync adapter boomer failed on create for proj/1",
        "sync adapter failing failed on create for proj/1",
    }
)


def _is_noise(
    kind: str,
    summary: str,
    payload: dict[str, Any],
    *,
    actor: str | None = None,
) -> bool:
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
    if _is_historic_fixture_error(kind=kind, actor=actor, summary=summary):
        return True
    return False


def _is_historic_fixture_error(
    *,
    kind: str,
    actor: str | None,
    summary: str,
) -> bool:
    """Hide fixture alerts emitted before tests disabled durable alert writes."""
    if kind != "alert" or actor != "error_log":
        return False
    normalized = _normalize_fixture_text(summary)
    return any(
        marker in normalized
        for marker in _HISTORIC_FIXTURE_ERROR_SUBSTRINGS
    )


def _normalize_fixture_text(text: str) -> str:
    normalized = (text or "").strip().lower()
    if normalized.startswith("[alert]"):
        normalized = normalized[len("[alert]"):].strip()
    return normalized


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

        try:
            from pollypm.store.registry import get_store_by_url
        except Exception:  # noqa: BLE001
            return []

        try:
            store = get_store_by_url(f"sqlite:///{self._state_db}")
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
            # Prefer an explicit ``payload.summary`` when emitters set
            # one (e.g. #1033's ``alert.cleared`` events) — it carries a
            # human-readable string the projector should use directly
            # instead of falling back to ``kind on actor``.
            payload_summary: str | None = None
            if isinstance(payload, dict):
                raw_summary = payload.get("summary")
                if isinstance(raw_summary, str) and raw_summary:
                    payload_summary = raw_summary
            summary = parsed.summary or payload_summary or _fallback_summary(
                kind, message, actor,
            )
            severity = parsed.severity or _severity_from_message_row(row)
            project = (
                parsed.project
                or (payload.get("project") if isinstance(payload, dict) else None)
                or _project_from_text(summary)
                or _project_from_text(message)
                or _project_from_actor(actor)
            )
            verb = parsed.verb or kind
            subject = parsed.subject or (row.get("sender") or None)
            if parsed.extra:
                payload = {**payload, **parsed.extra}
            if _is_noise(kind, summary, payload, actor=actor):
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
                    pinned=bool(payload.get("pinned")) if isinstance(payload, dict) else False,
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
            has_work_transitions = conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'work_transitions'"
            ).fetchone()
            if has_work_transitions is None:
                return []
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
                    pinned=False,
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

        project_filter = set(projects) if projects else None
        entries: list[FeedEntry] = []
        entries.extend(
            self._project_from_state_store(
                since_id=since_id, since_ts=since_ts, limit=limit,
            )
        )
        for project_key, work_db in self._work_dbs:
            if project_filter is not None and project_key not in project_filter:
                continue
            entries.extend(
                self._project_from_work_db(
                    project_key, work_db, since_ts=since_ts, limit=limit,
                )
            )

        # Filter post-projection so callers can constrain without having
        # to wire each filter into every source.
        kind_filter = _expand_kind_filter(kinds)
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
        # Sources use different timestamp shapes — the state store
        # writes ``YYYY-MM-DD HH:MM:SS`` (SQLite ``CURRENT_TIMESTAMP``)
        # while ``work_transitions`` writes
        # ``YYYY-MM-DDTHH:MM:SS.ffffff+00:00`` (Python ``isoformat()``).
        # Lexical sort puts a space (0x20) before a ``T`` (0x54), so
        # newer work-transitions land *after* older state events in
        # the same second (#791). Normalize before sorting.
        filtered.sort(key=_timestamp_sort_key, reverse=True)
        # Collapse repeated alerts (#867). The plan_gate sweep emits one
        # alert per missing-plan project per cycle — once the user has
        # seen it, repeating it every 30s in the activity feed is
        # noise that pushes everything else out of view. Dedupe by
        # (kind, actor, summary) keeps the *newest* occurrence and drops
        # subsequent older copies. Other event kinds pass through
        # untouched: only the high-rep alert path is collapsed.
        deduped: list[FeedEntry] = []
        seen_alert_keys: set[tuple[str, str, str]] = set()
        for entry in filtered:
            if entry.kind == "alert":
                key = (entry.kind, entry.actor or "", entry.summary or "")
                if key in seen_alert_keys:
                    continue
                seen_alert_keys.add(key)
            deduped.append(entry)
        return deduped[:limit]


_ALERT_KIND_FAMILY: frozenset[str] = frozenset({"alert", "alert.cleared"})


def _expand_kind_filter(kinds: Iterable[str] | None) -> set[str] | None:
    """Expand a ``--kind`` filter so an ``alert`` term matches the family.

    #1033 introduced ``alert.cleared`` as a sibling kind for cleared
    alerts. The user-visible filter ``--kind alert`` should match both
    creates and clears so a single command shows the full lifecycle —
    expand the term here rather than asking the user to spell out two
    kinds. Other terms pass through untouched.
    """
    if not kinds:
        return None
    expanded: set[str] = set()
    for raw in kinds:
        if not raw:
            continue
        expanded.add(raw)
        if raw == "alert":
            expanded.update(_ALERT_KIND_FAMILY)
    return expanded


def _timestamp_sort_key(entry: FeedEntry) -> str:
    """Normalize the entry timestamp into a key safe for lexical sort.

    Replaces the SQLite ``YYYY-MM-DD HH:MM:SS`` shape with the ISO-8601
    ``T`` separator so it sorts adjacent to ``isoformat()`` strings
    written by other sources. Handles missing/malformed timestamps by
    returning the empty string (which sorts to the end under ``DESC``).
    """
    raw = getattr(entry, "timestamp", "") or ""
    if not raw:
        return ""
    # `2026-04-26 14:30:00` → `2026-04-26T14:30:00`. Only replace the
    # first space — the time component never contains one.
    return raw.replace(" ", "T", 1)


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
    kind = _kind_from_message_row(row)
    # ``alert.cleared`` is good news (#1033) — render at routine weight
    # so a wall of green clears doesn't visually outweigh a single
    # active alert. Checked before the payload-severity branch because
    # cleared events inherit ``severity='warn'`` from the original alert
    # and would otherwise dress up as ``recommendation``.
    if kind == "alert.cleared":
        return "routine"
    payload = row.get("payload") or {}
    if isinstance(payload, dict):
        sev = payload.get("severity")
        if isinstance(sev, str) and sev:
            # Normalize store 'warn'/'error' -> feed vocabulary.
            if sev in {"critical", "error", "stuck"}:
                return "critical"
            if sev in {"warn", "warning"}:
                return "recommendation"
    if kind in {"alert", "recovery"}:
        return "recommendation"
    if kind in {"error", "stuck"}:
        return "critical"
    return "routine"


def _entry_id_from_row(row: dict[str, Any]) -> str:
    """Stable entry id for a ``messages`` row (``msg:<id>``)."""
    raw_id = row.get("id") or 0
    return f"msg:{int(raw_id)}"
