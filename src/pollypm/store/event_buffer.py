"""Fire-and-forget event buffer — bounded queue → batched ``messages`` inserts.

Most PollyPM subsystems want to emit a firehose event without caring whether
the DB is momentarily busy: the supervisor heartbeat, flow-engine transitions,
pane-pattern matches, etc. Routing every single emit through a synchronous
insert puts a serialized-writer stall on the hot path and can cascade into
worker-session latency.

This module solves that with a classic producer/consumer:

* Callers invoke :meth:`EventBuffer.append` — a non-blocking enqueue onto a
  bounded :class:`queue.Queue` (capacity 10k). Overflow drops the oldest
  pending event with a log line (never the new one — the newest signal is
  usually the most interesting).
* A single background thread drains the queue, batching up to
  ``batch_size`` rows per transaction or flushing every ``flush_interval``
  seconds — whichever comes first.
* Every batch commits in one short-lived writer transaction so the pool's
  ``pool_size=1`` never wedges.

Shutdown paths (``close()`` and the ``SIGTERM``/``SIGINT`` handlers) are
idempotent and guarantee a final flush of anything already enqueued. The
signal handler intentionally chains to any pre-existing handler so PollyPM
doesn't eat Ctrl-C for the host process.

Issue #338. The buffer currently only emits rows with ``type='event'``;
future issues layering on notify/alert tiers will inject via their own
:class:`SQLAlchemyStore` methods rather than this queue.
"""

from __future__ import annotations

import json
import logging
import queue
import signal
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import insert

from pollypm.storage.sqlite_pragmas import retry_on_database_locked
from pollypm.store.schema import messages

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from pollypm.store.sqlalchemy_store import SQLAlchemyStore


logger = logging.getLogger(__name__)


# Module-level guard so multiple ``EventBuffer`` instances in the same
# process don't each clobber the other's signal handler. The first buffer
# wins; later buffers chain through it via the process-wide handler stack.
_SIGNAL_HANDLERS_LOCK = threading.Lock()
_BUFFERS_FOR_SIGNAL: list["EventBuffer"] = []
_PREVIOUS_SIGNAL_HANDLERS: dict[int, Any] = {}
_SIGNAL_HANDLERS_INSTALLED = False


@dataclass(frozen=True)
class _PendingEvent:
    """One row queued for the background drain.

    Frozen so the drain thread can hand it to SQLAlchemy without worrying
    that the producer thread will mutate the dict mid-insert.
    """

    scope: str
    sender: str
    subject: str
    payload_json: str


# Sentinel pushed onto the queue by ``close()`` to unblock a drainer that
# is parked on a blocking ``queue.get``. It is NOT a real event — the
# drain loop filters it out before flushing a batch.
_SHUTDOWN_SENTINEL = _PendingEvent(
    scope="__shutdown__",
    sender="__shutdown__",
    subject="__shutdown__",
    payload_json="{}",
)


class EventBuffer:
    """Background-thread drain of events into the unified ``messages`` table.

    Parameters
    ----------
    store:
        The :class:`SQLAlchemyStore` owning the writer engine. Held by
        reference so the buffer always uses the store's current engine
        (important if the store ever hot-swaps URLs during tests).
    batch_size:
        Soft upper bound on rows drained per transaction. Default 100;
        tuned so a single ``executemany`` fits comfortably below SQLite's
        implicit statement cache ceiling.
    flush_interval:
        Seconds to wait for a full batch before committing whatever is
        pending. Default 0.1s — low enough that events are visible to
        readers within a frame of the UI refresh rate.
    capacity:
        Maximum number of queued-but-undrained events. Default 10_000.
        Full-capacity behavior drops the OLDEST pending event (not the
        new one) with a warning log, so live signals never vanish.
    install_signal_handlers:
        If ``True`` (default), install process-wide ``SIGTERM``/``SIGINT``
        handlers on the first buffer in the process. The handlers chain
        to whatever handler was previously registered, so Ctrl-C still
        bubbles up to pytest / the supervisor main loop.
    """

    def __init__(
        self,
        store: "SQLAlchemyStore",
        *,
        batch_size: int = 100,
        flush_interval: float = 0.1,
        capacity: int = 10_000,
        install_signal_handlers: bool = True,
    ) -> None:
        if batch_size <= 0:
            raise ValueError(
                "EventBuffer batch_size must be positive. "
                "A non-positive batch disables the drain and wedges emitters. "
                "Fix: pass batch_size=100 (the default) or a smaller positive int."
            )
        if flush_interval <= 0:
            raise ValueError(
                "EventBuffer flush_interval must be positive. "
                "A non-positive interval disables the wake-up timer and stalls flushes. "
                "Fix: pass flush_interval=0.1 (default) or any positive float."
            )
        if capacity <= 0:
            raise ValueError(
                "EventBuffer capacity must be positive. "
                "A non-positive capacity means producers block forever on a full queue. "
                "Fix: pass capacity=10_000 (default) or any positive int."
            )

        self._store = store
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._capacity = capacity

        # Bounded deque-backed queue. We drive overflow manually so we drop
        # the OLDEST pending event (not the incoming one): see ``append``.
        self._queue: queue.Queue[_PendingEvent] = queue.Queue(maxsize=capacity)
        self._stop_event = threading.Event()
        self._closed = False
        self._close_lock = threading.Lock()

        self._thread = threading.Thread(
            target=self._drain_loop,
            name="pollypm-event-buffer",
            daemon=True,
        )
        self._thread.start()

        if install_signal_handlers:
            _register_buffer_for_signals(self)

    # ------------------------------------------------------------------
    # Producer API
    # ------------------------------------------------------------------

    def append(
        self,
        scope: str,
        sender: str,
        subject: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Enqueue an event for the background drain. Never blocks.

        If the queue is at ``capacity``, the OLDEST pending event is
        dropped (with a warning log) to make room. Dropping the newest
        incoming event would mean losing live signal while preserving
        stale ones; the opposite is what operators actually want.

        Parameters
        ----------
        scope:
            Routing scope ('root', 'workspace', or a project key).
        sender:
            Logical origin of the event — session name, subsystem tag,
            etc. Free-form string.
        subject:
            Short human-readable title. Flows straight into the row's
            ``subject`` column.
        payload:
            Optional structured blob. Serialized with ``json.dumps`` so
            it stays queryable via JSON1 operators downstream.
        """
        if self._closed:
            # Dropped-after-close is worth a debug but not a warning — it
            # generally means a worker emitted during shutdown, which is
            # expected. Supervisors wanting stricter semantics should
            # call ``close()`` only after fully quiescing producers.
            logger.debug(
                "event-buffer: dropped append after close (subject=%r)", subject
            )
            return

        event = _PendingEvent(
            scope=scope,
            sender=sender,
            subject=subject,
            payload_json=json.dumps(payload if payload is not None else {}),
        )
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            # Make room by discarding the oldest queued event, then retry.
            # Window between get + put is fine — we're the only one
            # shrinking the queue on overflow, and the drain thread only
            # shrinks it via get() which is safe to race with.
            try:
                dropped = self._queue.get_nowait()
                logger.warning(
                    "event-buffer: capacity (%d) reached, dropped oldest event "
                    "(scope=%r, sender=%r, subject=%r)",
                    self._capacity,
                    dropped.scope,
                    dropped.sender,
                    dropped.subject,
                )
            except queue.Empty:
                # The drain thread raced us and emptied the queue; fall
                # through to put_nowait which will now succeed.
                pass
            try:
                self._queue.put_nowait(event)
            except queue.Full:  # pragma: no cover - extremely tight race
                logger.warning(
                    "event-buffer: capacity (%d) still full after eviction, "
                    "dropping incoming event (subject=%r)",
                    self._capacity,
                    subject,
                )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self, *, timeout: float = 5.0) -> None:
        """Stop the drain thread after flushing pending events.

        Idempotent — repeated calls are no-ops. The graceful path bounds
        itself to ``timeout`` seconds (default 5s) so a hanging writer
        doesn't block process shutdown indefinitely. Any events still
        queued when the thread fails to join are logged.
        """
        with self._close_lock:
            if self._closed:
                return
            self._closed = True

        self._stop_event.set()
        # Wake the drainer if it's parked on a blocking ``queue.get``.
        # The sentinel is filtered out in ``_flush_batch`` so it never
        # reaches the DB.
        try:
            self._queue.put_nowait(_SHUTDOWN_SENTINEL)
        except queue.Full:
            # Buffer is full — the drainer will wake when it next
            # consumes anyway. Not worth evicting a real event for a
            # wake-up nudge.
            pass
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            remaining = self._queue.qsize()
            event_word = "event" if remaining == 1 else "events"
            logger.warning(
                "event-buffer: drain thread did not exit within %.1fs; "
                "%d %s may be unflushed",
                timeout,
                remaining,
                event_word,
            )

        _deregister_buffer_from_signals(self)

    def is_closed(self) -> bool:
        """Return ``True`` if :meth:`close` has been invoked."""
        return self._closed

    def pending_count(self) -> int:
        """Return the current queue depth. Useful for tests."""
        return self._queue.qsize()

    # ------------------------------------------------------------------
    # Drain thread
    # ------------------------------------------------------------------

    def _drain_loop(self) -> None:
        """Main loop for the background drainer thread.

        Uses a short ``Queue.get(timeout=flush_interval)`` call as the
        wake-up timer so the thread spends most of its life parked in
        the kernel's condvar queue rather than polling.
        """
        while not self._stop_event.is_set():
            batch = self._collect_batch()
            if batch:
                self._flush_batch(batch)

        # Shutdown: drain whatever is still queued so ``close()`` /
        # ``SIGTERM`` honor the "flush pending before exit" contract.
        final = self._drain_remaining()
        if final:
            self._flush_batch(final)

    def _collect_batch(self) -> list[_PendingEvent]:
        """Gather up to ``batch_size`` events.

        Blocks on the first ``get`` for up to ``flush_interval`` so an
        idle buffer costs no CPU. Once that first event arrives, any
        further events already in the queue are drained non-blockingly
        — waiting for a full batch would delay flushes when traffic is
        bursty, and the whole point of ``flush_interval`` is to bound
        the time an event sits unflushed.
        """
        batch: list[_PendingEvent] = []
        try:
            first = self._queue.get(timeout=self._flush_interval)
        except queue.Empty:
            return batch
        batch.append(first)

        while len(batch) < self._batch_size:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return batch

    def _drain_remaining(self) -> list[_PendingEvent]:
        """Non-blocking drain of whatever is currently queued."""
        remaining: list[_PendingEvent] = []
        while True:
            try:
                remaining.append(self._queue.get_nowait())
            except queue.Empty:
                return remaining

    def _flush_batch(self, batch: list[_PendingEvent]) -> None:
        """Insert ``batch`` as ``messages`` rows in a single writer transaction.

        Errors are logged and swallowed — the buffer's contract is
        fire-and-forget; a transient DB hiccup must not poison the
        drain thread. Production callers that need delivery guarantees
        should use the synchronous ``SQLAlchemyStore.record_event``.

        The shutdown sentinel (if present) is filtered out here so it
        never reaches the DB.
        """
        if not batch:
            return
        rows = [
            {
                "scope": ev.scope,
                "type": "event",
                "tier": "immediate",
                "recipient": "*",
                "sender": ev.sender,
                "state": "open",
                "subject": ev.subject,
                "body": "",
                "payload_json": ev.payload_json,
                "labels": "[]",
            }
            for ev in batch
            if ev is not _SHUTDOWN_SENTINEL
        ]
        if not rows:
            return

        # #1050 — wrap the flush in ``retry_on_database_locked`` so the
        # common case (transient WAL contention from a competing writer)
        # doesn't drop audit rows on the floor. The shared retry ladder
        # is the same one #1021 added to ``queue.enqueue`` / ``queue.fail``;
        # it sleeps 0.1 s / 0.5 s / 2.0 s between attempts and re-raises
        # the final lock error if contention is sustained. Sustained
        # contention still drops (same behaviour as before this fix) —
        # spool-to-disk fallback is tracked as a follow-up.
        def _do_flush() -> None:
            with self._store.transaction() as conn:
                conn.execute(insert(messages), rows)

        try:
            retry_on_database_locked(_do_flush, label="event_buffer.flush")
        except Exception:
            row_word = "row" if len(rows) == 1 else "rows"
            logger.exception(
                "event-buffer: flush failed (%d %s dropped)", len(rows), row_word,
            )


# --------------------------------------------------------------------------
# Signal handler plumbing
# --------------------------------------------------------------------------


def _register_buffer_for_signals(buffer: EventBuffer) -> None:
    """Attach ``buffer`` to the process-wide signal-shutdown list.

    First buffer in the process installs SIGTERM + SIGINT handlers that
    call :meth:`EventBuffer.close` on every registered buffer, then
    re-raises / delegates to the previously installed handler so host
    code keeps the behavior it expects.
    """
    global _SIGNAL_HANDLERS_INSTALLED

    with _SIGNAL_HANDLERS_LOCK:
        _BUFFERS_FOR_SIGNAL.append(buffer)
        if _SIGNAL_HANDLERS_INSTALLED:
            return

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                _PREVIOUS_SIGNAL_HANDLERS[sig] = signal.getsignal(sig)
                signal.signal(sig, _on_shutdown_signal)
            except (ValueError, OSError):
                # signal.signal only works from the main thread. Tests or
                # embedded hosts may invoke the buffer from a worker
                # thread, in which case we silently skip installation —
                # the buffer still supports explicit ``close()``.
                logger.debug(
                    "event-buffer: could not install signal handler for %s "
                    "(not main thread?) — falling back to explicit close()",
                    sig,
                )
                continue
        _SIGNAL_HANDLERS_INSTALLED = True


def _deregister_buffer_from_signals(buffer: EventBuffer) -> None:
    """Remove ``buffer`` from the shutdown list.

    We intentionally do NOT restore the previous signal handler here —
    other buffers in the same process may still be registered. On final
    process exit the OS tears the handler down anyway.
    """
    with _SIGNAL_HANDLERS_LOCK:
        try:
            _BUFFERS_FOR_SIGNAL.remove(buffer)
        except ValueError:
            pass


def _on_shutdown_signal(signum: int, frame: Any) -> None:
    """SIGTERM / SIGINT handler: flush every registered buffer, then chain."""
    with _SIGNAL_HANDLERS_LOCK:
        buffers = list(_BUFFERS_FOR_SIGNAL)
        previous = _PREVIOUS_SIGNAL_HANDLERS.get(signum)

    for buf in buffers:
        try:
            buf.close()
        except Exception:  # pragma: no cover - shutdown best-effort
            logger.exception("event-buffer: close() failed during signal flush")

    # Chain to whatever handler was in place before we installed ours.
    # ``signal.default_int_handler`` for SIGINT raises KeyboardInterrupt
    # which is what pytest / interactive shells expect.
    if callable(previous) and previous not in (
        signal.SIG_DFL,
        signal.SIG_IGN,
        _on_shutdown_signal,
    ):
        previous(signum, frame)
    elif previous == signal.SIG_DFL:
        # Restore default and re-raise so the OS semantics take over.
        signal.signal(signum, signal.SIG_DFL)
        signal.raise_signal(signum)


__all__ = ["EventBuffer"]
