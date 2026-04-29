"""Tests for the issues #963 / #966 two-phase tmux session launch contract.

Original (pre-#963) flow: ``tmux new-window -d 'claude --resume X'`` —
pane materializes already running the slow agent CLI bootstrap, leaving
the user staring at a blank pane for several seconds.

#963 flow (broken — see #966): open an empty pane via ``new-window``,
then deliver the launch command via ``send-keys '<huge sh -lc ...>'
Enter``. The very long single-quoted argument never survived the
typing path: zsh stayed in ``quote>`` continuation forever and the
agent CLI never started.

Current flow (#966):
1. ``tmux new-window -d`` (no command) — pane materializes empty so the
   user's click feels instant.
2. ``tmux respawn-pane -k -t <pane> <argv tokens>`` — tmux spawns the
   launcher directly via ``execvp``, with no shell-quoting roundtrip.

These tests pin down both phases at the ``TmuxClient`` boundary and
through ``TmuxSessionService.create`` (the path used by the per-task
worker spawn, the supervisor recreate path, and the operator/heartbeat
boot).
"""

from __future__ import annotations

import subprocess

from pollypm.tmux.client import TmuxClient


def _capture_run(monkeypatch, *, pane_id: str = "%42") -> list[list[str]]:
    """Install a fake ``subprocess.run`` that records every tmux call.

    Returns the list (mutated in place by the recorder) so tests can
    walk through the command sequence.
    """
    captured: list[list[str]] = []

    def fake_run(args, **kwargs):
        captured.append(list(args))

        class Result:
            returncode = 0
            stdout = pane_id
            stderr = ""

        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)
    return captured


# ---------------------------------------------------------------------------
# Phase 1 — empty pane creation
# ---------------------------------------------------------------------------


def test_phase1_create_window_does_not_carry_command(monkeypatch) -> None:
    """Phase 1: ``new-window`` invocation lacks the launch command.

    The pane must materialize as a default shell — no agent CLI banner,
    no slow bootstrap. The launch command lives in Phase 2.
    """
    captured = _capture_run(monkeypatch)
    client = TmuxClient()
    monkeypatch.setattr(client, "has_session", lambda name: False)

    client.create_window(
        "storage", "task-acme-7", "claude --resume long-id", detached=True,
    )

    new_window_calls = [c for c in captured if "new-window" in c]
    assert len(new_window_calls) == 1
    new_window = new_window_calls[0]
    # The launch command must not appear anywhere in the new-window call.
    assert "claude --resume long-id" not in new_window
    assert "claude" not in new_window
    # tmux must be detached (-d) so the pane materializes without
    # stealing focus from whatever pane the user is already viewing.
    assert "-d" in new_window
    # tmux must capture the new pane's id (-P -F '#{pane_id}') so the
    # caller can target Phase 2 by stable id.
    assert "-P" in new_window
    assert "#{pane_id}" in new_window


def test_phase1_create_session_does_not_carry_command(monkeypatch) -> None:
    """Same Phase 1 contract for ``new-session`` (used at bootstrap)."""
    captured = _capture_run(monkeypatch, pane_id="%99")
    client = TmuxClient()
    monkeypatch.setattr(client, "has_session", lambda name: False)

    client.create_session("storage", "main", "codex --foo")

    new_session_calls = [c for c in captured if "new-session" in c]
    assert len(new_session_calls) == 1
    assert "codex --foo" not in new_session_calls[0]
    assert "codex" not in new_session_calls[0]


# ---------------------------------------------------------------------------
# Phase 2 — launch command delivered via respawn-pane (#966)
# ---------------------------------------------------------------------------


def test_phase2_respawn_pane_delivers_launch_command(monkeypatch) -> None:
    """Phase 2: the launch command is handed to ``respawn-pane`` (argv
    form). No ``send-keys`` step — that path was abandoned in #966."""
    captured = _capture_run(monkeypatch)
    client = TmuxClient()
    monkeypatch.setattr(client, "has_session", lambda name: False)

    client.create_window("storage", "task-acme-7", "claude --resume long-id")

    respawn_calls = [c for c in captured if "respawn-pane" in c]
    assert len(respawn_calls) == 1
    respawn = respawn_calls[0]
    # ``-k`` (kill the empty Phase-1 shell) and the launch tokens must
    # all be present as separate argv entries.
    assert "-k" in respawn
    assert "claude" in respawn
    assert "--resume" in respawn
    assert "long-id" in respawn
    # No ``send-keys`` anywhere in the launch path — that was the #966
    # regression vector.
    assert not any("send-keys" in c for c in captured)


def test_phase2_targets_pane_id_when_available(monkeypatch) -> None:
    """Phase 2 targets the pane_id returned by Phase 1, not the
    ``session:window`` string.

    Pane ids are stable across rename/move; window-name targets resolve
    through tmux at call time and can race when other windows of the
    same name exist (the bug pattern that motivated #934).
    """
    captured = _capture_run(monkeypatch, pane_id="%77")
    client = TmuxClient()
    monkeypatch.setattr(client, "has_session", lambda name: False)

    pane_id = client.create_window("storage", "task-foo-1", "claude --resume X")
    assert pane_id == "%77"

    respawn = next(c for c in captured if "respawn-pane" in c)
    # ``-t %77`` must appear: the pane_id is the target, not "storage:task-foo-1".
    assert "-t" in respawn
    target_idx = respawn.index("-t")
    assert respawn[target_idx + 1] == "%77"


def test_phase2_skipped_when_window_already_exists(monkeypatch) -> None:
    """Idempotency guard: re-running ``create_window`` for an existing
    window must NOT re-respawn the launch command (otherwise we'd
    clobber an already-running agent CLI).
    """
    from pollypm.tmux.client import TmuxWindow

    captured = _capture_run(monkeypatch)
    client = TmuxClient()
    monkeypatch.setattr(client, "has_session", lambda name: True)
    monkeypatch.setattr(
        client,
        "list_windows",
        lambda name: [TmuxWindow(
            session=name, index=0, name="task-foo-1", active=True,
            pane_id="%5", pane_current_command="bash",
            pane_current_path="/tmp", pane_dead=False,
        )],
    )

    result = client.create_window("storage", "task-foo-1", "claude --resume X")
    assert result is None
    assert not any("respawn-pane" in c for c in captured)
    assert not any("send-keys" in c for c in captured)
    assert not any("new-window" in c for c in captured)


def test_phase2_skipped_when_session_already_exists(monkeypatch) -> None:
    """Same idempotency contract for ``create_session``."""
    captured = _capture_run(monkeypatch)
    client = TmuxClient()
    monkeypatch.setattr(client, "has_session", lambda name: True)

    result = client.create_session("storage", "main", "claude --resume X")
    assert result is None
    assert not any("respawn-pane" in c for c in captured)
    assert not any("send-keys" in c for c in captured)
    assert not any("new-session" in c for c in captured)


def test_phase2_does_not_use_send_keys_for_huge_payloads(monkeypatch) -> None:
    """#966 — the regression vector was a ``sh -lc '<huge base64>'``
    string typed at zsh via ``send-keys``. The closing single-quote
    never made it across the typing layer, so zsh stayed in
    ``quote>`` continuation forever and the agent CLI never started.

    Contract: the launch path must not call ``send-keys`` for ANY
    payload — short, long, or base64-shaped.
    """
    huge_b64 = "A" * 1500  # mimic real runtime_launcher payload size
    huge_command = f"sh -lc 'exec /opt/python -m pollypm.runtime_launcher {huge_b64}'"

    captured = _capture_run(monkeypatch)
    client = TmuxClient()
    monkeypatch.setattr(client, "has_session", lambda name: False)

    client.create_window("storage", "task-payload", huge_command)

    assert not any("send-keys" in c for c in captured), (
        "launch path must never use send-keys (#966)"
    )
    respawn = next(c for c in captured if "respawn-pane" in c)
    # The huge payload must arrive as a single argv element (it's the
    # third positional argument to ``sh -lc``), not chopped up.
    assert huge_b64 in " ".join(respawn)
    # And the wrapper tokens are individual argv entries.
    assert "sh" in respawn
    assert "-lc" in respawn


def test_phase2_handles_shell_special_characters(monkeypatch) -> None:
    """Single-quotes, double-quotes, newlines, and backticks inside the
    launch command would all break a ``send-keys`` path. The argv form
    of ``respawn-pane`` doesn't re-parse through a shell, so these
    characters must survive verbatim in the tokenized argv.
    """
    nasty = (
        "sh -lc 'exec /bin/echo \"hello\\nworld\" `date` $foo' "
    )
    captured = _capture_run(monkeypatch)
    client = TmuxClient()
    monkeypatch.setattr(client, "has_session", lambda name: False)

    client.create_window("storage", "task-nasty", nasty)

    respawn = next(c for c in captured if "respawn-pane" in c)
    # The inner ``sh -lc`` body lives as a single argv element; whatever
    # tokenization happens must preserve the embedded characters.
    assert any("hello" in tok and "world" in tok for tok in respawn), respawn
    assert any("`date`" in tok for tok in respawn), respawn
    # And there must be no ``send-keys`` typing layer that could choke
    # on the backticks or quotes.
    assert not any("send-keys" in c for c in captured)


def test_phase2_argv_first_token_is_binary(monkeypatch) -> None:
    """``respawn-pane`` argv form: tmux execs argv[0] directly. The
    first token after ``-t <target>`` must be the binary, with each
    subsequent argument as its own positional arg — not a single
    shell-quoted blob."""
    captured = _capture_run(monkeypatch)
    client = TmuxClient()
    monkeypatch.setattr(client, "has_session", lambda name: False)

    client.create_window(
        "storage",
        "task-direct",
        "sh -lc 'exec /usr/bin/python -m pollypm.runtime_launcher PAYLOAD'",
    )

    respawn = next(c for c in captured if "respawn-pane" in c)
    # Find ``-t <target>`` then assert the next token is the binary.
    target_idx = respawn.index("-t")
    assert respawn[target_idx + 2] == "sh"
    assert respawn[target_idx + 3] == "-lc"
    # The fourth element is the inner shell command body — a single
    # positional argv entry, not split.
    inner = respawn[target_idx + 4]
    assert "exec /usr/bin/python -m pollypm.runtime_launcher PAYLOAD" in inner


# ---------------------------------------------------------------------------
# Backwards compatibility — explicit two_phase=False still inlines
# ---------------------------------------------------------------------------


def test_two_phase_false_inlines_command(monkeypatch) -> None:
    """The legacy single-call form is still available behind
    ``two_phase=False`` for non-agent callers (e.g. ``recover_session``
    which respawns a pane scoped to the lifetime of one command).
    """
    captured = _capture_run(monkeypatch)
    client = TmuxClient()
    monkeypatch.setattr(client, "has_session", lambda name: False)

    client.create_window("storage", "win", "echo hello", two_phase=False)
    new_window = next(c for c in captured if "new-window" in c)
    assert "echo hello" in new_window
    assert not any("send-keys" in c for c in captured)


# ---------------------------------------------------------------------------
# Integration: TmuxSessionService.create routes through two-phase
# ---------------------------------------------------------------------------


def test_session_service_create_uses_two_phase(monkeypatch, tmp_path) -> None:
    """End-to-end: ``TmuxSessionService.create`` (the path the per-task
    worker spawn, supervisor reconcile, and account-switch flows all
    funnel through) drives the two-phase contract.

    We mock at the ``subprocess.run`` boundary so this exercises the
    real ``TmuxClient.create_window`` code path.
    """
    from pollypm.models import (
        AccountConfig,
        PollyPMConfig,
        PollyPMSettings,
        ProjectSettings,
        ProviderKind,
    )
    from pollypm.session_services.tmux import TmuxSessionService

    base_dir = tmp_path / ".pollypm"
    config = PollyPMConfig(
        project=ProjectSettings(
            name="Fixture",
            root_dir=tmp_path,
            tmux_session="pollypm",
            base_dir=base_dir,
            logs_dir=base_dir / "logs",
            snapshots_dir=base_dir / "snapshots",
            state_db=base_dir / "state.db",
        ),
        pollypm=PollyPMSettings(controller_account="acct"),
        accounts={
            "acct": AccountConfig(name="acct", provider=ProviderKind.CLAUDE),
        },
        sessions={},
    )

    # Minimal store stub.
    class _Store:
        def list_sessions(self) -> list:
            return []

    captured: list[list[str]] = []

    def fake_run(args, **kwargs):
        captured.append(list(args))

        class Result:
            returncode = 0
            # has-session -> 1 (no), list-windows -> empty
            stdout = "%21" if args and args[0] == "tmux" and "new-window" in args else ""
            stderr = ""

        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)

    service = TmuxSessionService(config=config, store=_Store())
    # Force ``has_session`` to True so the create_session branch isn't
    # taken (we want to hit create_window, the more common path).
    monkeypatch.setattr(service.tmux, "has_session", lambda name: True)
    # Skip stabilization (we're not testing provider readiness here).
    monkeypatch.setattr(service, "_stabilize", lambda *a, **kw: None)
    # Skip _find_window so the pre-existing-window short-circuit doesn't
    # fire — the test wants to exercise the actual two-phase create.
    monkeypatch.setattr(service, "_find_window", lambda s, w: None)
    # Skip pipe-pane and history-limit (they hit subprocess too but are
    # noise for this test — they're already exercised by other tests).
    monkeypatch.setattr(service.tmux, "pipe_pane", lambda *a, **kw: None)
    monkeypatch.setattr(service.tmux, "set_pane_history_limit", lambda *a, **kw: None)
    monkeypatch.setattr(service.tmux, "set_window_option", lambda *a, **kw: None)

    handle = service.create(
        name="task-acme-7",
        provider="claude",
        account="acct",
        cwd=tmp_path,
        command="claude --resume xyz",
        window_name="task-acme-7",
        tmux_session="pollypm-storage-closet",
        stabilize=False,
    )
    assert handle.window_name == "task-acme-7"

    # The two phases must be present in the recorded subprocess calls.
    new_window_calls = [c for c in captured if "new-window" in c]
    respawn_calls = [c for c in captured if "respawn-pane" in c]
    assert new_window_calls, "expected a new-window call from TmuxClient.create_window"
    assert respawn_calls, (
        "expected a respawn-pane call delivering the launch command (#966)"
    )
    # Phase 1: launch command must not appear in new-window.
    assert "claude --resume xyz" not in new_window_calls[0]
    # Phase 2: launch tokens must appear as separate argv entries in respawn-pane.
    rp = respawn_calls[0]
    assert "claude" in rp
    assert "--resume" in rp
    assert "xyz" in rp
    # And the launch path must not fall back to send-keys.
    assert not any("send-keys" in c for c in captured)
