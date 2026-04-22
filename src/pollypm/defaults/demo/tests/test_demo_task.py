from pathlib import Path


def test_task_doc_points_at_the_seeded_bug_and_tests() -> None:
    text = Path("TASK.md").read_text(encoding="utf-8")
    assert "demo_app.py" in text
    assert "tests/test_demo_app.py" in text
    assert "30-minute block" in text
