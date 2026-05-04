"""TTY-less keystroke bridge for cockpit Textual apps (#1109 follow-up).

Background
----------
``LinuxDriver`` reads keystrokes via ``os.read(sys.__stdin__.fileno(), …)``
on a background thread (see ``textual/drivers/linux_driver.py``
``run_input_thread``). When no tmux client is attached to the cockpit's
session, ``tmux send-keys`` writes to the pane's PTY but the cockpit
process's stdin reader does not observe those bytes — Textual's input
event loop stays silent even though the render loop continues to fire.

The first attempt at #1109 (SHA ``42941125``) added ``pm up
--phantom-client``: a detached ``script``-wrapped ``tmux attach -d`` to
keep a TTY consumer alive. Empirically that does not unwedge the
cockpit either: even with the phantom client attached, ``send-keys``
from a third process still no-ops against the cockpit-pane.

Root-cause fix (this module)
----------------------------
Provide a side-channel input path that does not depend on PTY plumbing
at all:

1. Cockpit-side (``start_input_bridge``): a Textual ``App`` opens a Unix
   socket under ``$POLLYPM_HOME/cockpit_inputs/<pid>.sock``. A worker
   thread accepts connections and reads newline-delimited key tokens.
   Each token is dispatched via ``App.simulate_key`` (which calls
   ``post_message`` — thread-safe across threads in Textual) so the key
   reaches the focused widget exactly as if a real terminal had produced
   it.

2. Caller-side (``send_key``): a thin client connects to the most recent
   bridge socket for a given pane "kind" and writes the key token.
   Wired into the CLI as ``pm cockpit-send-key <key> [--kind …]``.

The bridge survives the TTY-less wedge because no part of its data path
runs through ``sys.__stdin__``. Existing TTY input continues to work
unchanged — ``LinuxDriver`` is untouched.

Contract
--------
- Inputs: a running ``textual.app.App`` instance + a directory in which
  to drop the socket.
- Outputs: a ``BridgeHandle`` exposing ``socket_path`` and ``stop()``.
- Side effects: creates a Unix-domain socket file; spawns a daemon
  thread.
- Invariants: best-effort — failure to bind the socket logs a warning
  and returns ``None``; never propagates an exception that would block
  cockpit boot.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - type-only
    from textual.app import App

logger = logging.getLogger(__name__)

_LISTEN_BACKLOG = 64

# Newline-delimited key tokens. A line that is exactly ``"<bs>"`` is the
# special ``backspace`` key, ``"<cr>"`` is ``enter``, etc. Anything else
# is passed straight through to ``App.simulate_key`` which already
# accepts both single characters and Textual key names like ``"space"``.
_SPECIAL_TOKENS: dict[str, str] = {
    "<bs>": "backspace",
    "<cr>": "enter",
    "<esc>": "escape",
    "<tab>": "tab",
    "<space>": "space",
    "<up>": "up",
    "<down>": "down",
    "<left>": "left",
    "<right>": "right",
    "<pgup>": "pageup",
    "<pgdn>": "pagedown",
    "<home>": "home",
    "<end>": "end",
}


def _normalize_token(raw: str) -> str | None:
    """Map a wire-format token to the form ``simulate_key`` expects."""
    token = raw.strip()
    if not token:
        return None
    lowered = token.lower()
    if lowered in _SPECIAL_TOKENS:
        return _SPECIAL_TOKENS[lowered]
    # Allow ``ctrl+x`` / ``shift+a`` style modifiers verbatim — Textual
    # already understands those.
    return token


@dataclass
class BridgeHandle:
    """Handle returned by :func:`start_input_bridge`.

    ``socket_path`` is the absolute filesystem path callers should
    connect to. ``stop()`` shuts down the listener thread and removes
    the socket file (best-effort).
    """

    socket_path: Path
    _server_socket: socket.socket
    _thread: threading.Thread
    _stop_flag: threading.Event

    def stop(self) -> None:
        self._stop_flag.set()
        try:
            self._server_socket.close()
        except OSError:
            pass
        try:
            self.socket_path.unlink()
        except OSError:
            pass


def _bridge_dir(config_path: Path) -> Path:
    """Return the directory we drop bridge sockets into.

    ``config_path`` is something like ``~/.pollypm/config.toml`` —
    ``parent`` lands us in the install root. We keep a sibling
    ``cockpit_inputs/`` directory so the sockets co-locate with
    ``cockpit_debug.log`` and friends.

    Note: AF_UNIX paths are capped at ~104 chars on macOS / ~108 on
    Linux. Callers should pair this with ``_resolve_bridge_path`` which
    falls back to ``$TMPDIR`` for over-long paths (production
    ``~/.pollypm/cockpit_inputs/`` is well under the limit; pytest
    ``tmp_path`` directories are not).
    """
    return config_path.parent / "cockpit_inputs"


# AF_UNIX path-length cap. macOS uses ``sun_path[104]``; Linux uses
# ``[108]``. Use the more conservative number so behaviour is uniform.
_AF_UNIX_PATH_LIMIT = 100


def _resolve_bridge_path(config_path: Path, filename: str) -> Path:
    """Pick a socket path that fits inside ``sun_path``.

    Prefers ``<config>.parent/cockpit_inputs/<filename>``. If the
    resulting path exceeds the AF_UNIX limit (e.g. inside deep pytest
    tmp dirs), falls back to ``$TMPDIR/pollypm-cockpit_inputs/<filename>``.
    """
    primary = _bridge_dir(config_path) / filename
    if len(str(primary)) <= _AF_UNIX_PATH_LIMIT:
        return primary
    import tempfile

    fallback_dir = Path(tempfile.gettempdir()) / "pollypm-cockpit_inputs"
    return fallback_dir / filename


def _socket_filename(kind: str, pid: int) -> str:
    safe_kind = "".join(c if c.isalnum() else "_" for c in kind)[:32]
    return f"{safe_kind}-{pid}.sock"


def _socket_owner_pid(socket_path: Path) -> int | None:
    stem = socket_path.name.removesuffix(".sock")
    _, separator, pid_text = stem.rpartition("-")
    if not separator:
        return None
    try:
        return int(pid_text)
    except ValueError:
        return None


def _pid_is_alive(pid: int) -> bool:
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


def _socket_owner_pid_alive(socket_path: Path) -> bool:
    pid = _socket_owner_pid(socket_path)
    return pid is not None and _pid_is_alive(pid)


def start_input_bridge(
    app: "App",
    *,
    kind: str,
    config_path: Path,
) -> BridgeHandle | None:
    """Start a Unix-socket key-bridge attached to ``app``.

    Returns ``None`` (after logging) if the socket cannot be bound — the
    cockpit is still functional via TTY input, so we never fail boot.

    Args:
        app: The running Textual ``App``. Must already be in
            ``run()``-ed state by the time keys arrive (we capture a
            reference for ``call_from_thread``).
        kind: A short label for this app surface (``"cockpit"``,
            ``"dashboard"``, ``"inbox"``, …). Encoded into the socket
            filename so multiple cockpit-pane processes don't collide.
        config_path: PollyPM config path; we drop sockets in
            ``<config>.parent/cockpit_inputs/``.
    """
    socket_path = _resolve_bridge_path(
        config_path, _socket_filename(kind, os.getpid())
    )
    try:
        socket_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning(
            "cockpit_input_bridge: mkdir %s failed: %s", socket_path.parent, exc
        )
        return None
    # Stale leftovers from a previous crash would make ``bind`` fail
    # with EADDRINUSE — best-effort unlink first.
    try:
        socket_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning(
            "cockpit_input_bridge: stale socket %s could not be unlinked: %s",
            socket_path,
            exc,
        )

    server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server_sock.bind(str(socket_path))
    except OSError as exc:
        logger.warning("cockpit_input_bridge: bind %s failed: %s", socket_path, exc)
        try:
            server_sock.close()
        except OSError:
            pass
        return None

    server_sock.listen(_LISTEN_BACKLOG)
    # Short accept timeout so the listener thread can poll the stop
    # flag without blocking shutdown indefinitely.
    server_sock.settimeout(0.5)

    stop_flag = threading.Event()

    def _dispatch_key(key_token: str) -> None:
        # ``simulate_key`` is a thin wrapper around ``post_message``.
        # Textual routes cross-thread ``post_message`` calls through the
        # app loop, so the bridge accept loop does not have to block on
        # synchronous ``call_from_thread`` for every queued key.
        try:
            app.simulate_key(key_token)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cockpit_input_bridge: simulate_key(%r) failed: %s", key_token, exc)

    def _serve() -> None:
        while not stop_flag.is_set():
            try:
                conn, _ = server_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                # Socket closed — listener is shutting down.
                break
            try:
                conn.settimeout(2.0)
                with conn.makefile("r", encoding="utf-8", newline="\n") as stream:
                    for line in stream:
                        token = _normalize_token(line)
                        if token is None:
                            continue
                        _dispatch_key(token)
            except (OSError, socket.timeout):
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    thread = threading.Thread(
        target=_serve,
        name=f"cockpit-input-bridge-{kind}",
        daemon=True,
    )
    thread.start()

    logger.info("cockpit_input_bridge: listening on %s", socket_path)
    return BridgeHandle(
        socket_path=socket_path,
        _server_socket=server_sock,
        _thread=thread,
        _stop_flag=stop_flag,
    )


def list_bridge_sockets(config_path: Path, *, kind: str | None = None) -> list[Path]:
    """Return all live-looking bridge sockets, newest first.

    "Live-looking" only means: file exists in the bridge dir and is a
    socket. We do not stat for connectivity here — callers should
    ``connect()`` and treat ECONNREFUSED as "stale, try the next one".

    Checks both the primary ``<config>/cockpit_inputs/`` directory and
    the AF_UNIX-fallback ``$TMPDIR/pollypm-cockpit_inputs/`` so callers
    get a consistent view regardless of whether the cockpit's config
    path was short enough to host the socket directly.
    """
    import tempfile

    candidates_dirs = [
        _bridge_dir(config_path),
        Path(tempfile.gettempdir()) / "pollypm-cockpit_inputs",
    ]
    matches: list[Path] = []
    prefix: str | None = None
    if kind is not None:
        safe_kind = "".join(c if c.isalnum() else "_" for c in kind)[:32]
        prefix = f"{safe_kind}-"
    seen: set[Path] = set()
    for bridge_dir in candidates_dirs:
        if not bridge_dir.is_dir():
            continue
        for entry in bridge_dir.iterdir():
            if not entry.name.endswith(".sock"):
                continue
            if prefix is not None and not entry.name.startswith(prefix):
                continue
            resolved = entry.resolve()
            if resolved in seen:
                continue
            try:
                if entry.is_socket():
                    matches.append(entry)
                    seen.add(resolved)
            except OSError:
                continue
    matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return matches


def send_key(socket_path: Path, key: str, *, timeout: float = 2.0) -> None:
    """Connect to ``socket_path`` and forward ``key``.

    Raises ``OSError`` on connect/send failure so the CLI can surface a
    real exit code.
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(socket_path))
        payload = (key + "\n").encode("utf-8")
        sock.sendall(payload)
    finally:
        try:
            sock.close()
        except OSError:
            pass


def send_key_to_first_live(
    config_path: Path,
    key: str,
    *,
    kind: str | None = None,
    timeout: float = 2.0,
) -> Path | None:
    """Send ``key`` to the most-recently-bound bridge socket.

    Returns the path of the socket we delivered to, or ``None`` if no
    bridge socket accepted the connection. Stale (orphaned) sockets are
    silently skipped — they raise ``ConnectionRefusedError`` /
    ``FileNotFoundError`` on ``connect``.
    """
    last_error: OSError | None = None
    for candidate in list_bridge_sockets(config_path, kind=kind):
        try:
            send_key(candidate, key, timeout=timeout)
            return candidate
        except FileNotFoundError as exc:
            last_error = exc
            continue
        except ConnectionRefusedError as exc:
            last_error = exc
            # ECONNREFUSED can be transient if the bridge accept backlog is
            # momentarily full. Only unlink when the PID encoded in the
            # filename is no longer alive; otherwise a rapid key burst can
            # delete the live cockpit's only discoverable socket.
            if _socket_owner_pid_alive(candidate):
                logger.info(
                    "cockpit_input_bridge: keeping refused socket for live pid: %s",
                    candidate,
                )
                continue
            try:
                candidate.unlink()
            except OSError:
                pass
            continue
        except OSError as exc:
            last_error = exc
            continue
    if last_error is not None:
        logger.info("cockpit_input_bridge: no live socket for kind=%s: %s", kind, last_error)
    return None
