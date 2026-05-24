"""Regression guards for the web_api/work_service test sandbox (#2177)."""

from __future__ import annotations

import os
from pathlib import Path


def test_web_api_tests_run_with_sandboxed_pollypm_home() -> None:
    import pollypm.config as config_mod
    import pollypm.web_api.token as token_mod

    home = Path(os.environ["HOME"])
    pollypm_home = home / ".pollypm"

    assert home.name == "home"
    assert pollypm_home.is_dir()
    assert Path(config_mod.GLOBAL_CONFIG_DIR) == pollypm_home
    assert Path(config_mod.DEFAULT_CONFIG_PATH) == pollypm_home / "pollypm.toml"
    assert Path(token_mod.DEFAULT_TOKEN_PATH) == pollypm_home / "api-token"


def test_web_api_work_service_writes_land_in_per_test_pg_schema(
    api_config,
    api_pg_pool,
) -> None:
    from pollypm.work.factory import create_work_service

    with create_work_service(config=api_config, project_key="myproj") as svc:
        task = svc.create(
            title="schema isolation guard",
            type="task",
            project="myproj",
            flow_template="default",
            roles={"worker": "agent-1"},
            created_by="pytest",
        )

    with api_pg_pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT title
            FROM work_tasks
            WHERE project = %s AND task_number = %s
            """,
            (task.project, task.task_number),
        )
        row = cur.fetchone()

    assert row == ("schema isolation guard",)
