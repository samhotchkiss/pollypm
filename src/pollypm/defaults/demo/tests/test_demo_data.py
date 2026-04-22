from demo_data import DEMO_TASKS, demo_task_titles


def test_demo_data_exposes_three_sample_tasks() -> None:
    assert len(DEMO_TASKS) == 3
    assert demo_task_titles()[0] == "Fix the queue estimate bug"
