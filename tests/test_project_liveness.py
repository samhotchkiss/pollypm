from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pollypm.project_liveness import (
    is_real_operator_project,
    project_key_looks_synthetic,
    project_path_looks_synthetic,
    real_operator_project_keys,
)


def test_project_key_looks_synthetic_for_known_test_patterns() -> None:
    assert project_key_looks_synthetic("pm_test_01wave_1779715196")
    assert project_key_looks_synthetic("pm test 01wave 1779715196")
    assert project_key_looks_synthetic("queuestorm_1779777233")
    assert project_key_looks_synthetic("myproj")
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
    assert not is_real_operator_project(
        "pm_test_01wave_1779715196",
        SimpleNamespace(tracked=True, path="/Users/sam/dev/pm_test_01wave_1779715196"),
    )
    assert not is_real_operator_project(
        "scratch",
        SimpleNamespace(tracked=True, path="/private/tmp/scratch"),
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
