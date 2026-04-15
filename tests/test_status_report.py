from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path


def _load_status_report_module():
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "status_report.py"
    spec = importlib.util.spec_from_file_location("status_report", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_render_report_uses_required_status_order():
    module = _load_status_report_module()
    tasks = [
        {
            "task_id": "proj/2",
            "title": "Review task",
            "priority": "high",
            "assignee": "reviewer",
            "current_node_id": "review",
            "work_status": "review",
        },
        {
            "task_id": "proj/1",
            "title": "Queued task",
            "priority": "normal",
            "assignee": None,
            "current_node_id": None,
            "work_status": "queued",
        },
        {
            "task_id": "proj/3",
            "title": "Blocked task",
            "priority": "low",
            "assignee": "worker",
            "current_node_id": "implement",
            "work_status": "blocked",
        },
    ]

    report = module.render_report(tasks, generated_at=datetime(2026, 4, 14, tzinfo=UTC))

    assert "| Draft | 0 |" in report
    assert "| Queued | 1 |" in report
    assert "| Review | 1 |" in report
    assert "| Blocked | 1 |" in report
    assert "- `proj/1` | Queued task | priority: `normal` | assignee: `-` | node: `-`" in report

    headings = [
        "## Draft (0)",
        "## Queued (1)",
        "## In Progress (0)",
        "## Review (1)",
        "## Done (0)",
        "## Cancelled (0)",
        "## On Hold (0)",
        "## Blocked (1)",
    ]
    positions = [report.index(heading) for heading in headings]
    assert positions == sorted(positions)


def test_main_writes_report(tmp_path, monkeypatch, capsys):
    module = _load_status_report_module()
    output_path = tmp_path / "status-report.md"

    monkeypatch.setattr(
        module,
        "fetch_tasks",
        lambda pm_command="pm": [
            {
                "task_id": "proj/9",
                "title": "Done task",
                "priority": "critical",
                "assignee": "worker_pollypm",
                "current_node_id": "done",
                "work_status": "done",
            }
        ],
    )

    exit_code = module.main(["--output", str(output_path)])

    assert exit_code == 0
    assert output_path.exists()
    content = output_path.read_text()
    assert "# Project Status Report" in content
    assert "## Done (1)" in content
    assert "`proj/9`" in content

    captured = capsys.readouterr()
    assert f"Wrote {output_path}" in captured.out
