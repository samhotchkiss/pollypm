"""Reaper for stale ``~/.pollypm/cockpit_inputs/*.sock`` files (#1368).

Background
----------
:mod:`pollypm.cockpit_input_bridge` binds an AF_UNIX socket per cockpit
process at ``<base_dir>/cockpit_inputs/<kind>-<pid>.sock``. The
``BridgeHandle.stop`` happy-path unlinks on clean shutdown, but:

* ``SIGKILL`` / crash / tmux session teardown skip cleanup entirely.
* The only opportunistic GC is inside
  :func:`cockpit_input_bridge.send_key_to_first_live`, which only
  unlinks a socket if a caller actually attempts a connection AND
  receives ``ConnectionRefusedError`` AND the encoded PID is dead.

In practice the directory accumulates one entry per crashed cockpit
boot. On the reporting machine 318 stale entries had piled up; every
``send_key`` walks all 318 (newest-first sorted) before reaching the
live socket.

This module mirrors :mod:`pollypm.work.worker_marker_reaper` for the
session-marker / worker-marker case: a bootstrap-time sweep that runs
once at ``Supervisor.bootstrap_tmux`` and unlinks any
``cockpit-<pid>.sock`` whose PID is no longer alive.

Staleness signal
----------------
The socket filename already encodes the cockpit PID (see
``cockpit_input_bridge._socket_filename``). We re-use that PID for the
liveness check rather than running ``lsof`` per socket — at 318+ files
the ``lsof`` approach is N round-trips through the kernel, whereas
``os.kill(pid, 0)`` is constant-time per file.

A socket is reaped when ALL of the following hold:

* Filename parses as ``<kind>-<pid>.sock`` with an integer PID.
* ``os.kill(pid, 0)`` returns ``ProcessLookupError`` (or any
  non-permission-error variant signalling the PID is gone).
* The on-disk entry is actually a socket file (not a regular file
  someone dropped here by mistake).

A live cockpit's socket is preserved unconditionally — we err on the
side of leaking one socket rather than nuking the live cockpit's only
discoverable bridge.

Why bootstrap-only
------------------
The cockpit can re-bind the same path while a runtime sweep races; the
existing creation-side code already calls ``socket_path.unlink()``
before ``bind`` to clear stale leftovers, so a runtime sweeper doesn't
add much over the bootstrap reaper. Restricting the sweep to
``bootstrap_tmux`` means: no per-task cockpit is alive yet, so any
reapable socket is by definition dead.
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReapedSocket:
    """Record of a cockpit-input socket that was unlinked by the reaper.

    Surfaces enough context for log lines / audit events: which
    directory it lived in, what kind of pane it served, the PID encoded
    in the filename, the on-disk path, and the staleness reason.
    """

    socket_path: Path
    kind: str
    pid: int | None
    reason: str


def _candidate_dirs(base_dir: Path) -> list[Path]:
    """Return the directories the bridge can drop sockets into.

    Mirrors ``cockpit_input_bridge._bridge_dir`` and the AF_UNIX
    fallback in ``_resolve_bridge_path``. We sweep both so the reaper
    has the same view as ``list_bridge_sockets``.
    """
    primary = base_dir / "cockpit_inputs"
    fallback = Path(tempfile.gettempdir()) / "pollypm-cockpit_inputs"
    out: list[Path] = []
    seen: set[Path] = set()
    for candidate in (primary, fallback):
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        if resolved in seen:
            continue
        seen.add(resolved)
        out.append(candidate)
    return out


def _iter_socket_entries(directory: Path) -> Iterable[Path]:
    """Yield ``*.sock`` files inside ``directory``.

    Returns an empty iterator (rather than raising) when the directory
    is missing — fresh installs and machines that have never booted a
    cockpit simply have nothing to reap.
    """
    try:
        if not directory.is_dir():
            return ()
        return tuple(p for p in directory.iterdir() if p.name.endswith(".sock"))
    except OSError as exc:
        logger.debug(
            "cockpit_socket_reaper: cannot list %s: %s", directory, exc,
        )
        return ()


def _parse_socket_filename(name: str) -> tuple[str, int | None]:
    """Parse ``<kind>-<pid>.sock`` into ``(kind, pid)``.

    Returns ``(kind, None)`` when the filename does not match the
    expected shape — caller should treat unparseable entries as
    unknown and leave them alone.
    """
    stem = name.removesuffix(".sock")
    kind, separator, pid_text = stem.rpartition("-")
    if not separator:
        return (stem, None)
    try:
        return (kind, int(pid_text))
    except ValueError:
        return (kind, None)


def _pid_is_alive(pid: int) -> bool:
    """Return True iff ``pid`` corresponds to a live process.

    ``EPERM`` (PermissionError) means the PID exists but isn't ours —
    treat as alive. Any other ``OSError`` is treated as "can't tell"
    and surfaces as dead, matching the conservative behaviour of
    :func:`cockpit_input_bridge._pid_is_alive`.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _classify(socket_path: Path) -> ReapedSocket | None:
    """Decide whether ``socket_path`` is reapable. ``None`` keeps it.

    A live socket is preserved. A socket whose PID we cannot parse is
    preserved (we don't recognise the format — better to leak one
    than nuke a future format). A socket whose PID is gone is reaped.
    A non-socket file that ended in ``.sock`` is left alone — we do
    not own arbitrary files in this directory.
    """
    name = socket_path.name
    kind, pid = _parse_socket_filename(name)
    if pid is None:
        # Unrecognised shape. Don't touch.
        return None

    try:
        if not socket_path.is_socket():
            return None
    except OSError:
        return None

    if _pid_is_alive(pid):
        return None

    return ReapedSocket(
        socket_path=socket_path,
        kind=kind,
        pid=pid,
        reason=f"owning pid {pid} is not alive",
    )


def _emit_audit(reaped: ReapedSocket) -> None:
    """Emit a ``socket.reaped`` event via :mod:`pollypm.audit`.

    Best-effort: the audit module already swallows write failures, but
    we additionally guard against the import itself failing on installs
    where the audit package is unavailable (shouldn't happen post
    PR #1342, but mirroring the defensive pattern in
    ``session_services/tmux.py``).
    """
    try:
        from pollypm.audit import emit as _audit_emit
        from pollypm.audit.log import EVENT_SOCKET_REAPED
    except Exception:  # noqa: BLE001
        return
    try:
        # ``cockpit_inputs`` lives at the workspace level, not under any
        # one project, but the audit log's central-tail writer only fires
        # when ``project`` is truthy. Use a fixed ``_workspace`` key so
        # operators can ``cat ~/.pollypm/audit/_workspace.jsonl`` to see
        # every cockpit-socket reap across boot cycles.
        _audit_emit(
            event=EVENT_SOCKET_REAPED,
            project="_workspace",
            subject=reaped.socket_path.name,
            actor="system",
            status="ok",
            metadata={
                "path": str(reaped.socket_path),
                "kind": reaped.kind,
                "pid": reaped.pid,
                "reason": reaped.reason,
            },
        )
    except Exception:  # noqa: BLE001
        # Audit is best-effort by design. Never fail the reap path.
        pass


def reap_stale_cockpit_sockets(base_dir: Path) -> list[ReapedSocket]:
    """Bootstrap-time reaper. Walk known socket dirs, unlink dead entries.

    Mirrors :func:`pollypm.work.worker_marker_reaper.reap_orphan_worker_markers`.
    Designed to be called from ``Supervisor._bootstrap_clear_markers``
    so the cockpit boots into a clean ``cockpit_inputs/`` directory.

    Args:
        base_dir: ``self.config.project.base_dir`` — typically
            ``~/.pollypm``. The reaper inspects ``base_dir/cockpit_inputs``
            and the AF_UNIX-fallback ``$TMPDIR/pollypm-cockpit_inputs``.

    Returns:
        The list of :class:`ReapedSocket` records that were unlinked,
        so callers can log / count / surface to the audit log.
    """
    reaped: list[ReapedSocket] = []
    for directory in _candidate_dirs(base_dir):
        for entry in _iter_socket_entries(directory):
            decision = _classify(entry)
            if decision is None:
                continue
            try:
                entry.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning(
                    "cockpit_socket_reaper: failed to unlink %s: %s",
                    entry, exc,
                )
                continue
            logger.warning(
                "cockpit_socket_reaper: reaped %s (kind=%s, pid=%s, reason=%s)",
                decision.socket_path,
                decision.kind,
                decision.pid,
                decision.reason,
            )
            _emit_audit(decision)
            reaped.append(decision)
    return reaped


__all__ = [
    "ReapedSocket",
    "reap_stale_cockpit_sockets",
]
