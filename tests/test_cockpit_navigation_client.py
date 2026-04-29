from __future__ import annotations

import ast
import json
import multiprocessing
from pathlib import Path

from pollypm.cockpit_navigation_client import (
    CockpitNavigationClient,
    CockpitNavigationClientOutcome,
    CockpitNavigationClientRequest,
    DirectCockpitNavigationAdapter,
    FileCockpitNavigationQueue,
    StandaloneCockpitNavigationAdapter,
    cockpit_navigation_queue_path,
    file_navigation_client,
)


REPO_ROOT = Path(__file__).resolve().parent.parent


def _submit_file_queue_request(
    queue_path: str,
    client_id: str,
    key: str,
    sequence_start: int,
) -> None:
    queue = FileCockpitNavigationQueue(Path(queue_path), max_entries=50)
    client = CockpitNavigationClient(
        queue,
        client_id=client_id,
        sequence_start=sequence_start,
    )
    client.navigate(key)


def test_direct_adapter_receives_navigation_requests() -> None:
    received: list[CockpitNavigationClientRequest] = []

    def handle(request: CockpitNavigationClientRequest) -> dict[str, object]:
        received.append(request)
        return {"accepted": True, "selected_key": request.selected_key}

    client = CockpitNavigationClient(
        DirectCockpitNavigationAdapter(handle),
        client_id="pane-a",
    )

    result = client.jump_to_project("demo", view="issues")

    assert result.outcome == CockpitNavigationClientOutcome.SUBMITTED
    assert result.ok is True
    assert result.handled is True
    assert result.details["owner_result"] == {
        "accepted": True,
        "selected_key": "project:demo:issues",
    }
    assert [request.selected_key for request in received] == [
        "project:demo:issues",
    ]
    assert received[0].navigation.origin == "right_pane"


def test_standalone_fallback_records_and_returns_unsupported_result() -> None:
    adapter = StandaloneCockpitNavigationAdapter()
    client = CockpitNavigationClient(adapter)

    result = client.jump_to_inbox("demo")

    assert result.outcome == CockpitNavigationClientOutcome.UNSUPPORTED
    assert result.ok is False
    assert result.handled is False
    assert "unsupported" in result.message
    assert result.request.selected_key == "inbox:demo"
    assert adapter.history == [result.request]
    assert client.history == [result]


def test_request_sequences_and_ids_are_monotonic() -> None:
    client = CockpitNavigationClient(client_id="pane")

    results = [
        client.navigate("inbox"),
        client.navigate("activity"),
        client.jump_to_project("demo"),
        client.jump_to_project("demo", view="issues", task_number=7),
    ]

    assert [result.request.sequence for result in results] == [1, 2, 3, 4]
    assert [result.request.request_id for result in results] == [
        "pane-00000001",
        "pane-00000002",
        "pane-00000003",
        "pane-00000004",
    ]


def test_right_pane_jump_helpers_represent_existing_route_shapes() -> None:
    received: list[CockpitNavigationClientRequest] = []
    client = CockpitNavigationClient(
        DirectCockpitNavigationAdapter(lambda request: received.append(request)),
        client_id="pane",
    )

    client.jump_to_inbox("demo")
    client.jump_to_activity("demo")
    client.jump_to_project("demo")
    client.jump_to_project("demo", view=None)
    client.jump_to_project("demo", view="issues")
    client.jump_to_project("demo", view="issues", task_number=7)

    assert [request.selected_key for request in received] == [
        "inbox:demo",
        "activity:demo",
        "project:demo:dashboard",
        "project:demo",
        "project:demo:issues",
        "project:demo:issues:task:7",
    ]
    assert [request.project_key for request in received] == [
        "demo",
        "demo",
        "demo",
        "demo",
        "demo",
        "demo",
    ]
    assert received[-1].task_id == "demo/7"
    assert received[0].payload["action"] == "jump_to_inbox"
    assert received[1].payload["action"] == "jump_to_activity"
    assert received[2].payload["action"] == "jump_to_project"


def test_file_queue_records_pending_requests(tmp_path: Path) -> None:
    queue_path = tmp_path / "cockpit_navigation_queue.json"
    queue = FileCockpitNavigationQueue(queue_path)
    client = CockpitNavigationClient(queue, client_id="pane")

    result = client.jump_to_activity("demo", payload={"source": "project-dashboard"})

    assert result.outcome == CockpitNavigationClientOutcome.QUEUED
    assert result.ok is True
    pending = queue.pending()
    assert [request.selected_key for request in pending] == ["activity:demo"]
    assert pending[0].payload == {
        "action": "jump_to_activity",
        "source": "project-dashboard",
    }

    raw = json.loads(queue_path.read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert raw["last_sequence"] == 1
    assert raw["requests"][0]["request_id"] == "pane-00000001"

    drained = queue.drain()
    assert [request.selected_key for request in drained] == ["activity:demo"]
    assert queue.pending() == ()
    assert json.loads(queue_path.read_text(encoding="utf-8"))[
        "last_drained_sequence"
    ] == 1


def test_file_queue_drain_does_not_clear_later_submits(tmp_path: Path) -> None:
    queue_path = tmp_path / "cockpit_navigation_queue.json"
    queue = FileCockpitNavigationQueue(queue_path)
    pane_a = CockpitNavigationClient(queue, client_id="pane-a")
    pane_b = CockpitNavigationClient(queue, client_id="pane-b")

    pane_a.navigate("inbox:demo")
    drained = queue.drain()
    pane_b.navigate("activity:demo")

    assert [request.selected_key for request in drained] == ["inbox:demo"]
    assert [request.selected_key for request in queue.pending()] == ["activity:demo"]


def test_file_queue_serializes_concurrent_submitters(tmp_path: Path) -> None:
    queue_path = tmp_path / "cockpit_navigation_queue.json"
    processes: list[multiprocessing.Process] = []
    context = multiprocessing.get_context("spawn")

    for index in range(16):
        process = context.Process(
            target=_submit_file_queue_request,
            args=(
                str(queue_path),
                f"pane-{index}",
                f"project:demo:{index:02d}",
                index,
            ),
        )
        process.start()
        processes.append(process)

    for process in processes:
        process.join(timeout=10)
    for process in processes:
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)

    exitcodes = [process.exitcode for process in processes]
    assert exitcodes == [0] * len(processes)

    pending = FileCockpitNavigationQueue(queue_path).pending()
    assert sorted(request.selected_key for request in pending) == [
        f"project:demo:{index:02d}" for index in range(16)
    ]


def test_file_queue_caps_oldest_entries(tmp_path: Path) -> None:
    queue_path = tmp_path / "cockpit_navigation_queue.json"
    queue = FileCockpitNavigationQueue(queue_path, max_entries=3)
    client = CockpitNavigationClient(queue, client_id="pane")

    for index in range(5):
        result = client.navigate(f"project:demo:{index}")

    assert result.details["dropped_count"] == 1
    assert [request.selected_key for request in queue.pending()] == [
        "project:demo:2",
        "project:demo:3",
        "project:demo:4",
    ]
    assert json.loads(queue_path.read_text(encoding="utf-8"))["dropped_count"] == 2


def test_file_navigation_client_uses_project_local_queue(tmp_path: Path) -> None:
    config_path = tmp_path / "pollypm.toml"
    base_dir = tmp_path / ".pollypm"
    config_path.write_text(
        "[project]\n"
        "name = \"PollyPM\"\n"
        "tmux_session = \"pollypm\"\n"
        f"base_dir = \"{base_dir}\"\n",
        encoding="utf-8",
    )

    client = file_navigation_client(config_path, client_id="pane")
    result = client.jump_to_inbox("demo")

    queue_path = cockpit_navigation_queue_path(config_path)
    assert result.outcome == CockpitNavigationClientOutcome.QUEUED
    assert queue_path == base_dir / "cockpit_navigation_queue.json"
    assert FileCockpitNavigationQueue(queue_path).pending()[0].selected_key == "inbox:demo"


def test_adapter_failures_are_typed_results() -> None:
    def explode(_request: CockpitNavigationClientRequest) -> None:
        raise RuntimeError("owner unavailable")

    client = CockpitNavigationClient(DirectCockpitNavigationAdapter(explode))

    result = client.jump_to_activity("demo")

    assert result.outcome == CockpitNavigationClientOutcome.FAILED
    assert result.ok is False
    assert result.handled is False
    assert result.error == "owner unavailable"
    assert "owner unavailable" in result.message
    assert client.history == [result]


def test_navigation_client_keeps_imports_light() -> None:
    path = REPO_ROOT / "src" / "pollypm" / "cockpit_navigation_client.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)

    forbidden_roots = {
        "pollypm.cockpit_ui",
        "pollypm.cockpit_rail",
        "pollypm.tmux",
        "textual",
    }
    offenders = sorted(
        name
        for name in imported
        for root in forbidden_roots
        if name == root or name.startswith(f"{root}.")
    )
    assert offenders == []
