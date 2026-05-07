"""Tests for the cockpit-input-socket reaper (#1368).

The reaper unlinks stale ``cockpit_inputs/<kind>-<pid>.sock`` entries
whose owning PID is no longer alive. We use real Unix sockets bound at
real on-disk paths (no kernel-state mocks) so the tests exercise the
same ``Path.is_socket`` / ``os.kill`` codepaths the production reaper
hits at supervisor boot.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Iterator

import pytest

from pollypm.cockpit_socket_reaper import (
    ReapedSocket,
    reap_stale_cockpit_sockets,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bind_socket(path: Path) -> socket.socket:
    """Bind a real AF_UNIX socket at ``path`` (caller is responsible for
    closing + unlinking on cleanup)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    sock.listen(1)
    return sock


def _spawn_briefly() -> int:
    """Spawn a short-lived subprocess and return its PID after it exits.

    Used to obtain a PID that is guaranteed dead at the time of the
    reap call, so we can name-encode it into a stale socket filename
    and verify the reaper picks it up. We capture the PID via
    ``Popen`` (``subprocess.run`` returns a ``CompletedProcess`` that
    intentionally does not expose ``pid`` once the child has reaped).
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.exit(0)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pid = proc.pid
    proc.wait()
    return pid


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def base_dir() -> Iterator[Path]:
    """Synthesize a ``self.config.project.base_dir`` under a short root.

    The reaper inspects ``<base_dir>/cockpit_inputs``. We avoid
    pytest's ``tmp_path`` here because its default location lives
    deep enough that the resulting AF_UNIX paths exceed the 104-char
    ``sun_path`` limit on macOS — the same constraint the production
    bridge code worked around in ``_resolve_bridge_path``. Using
    ``/tmp/pb-<rand>`` keeps every socket path short.
    """
    root = Path("/tmp") / f"pbrp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    base = root / "p"
    (base / "cockpit_inputs").mkdir(parents=True)
    try:
        yield base
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Bootstrap reaper — staleness + preservation
# ---------------------------------------------------------------------------


def test_reaps_socket_whose_pid_is_dead(base_dir: Path) -> None:
    """A real socket file whose encoded PID is dead is unlinked."""
    dead_pid = _spawn_briefly()
    stale = base_dir / "cockpit_inputs" / f"cockpit-{dead_pid}.sock"
    sock = _bind_socket(stale)
    sock.close()  # binding leaves the inode; no live process holds it now.
    assert stale.exists()

    reaped = reap_stale_cockpit_sockets(base_dir)

    assert not stale.exists(), "stale socket should have been unlinked"
    assert len(reaped) == 1
    entry = reaped[0]
    assert isinstance(entry, ReapedSocket)
    assert entry.socket_path == stale
    assert entry.kind == "cockpit"
    assert entry.pid == dead_pid
    assert "not alive" in entry.reason


def test_preserves_socket_owned_by_live_process(base_dir: Path) -> None:
    """A socket whose encoded PID is the live test process is preserved.

    ``os.getpid()`` is by definition alive for the duration of the
    test. The reaper must not touch it — that's the live cockpit on
    a real system.
    """
    live_pid = os.getpid()
    live_path = base_dir / "cockpit_inputs" / f"cockpit-{live_pid}.sock"
    sock = _bind_socket(live_path)
    try:
        reaped = reap_stale_cockpit_sockets(base_dir)
        assert reaped == []
        assert live_path.exists()
        assert live_path.is_socket()
    finally:
        sock.close()
        live_path.unlink(missing_ok=True)


def test_reaps_only_stale_when_mixed(base_dir: Path) -> None:
    """Mixed dir of live + stale sockets: only the stale ones go."""
    live_pid = os.getpid()
    dead_pid = _spawn_briefly()

    live_path = base_dir / "cockpit_inputs" / f"cockpit-{live_pid}.sock"
    stale_path = base_dir / "cockpit_inputs" / f"dashboard-{dead_pid}.sock"
    live_sock = _bind_socket(live_path)
    stale_sock = _bind_socket(stale_path)
    stale_sock.close()
    try:
        reaped = reap_stale_cockpit_sockets(base_dir)

        assert {r.socket_path for r in reaped} == {stale_path}
        assert live_path.exists() and live_path.is_socket()
        assert not stale_path.exists()
    finally:
        live_sock.close()
        live_path.unlink(missing_ok=True)


def test_skips_files_with_unparseable_names(base_dir: Path) -> None:
    """A ``.sock`` file that doesn't parse as ``<kind>-<pid>.sock`` is left."""
    weird = base_dir / "cockpit_inputs" / "no-pid-here.sock"
    weird.parent.mkdir(parents=True, exist_ok=True)
    # Write a regular file rather than a socket; the reaper should
    # leave it alone either way because the PID parse fails.
    weird.write_text("not a real socket")

    reaped = reap_stale_cockpit_sockets(base_dir)

    # ``no-pid-here.sock`` parses as kind="no-pid", pid="here" → ValueError.
    # Reaper falls into the ``pid is None`` branch and preserves the file.
    assert reaped == []
    assert weird.exists()


def test_skips_non_socket_file_with_dead_pid_name(base_dir: Path) -> None:
    """A regular file whose name encodes a dead PID is preserved.

    The reaper only touches actual socket inodes — we don't own
    arbitrary ``.sock``-named regular files in this directory.
    """
    dead_pid = _spawn_briefly()
    impostor = base_dir / "cockpit_inputs" / f"cockpit-{dead_pid}.sock"
    impostor.write_text("not a socket")

    reaped = reap_stale_cockpit_sockets(base_dir)

    assert reaped == []
    assert impostor.exists()


def test_handles_missing_directory() -> None:
    """A base_dir without ``cockpit_inputs/`` is a no-op, not an error."""
    root = Path("/tmp") / f"pbrp-fresh-{uuid.uuid4().hex[:8]}"
    root.mkdir()
    try:
        # No ``cockpit_inputs`` subdir.
        reaped = reap_stale_cockpit_sockets(root)
        assert reaped == []
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_sweeps_tmpdir_fallback_directory() -> None:
    """Stale sockets in the AF_UNIX-fallback ``$TMPDIR`` dir are also reaped.

    ``cockpit_input_bridge._resolve_bridge_path`` falls back to
    ``$TMPDIR/pollypm-cockpit_inputs`` when the primary path exceeds
    the ``sun_path`` limit. The reaper must sweep both so a long
    workspace path doesn't quietly leak sockets in /tmp.
    """
    root = Path("/tmp") / f"pbrp-fb-{uuid.uuid4().hex[:8]}"
    base = root / ".pollypm"
    base.mkdir(parents=True)

    fallback_dir = Path(tempfile.gettempdir()) / "pollypm-cockpit_inputs"
    fallback_dir.mkdir(parents=True, exist_ok=True)

    dead_pid = _spawn_briefly()
    stale_clean = fallback_dir / f"cockpit-{dead_pid}.sock"
    sock = _bind_socket(stale_clean)
    sock.close()
    try:
        reaped = reap_stale_cockpit_sockets(base)
        assert any(r.socket_path == stale_clean for r in reaped), (
            f"fallback dir not swept: {reaped}"
        )
        assert not stale_clean.exists()
    finally:
        try:
            stale_clean.unlink()
        except FileNotFoundError:
            pass
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Integration: bootstrap path emits a ``socket.reaped`` audit event
# ---------------------------------------------------------------------------


def test_emits_socket_reaped_audit_event(
    base_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reaping a stale socket appends a ``socket.reaped`` line to the
    central audit tail.

    Audit emit is best-effort and project-empty; the central tail
    lives at ``$POLLYPM_AUDIT_HOME/_unknown.jsonl`` for workspace-wide
    events. Setting the env var redirects writes off the user's real
    audit log so the test never touches live state.
    """
    audit_home = tmp_path / "audit-home"
    monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(audit_home))

    dead_pid = _spawn_briefly()
    stale = base_dir / "cockpit_inputs" / f"cockpit-{dead_pid}.sock"
    sock = _bind_socket(stale)
    sock.close()

    reaped = reap_stale_cockpit_sockets(base_dir)
    assert reaped, "expected the stale socket to be reaped"

    # ``cockpit_socket_reaper`` writes against the ``_workspace`` project
    # key (workspace-wide, not project-scoped). Read via the public
    # API so the test stays decoupled from the on-disk filename.
    from pollypm.audit.log import central_log_path, read_events

    central = central_log_path("_workspace")
    assert central.exists(), f"audit central tail missing: {central}"

    events = read_events("_workspace")
    socket_events = [e for e in events if e.event == "socket.reaped"]
    assert socket_events, f"expected socket.reaped event, got {[e.event for e in events]}"
    last = socket_events[-1]
    assert last.subject == stale.name
    assert last.metadata.get("pid") == dead_pid
    assert last.metadata.get("kind") == "cockpit"
    assert last.actor == "system"


# ---------------------------------------------------------------------------
# Integration: full create-then-reap cycle
# ---------------------------------------------------------------------------


def test_bootstrap_path_creates_then_reaps_only_stale(base_dir: Path) -> None:
    """Bootstrap-style flow: bind sockets, kill some "owners" by closing
    + naming them with dead PIDs, run reaper, assert only stale ones
    are gone."""
    live_pid = os.getpid()
    dead_pid_a = _spawn_briefly()
    dead_pid_b = _spawn_briefly()
    while dead_pid_b == dead_pid_a:
        dead_pid_b = _spawn_briefly()

    paths = {
        "live": base_dir / "cockpit_inputs" / f"cockpit-{live_pid}.sock",
        "dead_a": base_dir / "cockpit_inputs" / f"cockpit-{dead_pid_a}.sock",
        "dead_b": base_dir / "cockpit_inputs" / f"dashboard-{dead_pid_b}.sock",
    }
    socks: dict[str, socket.socket] = {}
    for key, p in paths.items():
        socks[key] = _bind_socket(p)

    # Close + leave file for the dead ones (simulating crashed cockpits
    # that didn't run BridgeHandle.stop). Live one stays bound.
    socks["dead_a"].close()
    socks["dead_b"].close()

    try:
        reaped = reap_stale_cockpit_sockets(base_dir)
        reaped_paths = {r.socket_path for r in reaped}

        assert paths["live"].exists()
        assert not paths["dead_a"].exists()
        assert not paths["dead_b"].exists()
        assert reaped_paths == {paths["dead_a"], paths["dead_b"]}
    finally:
        socks["live"].close()
        for p in paths.values():
            p.unlink(missing_ok=True)
