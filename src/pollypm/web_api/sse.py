"""Server-Sent Events stream for the audit log.

Per `docs/web-api-spec.md` §4:

- ``GET /api/v1/events`` returns ``text/event-stream``.
- Each message is one audit-log line as JSON.
- ``id: <ts>`` lets browsers reconnect via ``Last-Event-ID``.
- ``?since=<ISO-8601>`` is the cold-start replay cursor.
- ``?project=`` filters by project key (server-side).
- ``?event=`` is a glob pattern matched against the event name.
- Server emits ``: keep-alive`` comment lines every 15 seconds.

The audit log is append-only JSONL split across per-project files
plus a central tail. We tail every relevant file by polling the
file size periodically — that's cheaper than a watchdog notify chain
and works identically across macOS / Linux without a kqueue / inotify
dependency. Personal-use volume makes 250ms polls free.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

from pollypm.audit.log import (
    AuditEvent,
    central_log_path,
    project_log_path,
    read_events,
)
from pollypm.config import PollyPMConfig

logger = logging.getLogger(__name__)


# Tunable knobs. The defaults match the spec ("every 15 seconds")
# and a 250ms poll keeps tail latency under a quarter-second.
KEEPALIVE_INTERVAL_S = 15.0
TAIL_POLL_INTERVAL_S = 0.25


@dataclass(slots=True)
class _Subscription:
    """Per-connection filter + cursor state."""

    project: str | None
    event_glob: str | None
    last_ts: str | None  # exclusive lower bound for replay/tail


def _matches(event: AuditEvent, sub: _Subscription) -> bool:
    if sub.project is not None and event.project != sub.project:
        return False
    if sub.event_glob is not None and not fnmatch.fnmatch(event.event, sub.event_glob):
        return False
    return True


def _format_sse(event: AuditEvent) -> str:
    """Render an audit event as a single SSE message."""
    payload = {
        "schema": event.schema,
        "ts": event.ts,
        "project": event.project,
        "event": event.event,
        "subject": event.subject,
        "actor": event.actor,
        "status": event.status,
        "metadata": event.metadata or {},
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    # SSE message format: blank line terminates the message. ``id``
    # populates ``Last-Event-ID`` on the client.
    return f"event: audit\nid: {event.ts}\ndata: {body}\n\n"


def _candidate_paths(config: PollyPMConfig, project: str | None) -> list[Path]:
    """Return every audit-log path the subscription should tail.

    When a project filter is supplied we tail just that project's
    per-project log + central tail. Without a filter we tail the
    central tails for every registered project — that's the union of
    everything ``read_events`` would surface.
    """
    paths: list[Path] = []
    if project is not None:
        proj = config.projects.get(project)
        if proj is not None:
            per_project = project_log_path(proj.path)
            if per_project is not None:
                paths.append(per_project)
        paths.append(central_log_path(project))
        return paths

    # No filter — central tail per project. Per-project logs are the
    # source of truth, but for a fleet-wide subscription the central
    # tail mirror is the cheaper read path.
    seen: set[str] = set()
    for key, proj in config.projects.items():
        per_project = project_log_path(proj.path)
        if per_project is not None and str(per_project) not in seen:
            paths.append(per_project)
            seen.add(str(per_project))
        central = central_log_path(key)
        if str(central) not in seen:
            paths.append(central)
            seen.add(str(central))
    return paths


def _replay_initial(config: PollyPMConfig, sub: _Subscription) -> list[AuditEvent]:
    """Return the events the client should receive on connect.

    When ``last_ts`` is None ⇒ no replay (tail-only). Otherwise the
    server reads every relevant project log and returns events with
    ``ts > last_ts`` so the client can rebuild missed state.
    """
    if sub.last_ts is None:
        return []
    seen: set[tuple[str, str, str, str]] = set()
    out: list[AuditEvent] = []
    project_keys: list[str]
    if sub.project is not None and sub.project in config.projects:
        project_keys = [sub.project]
    else:
        project_keys = list(config.projects.keys())
    for key in project_keys:
        proj = config.projects[key]
        try:
            events = read_events(key, project_path=proj.path, since=sub.last_ts)
        except Exception as exc:  # noqa: BLE001
            logger.debug("sse replay: read_events failed for %s: %s", key, exc)
            continue
        for event in events:
            if not _matches(event, sub):
                continue
            sig = (event.ts, event.event, event.subject, event.project)
            if sig in seen:
                continue
            seen.add(sig)
            out.append(event)
    out.sort(key=lambda ev: ev.ts)
    return out


async def _tail_paths(
    paths: list[Path],
    *,
    sub: _Subscription,
) -> AsyncIterator[AuditEvent]:
    """Poll each path's size; yield new lines as they're appended.

    ``offsets`` keeps the byte position we've consumed in each file so
    truncations / rotations get re-read from the beginning. The audit
    log itself never truncates, so this is mostly defensive.
    """
    offsets: dict[Path, int] = {}
    for path in paths:
        # Start tailing from EOF so we don't replay the entire file
        # on connect — replay is the caller's responsibility (handled
        # by ``_replay_initial`` before tailing).
        try:
            offsets[path] = path.stat().st_size if path.exists() else 0
        except OSError:
            offsets[path] = 0

    while True:
        for path in list(paths):
            try:
                if not path.exists():
                    continue
                size = path.stat().st_size
            except OSError:
                continue
            prev = offsets.get(path, 0)
            if size < prev:
                # Truncation — start over from the beginning.
                prev = 0
            if size == prev:
                continue
            try:
                with open(path, "rb") as fh:
                    fh.seek(prev)
                    chunk = fh.read(size - prev)
            except OSError as exc:
                logger.debug("sse tail: read %s failed: %s", path, exc)
                continue
            offsets[path] = size
            try:
                text = chunk.decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                continue
            for line in text.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                event = AuditEvent.from_dict(obj)
                if not _matches(event, sub):
                    continue
                yield event
        await asyncio.sleep(TAIL_POLL_INTERVAL_S)


async def stream_audit_events(
    config: PollyPMConfig,
    *,
    project: str | None,
    event_glob: str | None,
    since: str | None,
    last_event_id: str | None,
) -> AsyncIterator[bytes]:
    """Yield SSE message bytes for ``GET /api/v1/events``.

    Per spec, ``Last-Event-ID`` (sent by the browser on auto-reconnect)
    takes precedence over the ``?since=`` query parameter.
    """
    cursor = last_event_id or since
    sub = _Subscription(project=project, event_glob=event_glob, last_ts=cursor)
    paths = _candidate_paths(config, project)

    # Replay first.
    for event in _replay_initial(config, sub):
        yield _format_sse(event).encode("utf-8")
        sub.last_ts = event.ts

    last_keepalive = asyncio.get_event_loop().time()
    sent_anything = False

    async def _tail_iter():
        async for event in _tail_paths(paths, sub=sub):
            yield event

    tail = _tail_iter().__aiter__()
    while True:
        try:
            tail_task = asyncio.create_task(tail.__anext__())
            timeout = max(0.0, KEEPALIVE_INTERVAL_S - (asyncio.get_event_loop().time() - last_keepalive))
            done, _ = await asyncio.wait({tail_task}, timeout=timeout)
            if tail_task in done:
                event = tail_task.result()
                if sub.last_ts is not None and event.ts <= sub.last_ts:
                    continue
                yield _format_sse(event).encode("utf-8")
                sub.last_ts = event.ts
                sent_anything = True
            else:
                tail_task.cancel()
                # Keep-alive comment per spec.
                yield b": keep-alive\n\n"
                last_keepalive = asyncio.get_event_loop().time()
        except StopAsyncIteration:
            break
        except (asyncio.CancelledError, GeneratorExit):
            try:
                tail_task.cancel()
            except Exception:  # noqa: BLE001
                pass
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("sse stream error: %s", exc)
            break
        finally:
            # If the consumer disconnected (ConnectionResetError surfaces
            # via the FastAPI streaming response), the next yield will
            # raise. We also bail out here when the loop exits cleanly.
            pass

    # If we never sent anything, emit one keep-alive so test clients
    # that drain bytes don't see an empty stream.
    if not sent_anything:
        yield b": keep-alive\n\n"


__all__ = [
    "KEEPALIVE_INTERVAL_S",
    "TAIL_POLL_INTERVAL_S",
    "stream_audit_events",
]
