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
