import subprocess

from pollypm.tmux.client import TmuxClient


def test_current_session_name_returns_none_outside_tmux(monkeypatch) -> None:
    monkeypatch.delenv("TMUX", raising=False)
    client = TmuxClient()

    assert client.current_session_name() is None


def test_current_window_index_returns_none_outside_tmux(monkeypatch) -> None:
    monkeypatch.delenv("TMUX", raising=False)
    client = TmuxClient()

    assert client.current_window_index() is None


def test_current_pane_id_returns_none_outside_tmux(monkeypatch) -> None:
    monkeypatch.delenv("TMUX", raising=False)
    client = TmuxClient()

    assert client.current_pane_id() is None


def test_new_session_attached_invokes_tmux(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(args, check=False):
        captured["args"] = args
        captured["check"] = check

        class Result:
            returncode = 0

        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)
    client = TmuxClient()

    result = client.new_session_attached("pollypm-onboarding", "onboarding", "echo hello")

    assert result == 0
    assert captured["check"] is False
    assert captured["args"] == [
        "tmux",
        "new-session",
        "-A",
        "-s",
        "pollypm-onboarding",
        "-n",
        "onboarding",
        "echo hello",
    ]


def test_run_check_false_returns_124_on_timeout(monkeypatch) -> None:
    """A wedged tmux server must not crash callers using ``check=False``.

    Sam's screenshot 2026-04-26 14:08 PM showed ``tmux has-session -t
    pollypm`` hanging past the 15s timeout and propagating
    ``subprocess.TimeoutExpired`` all the way up to ``pm``, crashing
    the CLI. ``has_session`` and friends use ``check=False`` precisely
    so they can interpret the returncode — they should see a non-zero
    result on timeout, not an exception.
    """

    def fake_run(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout", 15))

    monkeypatch.setattr("subprocess.run", fake_run)
    client = TmuxClient()

    result = client.run("has-session", "-t", "pollypm", check=False)
    assert result.returncode == 124
    assert "timed out" in result.stderr


def test_has_session_returns_false_when_tmux_hangs(monkeypatch) -> None:
    """``has_session`` returns False (not raises) when tmux is wedged."""

    def fake_run(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout", 15))

    monkeypatch.setattr("subprocess.run", fake_run)
    client = TmuxClient()

    assert client.has_session("pollypm") is False


def test_send_keys_long_text_uses_per_call_named_buffer(monkeypatch, tmp_path) -> None:
    """#808: long sends must use a per-call named tmux paste buffer so
    two concurrent sends can't paste each other's text. Two
    interleaved long sends should hit two distinct buffer names and
    each load-buffer must pair with a paste-buffer that names the
    same buffer.
    """
    captured: list[list[str]] = []

    def fake_run(args, **kwargs):
        captured.append(list(args))

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)
    client = TmuxClient()

    long_a = "A" * 200
    long_b = "B" * 200

    # Simulate two concurrent senders by interleaving manually — both
    # sends must end up with their own buffer name, never the unnamed
    # default.
    client.send_keys("pollypm:0.1", long_a, press_enter=False)
    client.send_keys("pollypm:0.2", long_b, press_enter=False)

    load_calls = [args for args in captured if "load-buffer" in args]
    paste_calls = [args for args in captured if "paste-buffer" in args]

    assert len(load_calls) == 2
    assert len(paste_calls) == 2
    # Every load and every paste must carry an explicit ``-b <name>``
    # option — the unnamed global buffer is what made cross-talk
    # possible.
    for call in load_calls + paste_calls:
        assert "-b" in call, f"expected -b in {call!r}"
        idx = call.index("-b")
        assert call[idx + 1].startswith("pollypm-"), call

    # The two sends must use distinct buffer names so concurrent
    # callers can't see each other's text.
    def _buffer_name(call: list[str]) -> str:
        return call[call.index("-b") + 1]

    load_names = [_buffer_name(c) for c in load_calls]
    paste_names = [_buffer_name(c) for c in paste_calls]
    assert load_names[0] != load_names[1]
    # Each paste must reference the same name as its preceding load.
    assert paste_names == load_names


def test_run_check_true_still_raises_on_timeout(monkeypatch) -> None:
    """``check=True`` callers opted into propagation; preserve that contract."""

    def fake_run(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout", 15))

    monkeypatch.setattr("subprocess.run", fake_run)
    client = TmuxClient()

    try:
        client.run("kill-server", check=True)
    except subprocess.TimeoutExpired:
        return
    raise AssertionError("expected TimeoutExpired to propagate when check=True")


def test_create_window_two_phase_default_omits_command(monkeypatch) -> None:
    """#963/#966 — ``create_window`` opens an empty pane (no command) by
    default, then hands the launch command to the pane via tmux
    ``respawn-pane``. The user sees a pane appear instantly; the agent
    CLI takes over a moment later without any ``send-keys`` typing.
    """
    captured: list[list[str]] = []

    def fake_run(args, **kwargs):
        captured.append(list(args))

        class Result:
            returncode = 0
            stdout = "%42"
            stderr = ""

        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)
    client = TmuxClient()
    # Bypass the has_session/list_windows pre-check so we exercise the
    # creation path directly.
    monkeypatch.setattr(client, "has_session", lambda name: False)

    pane_id = client.create_window("storage", "task-foo-1", "claude --resume xyz")

    assert pane_id == "%42"
    # Two tmux invocations: new-window WITHOUT the command, then respawn-pane.
    new_window = next(c for c in captured if "new-window" in c)
    respawn = next(c for c in captured if "respawn-pane" in c)
    assert "claude --resume xyz" not in new_window, (
        "new-window must not carry the launch command in two-phase mode"
    )
    # Argv tokens land as separate positional args.
    assert "claude" in respawn
    assert "--resume" in respawn
    assert "xyz" in respawn
    assert "-k" in respawn  # respawn-pane kills the empty Phase-1 shell
    # No send-keys path — that was the #966 regression vector.
    assert not any("send-keys" in c for c in captured)


def test_spawn_commands_propagate_launcher_workspace_env(monkeypatch) -> None:
    """#1143: tmux pane processes must honor the launcher's HOME/PATH.

    An existing tmux server keeps its own global environment. Without
    explicit per-spawn env, ``HOME=/tmp/isolated pm up`` can still spawn
    cockpit panes with the server's canonical HOME.
    """
    captured: list[list[str]] = []

    def fake_run(args, **kwargs):
        captured.append(list(args))

        class Result:
            returncode = 0
            stdout = "%42"
            stderr = ""

        return Result()

    monkeypatch.setenv("HOME", "/tmp/polly-empty-state")
    monkeypatch.setenv("PATH", "/tmp/polly-bin:/usr/bin:/bin")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/polly-empty-state/.config")
    monkeypatch.setenv("UNRELATED_SECRET", "do-not-forward")
    monkeypatch.setattr("subprocess.run", fake_run)

    client = TmuxClient()
    monkeypatch.setattr(client, "has_session", lambda name: False)

    client.create_session("storage", "main", "codex --resume abc")
    client.create_window("storage", "task-foo-1", "claude --resume xyz")
    client.split_window("pollypm:PollyPM", "pm cockpit-pane polly")
    client.respawn_pane("%42", "pm cockpit")

    spawn_calls = [
        call
        for call in captured
        if len(call) > 1
        and call[1] in {"new-session", "new-window", "split-window", "respawn-pane"}
    ]
    assert spawn_calls
    for call in spawn_calls:
        env_values = [
            call[index + 1]
            for index, arg in enumerate(call)
            if arg == "-e" and index + 1 < len(call)
        ]
        assert "HOME=/tmp/polly-empty-state" in env_values
        assert "PATH=/tmp/polly-bin:/usr/bin:/bin" in env_values
        assert "XDG_CONFIG_HOME=/tmp/polly-empty-state/.config" in env_values
        assert "UNRELATED_SECRET=do-not-forward" not in env_values


def test_create_window_two_phase_false_inlines_command(monkeypatch) -> None:
    """The legacy single-call form is preserved behind ``two_phase=False``
    for callers that need it (none currently — but keeps the door open
    for non-agent callers that want a pane scoped to a single command's
    lifetime)."""
    captured: list[list[str]] = []

    def fake_run(args, **kwargs):
        captured.append(list(args))

        class Result:
            returncode = 0
            stdout = "%7"
            stderr = ""

        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)
    client = TmuxClient()
    monkeypatch.setattr(client, "has_session", lambda name: False)

    client.create_window("storage", "win", "echo hi", two_phase=False)
    new_window = next(c for c in captured if "new-window" in c)
    assert "echo hi" in new_window
    assert not any("send-keys" in c for c in captured)
    assert not any("respawn-pane" in c for c in captured)


def test_create_session_two_phase_default_omits_command(monkeypatch) -> None:
    """#963/#966 — same two-phase guarantee on ``create_session``:
    new-session opens an empty pane, then ``respawn-pane`` (argv form)
    hands the launch command to the new pane without any ``send-keys``
    typing layer."""
    captured: list[list[str]] = []

    def fake_run(args, **kwargs):
        captured.append(list(args))

        class Result:
            returncode = 0
            stdout = "%9"
            stderr = ""

        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)
    client = TmuxClient()
    monkeypatch.setattr(client, "has_session", lambda name: False)

    pane_id = client.create_session("storage", "main", "codex --resume abc")

    assert pane_id == "%9"
    new_session = next(c for c in captured if "new-session" in c)
    respawn = next(c for c in captured if "respawn-pane" in c)
    assert "codex --resume abc" not in new_session, (
        "new-session must not carry the launch command in two-phase mode"
    )
    assert "codex" in respawn
    assert "--resume" in respawn
    assert "abc" in respawn
    # respawn-pane should target the pane_id we just got back from new-session,
    # not the session:window string — pane_ids are stable across renames.
    assert "%9" in respawn
    # No send-keys path — that was the #966 regression vector.
    assert not any("send-keys" in c for c in captured)


def test_create_session_two_phase_skips_send_when_session_exists(monkeypatch) -> None:
    """The idempotency contract holds: if the session already exists,
    ``create_session`` returns None and never respawns the pane."""
    captured: list[list[str]] = []

    def fake_run(args, **kwargs):
        captured.append(list(args))

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)
    client = TmuxClient()
    monkeypatch.setattr(client, "has_session", lambda name: True)

    result = client.create_session("storage", "main", "claude --resume xyz")
    assert result is None
    assert not any("respawn-pane" in c for c in captured)
    assert not any("send-keys" in c for c in captured)
    assert not any("new-session" in c for c in captured)


def test_create_window_two_phase_skips_send_when_window_exists(monkeypatch) -> None:
    """If the window already exists, ``create_window`` is a no-op — no
    new-window, no respawn-pane, no send-keys."""
    from pollypm.tmux.client import TmuxWindow

    captured: list[list[str]] = []

    def fake_run(args, **kwargs):
        captured.append(list(args))

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)
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

    result = client.create_window("storage", "task-foo-1", "claude --resume xyz")
    assert result is None
    assert not any("respawn-pane" in c for c in captured)
    assert not any("send-keys" in c for c in captured)
    assert not any("new-window" in c for c in captured)
