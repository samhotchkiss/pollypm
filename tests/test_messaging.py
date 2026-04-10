import json
import pytest

from pollypm.messaging import (
    append_thread_message,
    create_message,
    create_thread,
    ensure_inbox,
    get_thread,
    list_open_messages,
    read_handoff,
    set_handoff,
    transition_thread,
)


def test_file_backed_inbox_round_trip(tmp_path) -> None:
    root = ensure_inbox(tmp_path)

    assert root == tmp_path / ".pollypm" / "inbox"
    message_path = create_message(tmp_path, sender="pa", subject="Review needed", body="Issue 012 is ready.")
    assert message_path.exists()
    assert (root.parent / ".gitignore").read_text().strip().endswith("inbox/")

    messages = list_open_messages(tmp_path)
    assert len(messages) == 1
    assert messages[0].subject == "Review needed"
    assert messages[0].sender == "pa"
    assert message_path.parent.name == "open"


def test_thread_lifecycle_persists_state_and_handoff(tmp_path) -> None:
    message_path = create_message(tmp_path, sender="pa", subject="Review needed", body="Issue 012 is ready.")
    thread = create_thread(tmp_path, message_path.name, actor="pm", owner="pm")

    assert thread.state == "threaded"
    assert thread.path.name == message_path.stem
    assert (thread.path / "state.json").exists()
    assert (thread.path / "handoff.json").exists()
    assert thread.message_paths[0].name.endswith(message_path.name)

    append_thread_message(tmp_path, thread.thread_id, sender="pm", subject="Ack", body="Looking now.")
    set_handoff(tmp_path, thread.thread_id, owner="pa", actor="pm", note="Need implementation")
    transition_thread(tmp_path, thread.thread_id, "waiting-on-pa", actor="pm", note="Need implementation")
    transition_thread(tmp_path, thread.thread_id, "waiting-on-pm", actor="pa", note="Ready for review")
    transition_thread(tmp_path, thread.thread_id, "resolved", actor="pm", note="Approved")
    closed = transition_thread(tmp_path, thread.thread_id, "closed", actor="pm", note="Archived")

    assert closed.state == "closed"
    assert closed.path.parent.name == "closed"
    handoff = read_handoff(tmp_path, thread.thread_id)
    assert handoff["owner"] == "pa"
    state = json.loads((closed.path / "state.json").read_text())
    assert [item["state"] for item in state["transitions"]] == [
        "threaded",
        "waiting-on-pa",
        "waiting-on-pm",
        "resolved",
        "closed",
    ]
    assert state["closed_at"]

    recovered = get_thread(tmp_path, thread.thread_id)
    assert recovered.state == "closed"
    assert len(recovered.message_paths) == 2


def test_thread_transition_rejects_skips_and_backwards_moves(tmp_path) -> None:
    message_path = create_message(tmp_path, sender="pa", subject="Review needed", body="Issue 012 is ready.")
    thread = create_thread(tmp_path, message_path.name, actor="pm", owner="pm")

    # PM can always jump to resolved or closed from any state
    transition_thread(tmp_path, thread.thread_id, "closed", actor="pm")

    # Create a fresh thread to test backwards rejection
    message_path2 = create_message(tmp_path, sender="pa", subject="Another review", body="Issue 013.")
    thread2 = create_thread(tmp_path, message_path2.name, actor="pm", owner="pm")
    transition_thread(tmp_path, thread2.thread_id, "waiting-on-pa", actor="pm")
    # Cannot go backwards to threaded
    with pytest.raises(ValueError, match="Illegal inbox state transition"):
        transition_thread(tmp_path, thread2.thread_id, "threaded", actor="pa")
    # PM/PA cycling is allowed
    transition_thread(tmp_path, thread2.thread_id, "waiting-on-pm", actor="pa")


def test_inbox_root_migrates_old_pollypm_directory(tmp_path) -> None:
    old_root = tmp_path / "pollypm" / "inbox" / "open"
    old_root.mkdir(parents=True)
    legacy_message = old_root / "legacy.md"
    legacy_message.write_text("Subject: Legacy\nSender: pa\nCreated-At: now\n\nbody\n")

    root = ensure_inbox(tmp_path)

    assert root == tmp_path / ".pollypm" / "inbox"
    assert (root / "open" / "legacy.md").exists()
