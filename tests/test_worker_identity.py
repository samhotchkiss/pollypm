from pathlib import Path

from pollypm.cockpit_worker_identity import (
    load_worker_color_overrides,
    worker_identity,
)


def test_worker_identity_is_stable_and_role_aware() -> None:
    first = worker_identity("task-demo-42")
    second = worker_identity("task-demo-42")

    assert first == second
    assert first.avatar == "W42"
    assert worker_identity("polly").avatar == "P"
    assert worker_identity("russell").avatar == "R"
    assert first.color.startswith("#")
    assert len(first.color) == 7


def test_worker_identity_honors_config_overrides(tmp_path: Path) -> None:
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        "[worker_colors]\n"
        'polly = "#7FDBFF"\n'
        'russell = "#FFDC00"\n'
        'worker = "#00AA88"\n'
        'invalid = "blue"\n'
    )

    overrides = load_worker_color_overrides(config_path)

    assert overrides == {
        "polly": "#7FDBFF",
        "russell": "#FFDC00",
        "worker": "#00AA88",
    }
    assert worker_identity("operator", color_overrides=overrides).color == "#7FDBFF"
    assert worker_identity("russell-review", color_overrides=overrides).color == "#FFDC00"
    assert worker_identity("task-demo-7", color_overrides=overrides).color == "#00AA88"
