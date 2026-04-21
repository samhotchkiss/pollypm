from __future__ import annotations

from types import SimpleNamespace

from pollypm.cockpit_inbox import (
    build_inbox_thread_rows,
    inbox_thread_left_action,
    inbox_thread_right_action,
)


def _task(task_id: str, title: str):
    return SimpleNamespace(task_id=task_id, title=title)


def _reply(actor: str, text: str):
    return SimpleNamespace(actor=actor, text=text)


def test_build_inbox_thread_rows_only_includes_replies_when_expanded() -> None:
    task = _task("demo/1", "Feedback on task #5")
    replies = [_reply("user", "Got it"), _reply("polly", "Approved")]

    collapsed = build_inbox_thread_rows(
        [task],
        {"demo/1": replies},
        set(),
    )
    assert [row.key for row in collapsed] == ["task:demo/1"]
    assert collapsed[0].reply_count == 2
    assert collapsed[0].expanded is False

    expanded = build_inbox_thread_rows(
        [task],
        {"demo/1": replies},
        {"demo/1"},
    )
    assert [row.key for row in expanded] == [
        "task:demo/1",
        "reply:demo/1:0",
        "reply:demo/1:1",
    ]
    assert expanded[0].expanded is True
    assert expanded[1].is_reply is True
    assert expanded[2].is_reply is True


def test_inbox_thread_left_right_actions_follow_tree_navigation() -> None:
    task = _task("demo/1", "Feedback on task #5")
    replies = [_reply("user", "Got it"), _reply("polly", "Approved")]

    collapsed = build_inbox_thread_rows(
        [task],
        {"demo/1": replies},
        set(),
    )
    assert inbox_thread_right_action(collapsed, 0) == ("expand", None)
    assert inbox_thread_left_action(collapsed, 0) == ("noop", None)

    expanded = build_inbox_thread_rows(
        [task],
        {"demo/1": replies},
        {"demo/1"},
    )
    assert inbox_thread_right_action(expanded, 0) == ("select_child", 1)
    assert inbox_thread_left_action(expanded, 0) == ("collapse", None)
    assert inbox_thread_left_action(expanded, 1) == ("select_parent", 0)
    assert inbox_thread_right_action(expanded, 1) == ("noop", None)
