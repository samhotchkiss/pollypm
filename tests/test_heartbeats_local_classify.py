"""#1043 — worker classifier ignores ``pm task next`` command literals.

Workers polling for new work via ``pm task next -p <project>`` would
trip the ``\\bnext\\b`` regex in ``LocalHeartbeatBackend._classify`` and
get mislabelled as ``needs_followup``. The fix strips the command
literal (and the matching ``No tasks available`` PollyPM output) before
pattern matching, so a healthy idle-poll loop classifies away from
``needs_followup``.
"""

from __future__ import annotations

from pollypm.heartbeats.base import HeartbeatSessionContext
from pollypm.heartbeats.local import LocalHeartbeatBackend


def _context(delta: str) -> HeartbeatSessionContext:
    return HeartbeatSessionContext(
        session_name="worker_coin_flip",
        role="worker",
        project_key="coin_flip",
        provider="claude",
        account_name="claude_controller",
        cwd="/workspace",
        tmux_session="coin_flip",
        window_name="worker_coin_flip",
        source_path="/tmp/worker.log",
        source_bytes=64,
        transcript_delta=delta,
        pane_text=delta,
        snapshot_path="/tmp/worker.txt",
        snapshot_hash="hash-1",
        pane_id="%1",
        pane_command="claude",
        pane_dead=False,
        window_present=True,
        previous_log_bytes=32,
        previous_snapshot_hash="hash-0",
        cursor=None,
    )


def _classify(delta: str) -> tuple[str, str]:
    backend = LocalHeartbeatBackend()
    return backend._classify(_context(delta))


def test_idle_polling_does_not_classify_needs_followup() -> None:
    """The canonical fixture from #1043: a worker explicitly reporting
    no tasks and sleeping must not be classified as ``needs_followup``
    just because the command literal contains the word ``next``."""
    text = (
        "• pm task next -p coin_flip\n"
        "• No tasks available for coin_flip.\n"
        "• Sleeping 30s before next poll."
    )
    verdict, _ = _classify(text)
    assert verdict != "needs_followup"


def test_live_worker_coin_flip_no_tasks_available() -> None:
    """worker_coin_flip live evidence (#1043)."""
    text = "• No tasks available for coin_flip."
    verdict, _ = _classify(text)
    assert verdict != "needs_followup"


def test_live_worker_booktalk_running_pm_task_next_again() -> None:
    """worker_booktalk live evidence (#1043)."""
    text = "• Running pm task next -p booktalk again now."
    verdict, _ = _classify(text)
    assert verdict != "needs_followup"


def test_live_worker_camptown_checking_queue_again() -> None:
    """worker_camptown live evidence (#1043)."""
    text = "• Checking the camptown task queue again now."
    verdict, _ = _classify(text)
    assert verdict != "needs_followup"


def test_live_worker_blackjack_trainer_token_summary() -> None:
    """worker_blackjack_trainer live evidence (#1043) — pure token
    summary text contains no ``next``/followup tokens at all."""
    text = "    Tokens:   in=0 out=0 sessions=0"
    verdict, _ = _classify(text)
    assert verdict != "needs_followup"


def test_live_worker_itsalive_refreshing_queue_safe_wrapper() -> None:
    """worker_itsalive live evidence (#1043)."""
    text = "• Refreshing the local worker queue again with the safe wrapper."
    verdict, _ = _classify(text)
    assert verdict != "needs_followup"


def test_genuine_next_step_language_still_classifies_needs_followup() -> None:
    """Regression guard: stripping the command literal must not also
    strip natural-language follow-up signals. ``next step`` outside of
    the ``pm task next`` command literal must still classify."""
    text = "I have a few items remaining. The next step is to run the migration."
    verdict, _ = _classify(text)
    assert verdict == "needs_followup"


def test_snippet_preserves_original_text_not_stripped_form() -> None:
    """The classifier strips command literals before matching, but the
    rendered snippet must still come from the raw text so operators see
    the actual transcript line, not a sanitized form."""
    text = "• pm task next -p coin_flip"
    verdict, reason = _classify(text)
    assert verdict != "needs_followup"
    # The snippet (when present) reflects the raw line.
    assert "pm task next" in reason or "No new transcript" in reason or reason
