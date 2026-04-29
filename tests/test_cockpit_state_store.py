from __future__ import annotations

import json
from pathlib import Path

import pytest

from pollypm.cockpit_state_store import (
    DEFAULT_RAIL_WIDTH,
    DEFAULT_SELECTED_KEY,
    LIFECYCLE_TO_RIGHT_PANE_STATE,
    MAX_RAIL_WIDTH,
    MIN_RAIL_WIDTH,
    RIGHT_PANE_STATE_TO_LIFECYCLE,
    CockpitStateStore,
    lifecycle_to_right_pane_state,
    right_pane_state_to_lifecycle,
)
from pollypm.cockpit_contracts import RightPaneLifecycleState


def _store(tmp_path: Path) -> CockpitStateStore:
    return CockpitStateStore(tmp_path / "cockpit_state.json")


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_missing_file_returns_safe_defaults(tmp_path: Path) -> None:
    store = _store(tmp_path)

    assert store.raw_state() == {}
    assert store.selected_key() == DEFAULT_SELECTED_KEY
    assert store.active_request_id() is None
    assert store.right_pane_state() == "idle"
    assert store.right_pane_id() is None
    assert store.mounted_identity() is None
    assert store.rail_width() == DEFAULT_RAIL_WIDTH
    assert store.pinned_projects() == []
    assert store.should_show_palette_tip()

    snapshot = store.snapshot()
    assert snapshot.selected_key == DEFAULT_SELECTED_KEY
    assert snapshot.right_pane_state == "idle"
    assert snapshot.pinned_projects == ()


def test_corrupt_file_returns_defaults_and_next_write_replaces_it(tmp_path: Path) -> None:
    path = tmp_path / "cockpit_state.json"
    path.write_text("{not valid json", encoding="utf-8")
    store = CockpitStateStore(path)

    assert store.selected_key() == DEFAULT_SELECTED_KEY
    assert store.right_pane_state() == "idle"

    store.set_selected_key("inbox")

    assert _read_json(path) == {"selected": "inbox"}


def test_writes_preserve_unrelated_state_keys(tmp_path: Path) -> None:
    path = tmp_path / "cockpit_state.json"
    path.write_text(
        json.dumps({"selected": "polly", "custom_plugin_key": {"count": 2}}),
        encoding="utf-8",
    )
    store = CockpitStateStore(path)

    store.set_active_request_id("req-1")
    store.mark_palette_tip_seen()

    assert _read_json(path) == {
        "active_request_id": "req-1",
        "custom_plugin_key": {"count": 2},
        "palette_tip_seen": True,
        "selected": "polly",
    }


def test_selection_intent_is_separate_from_mounted_truth(tmp_path: Path) -> None:
    store = _store(tmp_path)
    mounted = {
        "rail_key": "polly",
        "session_name": "operator",
        "role": "operator-pm",
        "expected_window_name": "pm-operator",
        "right_pane_id": "%7",
        "window_index": 2,
        "mounted_at": "2026-04-29T12:00:00+00:00",
    }

    store.set_selected_key("project:demo:dashboard")
    store.set_right_pane_id("%7")
    store.set_mounted_identity(mounted)
    store.mark_right_pane_live_agent()

    assert store.selected_key() == "project:demo:dashboard"
    assert store.right_pane_state() == "live_agent"
    assert store.mounted_identity() == mounted

    store.clear_mounted_and_right_pane_state()

    assert store.selected_key() == "project:demo:dashboard"
    assert store.right_pane_state() == "idle"
    assert store.right_pane_id() is None
    assert store.mounted_identity() is None


def test_defaults_reject_invalid_persisted_values(tmp_path: Path) -> None:
    path = tmp_path / "cockpit_state.json"
    path.write_text(
        json.dumps(
            {
                "selected": "",
                "active_request_id": 42,
                "right_pane_state": "mounted",
                "right_pane_id": "",
                "mounted_identity": [],
                "rail_width": 999,
                "palette_tip_seen": "yes",
                "pinned_projects": [None, "", "alpha", "alpha", "beta"],
            }
        ),
        encoding="utf-8",
    )
    store = CockpitStateStore(path)

    assert store.selected_key() == DEFAULT_SELECTED_KEY
    assert store.active_request_id() is None
    assert store.right_pane_state() == "idle"
    assert store.right_pane_id() is None
    assert store.mounted_identity() is None
    assert store.rail_width() == DEFAULT_RAIL_WIDTH
    assert store.should_show_palette_tip()
    assert store.pinned_projects() == ["alpha", "beta"]


def test_right_pane_lifecycle_states_and_active_request_id(tmp_path: Path) -> None:
    store = _store(tmp_path)

    store.mark_right_pane_loading("req-123")
    assert store.right_pane_state() == "loading"
    assert store.active_request_id() == "req-123"

    store.mark_right_pane_static()
    assert store.right_pane_state() == "static"
    assert store.active_request_id() is None

    store.mark_right_pane_live_agent()
    assert store.right_pane_state() == "live_agent"

    store.mark_right_pane_error("failed to mount")
    assert store.right_pane_state() == "error"
    assert _read_json(store.path)["right_pane_error"] == "failed to mount"

    store.mark_right_pane_idle()
    assert store.right_pane_state() == "idle"
    assert "right_pane_error" not in _read_json(store.path)


def test_persisted_right_pane_state_maps_to_public_lifecycle_contract() -> None:
    expected = {
        "idle": RightPaneLifecycleState.UNMOUNTED,
        "loading": RightPaneLifecycleState.INITIALIZING,
        "static": RightPaneLifecycleState.STATIC_VIEW,
        "live_agent": RightPaneLifecycleState.LIVE_SESSION,
        "error": RightPaneLifecycleState.ERROR,
    }

    assert RIGHT_PANE_STATE_TO_LIFECYCLE == expected
    assert LIFECYCLE_TO_RIGHT_PANE_STATE == {
        lifecycle: state for state, lifecycle in expected.items()
    }

    for state, lifecycle in expected.items():
        assert right_pane_state_to_lifecycle(state) is lifecycle
        assert lifecycle_to_right_pane_state(lifecycle) == state

    with pytest.raises(ValueError):
        lifecycle_to_right_pane_state(RightPaneLifecycleState.STALE)


def test_active_request_id_can_be_set_and_cleared(tmp_path: Path) -> None:
    store = _store(tmp_path)

    store.set_active_request_id("req-a")
    assert store.active_request_id() == "req-a"

    store.set_active_request_id(None)
    assert store.active_request_id() is None
    assert "active_request_id" not in _read_json(store.path)


def test_right_pane_id_can_be_set_and_cleared(tmp_path: Path) -> None:
    store = _store(tmp_path)

    store.set_right_pane_id("%11")
    assert store.right_pane_id() == "%11"

    store.set_right_pane_id(None)
    assert store.right_pane_id() is None
    assert "right_pane_id" not in _read_json(store.path)


def test_rail_width_bounds_and_invalid_type(tmp_path: Path) -> None:
    store = _store(tmp_path)

    store.set_rail_width(44)
    assert store.rail_width() == 44

    store.set_rail_width(MIN_RAIL_WIDTH - 5)
    assert store.rail_width() == MIN_RAIL_WIDTH

    store.set_rail_width(MAX_RAIL_WIDTH + 5)
    assert store.rail_width() == MAX_RAIL_WIDTH

    with pytest.raises(TypeError):
        store.set_rail_width("wide")  # type: ignore[arg-type]


def test_pins_ordering_dedupe_and_toggle(tmp_path: Path) -> None:
    store = _store(tmp_path)

    store.set_pinned_projects(["alpha", "beta", "alpha", "", "gamma"])
    assert store.pinned_projects() == ["alpha", "beta", "gamma"]

    store.pin_project("beta")
    assert store.pinned_projects() == ["beta", "alpha", "gamma"]

    assert store.toggle_pinned_project("delta") is True
    assert store.pinned_projects() == ["delta", "beta", "alpha", "gamma"]

    assert store.toggle_pinned_project("beta") is False
    assert store.pinned_projects() == ["delta", "alpha", "gamma"]


def test_clearing_mounted_state_removes_legacy_and_typed_payloads(tmp_path: Path) -> None:
    path = tmp_path / "cockpit_state.json"
    path.write_text(
        json.dumps(
            {
                "selected": "workers",
                "mounted_session": "operator",
                "mounted_identity": {"session_name": "operator"},
                "right_pane_id": "%5",
                "right_pane_state": "live_agent",
                "active_request_id": "req-old",
            }
        ),
        encoding="utf-8",
    )
    store = CockpitStateStore(path)

    store.clear_mounted_and_right_pane_state()

    assert _read_json(path) == {"right_pane_state": "idle", "selected": "workers"}
