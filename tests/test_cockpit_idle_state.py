from pathlib import Path

from pollypm.cockpit import CockpitRouter


def test_operator_session_state_stays_ready_when_claude_prompt_precedes_long_status_line(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm'}\"\n"
    )

    launch = type(
        "Launch",
        (),
        {
            "window_name": "pm-operator",
            "session": type(
                "Session",
                (),
                {
                    "name": "operator",
                    "role": "operator-pm",
                    "project": "pollypm",
                    "provider": type("P", (), {"value": "claude"})(),
                },
            )(),
        },
    )()
    window = type(
        "Window",
        (),
        {
            "name": "pm-operator",
            "pane_dead": False,
            "pane_id": "%1",
        },
    )()

    status_line = "  ⏵⏵ bypass permissions on (shift+tab to cycle) " + ("." * 260)

    class FakeTmux:
        def capture_pane(self, pane_id: str, lines: int = 15) -> str:
            assert pane_id == "%1"
            assert lines == 15
            return "\n".join(
                [
                    "Claude Code v2.1.96",
                    "some prior transcript output",
                    "❯",
                    status_line,
                ]
            )

    router = CockpitRouter(config_path)
    router.tmux = FakeTmux()  # type: ignore[assignment]

    assert router._session_state("operator", [launch], [window], [], 0) == "ready"


def test_worker_session_state_stays_working_when_codex_shows_interrupt_banner(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm'}\"\n"
    )

    launch = type(
        "Launch",
        (),
        {
            "window_name": "worker-demo",
            "session": type(
                "Session",
                (),
                {
                    "name": "worker_demo",
                    "role": "worker",
                    "project": "demo",
                    "provider": type("P", (), {"value": "codex"})(),
                },
            )(),
        },
    )()
    window = type(
        "Window",
        (),
        {
            "name": "worker-demo",
            "pane_dead": False,
            "pane_id": "%2",
        },
    )()

    class FakeTmux:
        def capture_pane(self, pane_id: str, lines: int = 15) -> str:
            assert pane_id == "%2"
            assert lines == 15
            return "\n".join(
                [
                    "OpenAI Codex",
                    "• Working (12s • esc to interrupt)",
                    "› Implement {feature}",
                ]
            )

    router = CockpitRouter(config_path)
    router.tmux = FakeTmux()  # type: ignore[assignment]

    assert router._session_state("worker_demo", [launch], [window], [], 0).endswith("working")


def test_worker_session_state_reuses_recent_heartbeat_snapshot(
    tmp_path: Path,
) -> None:
    from datetime import UTC, datetime

    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm'}\"\n"
    )
    snapshot_path = tmp_path / "worker-snapshot.txt"
    snapshot_path.write_text(
        "\n".join(
            [
                "OpenAI Codex",
                "• Working (12s • esc to interrupt)",
                "› Implement {feature}",
            ]
        )
    )

    launch = type(
        "Launch",
        (),
        {
            "window_name": "worker-demo",
            "session": type(
                "Session",
                (),
                {
                    "name": "worker_demo",
                    "role": "worker",
                    "project": "demo",
                    "provider": type("P", (), {"value": "codex"})(),
                },
            )(),
        },
    )()
    window = type(
        "Window",
        (),
        {
            "name": "worker-demo",
            "pane_dead": False,
            "pane_id": "%9",
        },
    )()

    class FakeStore:
        def latest_heartbeat(self, session_name: str):
            assert session_name == "worker_demo"
            return type(
                "Heartbeat",
                (),
                {
                    "snapshot_path": str(snapshot_path),
                    "created_at": datetime.now(UTC).isoformat(),
                },
            )()

    class FakeSupervisor:
        store = FakeStore()

    class FakeTmux:
        def capture_pane(self, pane_id: str, lines: int = 15) -> str:
            raise AssertionError("capture_pane should not run when heartbeat snapshot is fresh")

    router = CockpitRouter(config_path)
    router.tmux = FakeTmux()  # type: ignore[assignment]
    router._load_supervisor = lambda fresh=False: FakeSupervisor()  # type: ignore[method-assign]

    assert router._session_state("worker_demo", [launch], [window], [], 0).endswith("working")
