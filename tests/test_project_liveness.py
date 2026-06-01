from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from pollypm.project_liveness import (
    coerce_utc_datetime,
    is_real_operator_project,
    project_key_looks_synthetic,
    project_path_looks_synthetic,
    real_operator_project_items,
    real_operator_project_keys,
    recent_real_work_project_keys,
    task_is_recent_done_work,
)


def test_project_key_looks_synthetic_for_known_test_patterns() -> None:
    assert project_key_looks_synthetic("pm_test_01wave_1779715196")
    assert project_key_looks_synthetic("pm test 01wave 1779715196")
    assert project_key_looks_synthetic("load-wave-1779715196")
    assert project_key_looks_synthetic("pr2498-drift-17797201")
    assert project_key_looks_synthetic("queuestorm_1779777233")
    assert project_key_looks_synthetic("myproj")
    assert project_key_looks_synthetic("smoketest")
    assert project_key_looks_synthetic("inbox")


def test_project_path_looks_synthetic_for_private_tmp_roots() -> None:
    assert project_path_looks_synthetic(Path("/private/tmp/pm_test_01wave_1779715196"))
    assert not project_path_looks_synthetic(Path("/Users/sam/dev/savethenovel"))


def test_is_real_operator_project_requires_tracked_non_synthetic_project() -> None:
    assert is_real_operator_project(
        "savethenovel",
        SimpleNamespace(tracked=True, path="/Users/sam/dev/savethenovel"),
    )
    assert not is_real_operator_project(
        "savethenovel",
        SimpleNamespace(tracked=False, path="/Users/sam/dev/savethenovel"),
    )
    assert is_real_operator_project(
        "savethenovel",
        SimpleNamespace(tracked=False, path="/Users/sam/dev/savethenovel"),
        default_tracked=False,
    )
    assert not is_real_operator_project(
        "pm_test_01wave_1779715196",
        SimpleNamespace(tracked=True, path="/Users/sam/dev/pm_test_01wave_1779715196"),
    )
    assert not is_real_operator_project(
        "scratch",
        SimpleNamespace(tracked=True, path="/private/tmp/pm_test_01wave_1779715196"),
    )


def test_real_operator_project_keys_filters_config_map() -> None:
    projects = {
        "savethenovel": SimpleNamespace(
            tracked=True,
            path="/Users/sam/dev/savethenovel",
        ),
        "myproj": SimpleNamespace(
            tracked=True,
            path="/Users/sam/dev/myproj",
        ),
        "polly_remote": SimpleNamespace(
            tracked=False,
            path="/Users/sam/dev/polly_remote",
        ),
    }

    assert real_operator_project_keys(projects) == frozenset({"savethenovel"})


def test_real_operator_project_items_falls_back_when_only_synthetic() -> None:
    projects = {
        "myproj": SimpleNamespace(tracked=True, path="/Users/sam/dev/myproj"),
        "pm_test_01wave_1779715196": SimpleNamespace(
            tracked=True,
            path="/Users/sam/dev/pm_test_01wave_1779715196",
        ),
    }

    assert [key for key, _project in real_operator_project_items(projects)] == [
        "myproj",
        "pm_test_01wave_1779715196",
    ]
    assert real_operator_project_items(projects, fallback_to_all=False) == ()


def test_recent_real_work_project_keys_use_recent_done_real_projects() -> None:
    now = datetime(2026, 6, 1, tzinfo=UTC)
    projects = {
        "active": SimpleNamespace(tracked=True, path="/Users/sam/dev/active"),
        "dormant": SimpleNamespace(tracked=True, path="/Users/sam/dev/dormant"),
        "queued_only": SimpleNamespace(tracked=True, path="/Users/sam/dev/queued"),
        "pm_test_01wave_1779715196": SimpleNamespace(
            tracked=True,
            path="/private/tmp/pm_test_01wave_1779715196",
        ),
    }
    tasks = {
        "active": [
            SimpleNamespace(
                work_status="done",
                updated_at=now - timedelta(days=1),
            ),
        ],
        "dormant": [
            SimpleNamespace(
                work_status="done",
                updated_at=now - timedelta(days=14),
            ),
        ],
        "queued_only": [
            SimpleNamespace(
                work_status="queued",
                updated_at=now,
            ),
        ],
        "pm_test_01wave_1779715196": [
            SimpleNamespace(
                work_status="done",
                updated_at=now - timedelta(days=1),
            ),
        ],
    }

    assert recent_real_work_project_keys(
        projects,
        lambda key: tasks[key],
        now=now,
    ) == frozenset({"active"})


def test_task_is_recent_done_work_treats_missing_timestamp_as_live() -> None:
    cutoff = datetime(2026, 5, 25, tzinfo=UTC)
    assert task_is_recent_done_work(
        SimpleNamespace(work_status="done", updated_at=None, created_at=None),
        cutoff=cutoff,
    )
    assert not task_is_recent_done_work(
        SimpleNamespace(work_status="queued", updated_at=None, created_at=None),
        cutoff=cutoff,
    )


def test_coerce_utc_datetime_handles_z_suffix_and_naive_values() -> None:
    assert coerce_utc_datetime("2026-06-01T12:00:00Z") == datetime(
        2026,
        6,
        1,
        12,
        0,
        tzinfo=UTC,
    )
    assert coerce_utc_datetime(datetime(2026, 6, 1, 12, 0)).tzinfo is UTC
