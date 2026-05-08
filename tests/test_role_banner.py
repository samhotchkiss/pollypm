"""Tests for the role-banner helper (#757).

The banner is the first thing the agent reads in a kickoff and is
designed to resist mid-flight persona-drift requests. These tests lock
in:

- shape (fence + role + session lines + trailing blank line)
- fail-closed on missing inputs (no banner rather than a broken one)
- idempotence (re-rendering a file that already carries a banner
  doesn't stack a second one)
"""

from __future__ import annotations

from pollypm.role_banner import prepend_role_banner, render_role_banner


def test_render_role_banner_includes_role_and_session() -> None:
    text = render_role_banner("architect_polly_remote", "architect")

    assert "CANONICAL ROLE: architect" in text
    assert "SESSION NAME:   architect_polly_remote" in text
    # Ends with a blank line so downstream content has visible air.
    assert text.endswith("\n\n")
    # Opening + closing fences so the banner is visually bounded.
    assert text.count("=" * 70) == 2


def test_render_role_banner_returns_empty_when_inputs_missing() -> None:
    """A partial banner is worse than none — mis-declared roles would
    train the model to ignore the line. Be fail-closed."""
    assert render_role_banner("", "architect") == ""
    assert render_role_banner("architect_polly_remote", "") == ""
    assert render_role_banner("   ", "architect") == ""


def test_prepend_role_banner_places_banner_at_top() -> None:
    body = "---\nname: architect\n---\n\n<identity>\nYou are ...\n</identity>\n"
    result = prepend_role_banner(body, session_name="architect_x", role="architect")

    assert result.startswith("=" * 70)
    # Original body still intact, positioned after the banner.
    assert body in result
    # Role + session appear BEFORE the YAML frontmatter.
    banner_end = result.index("---\nname: architect")
    header = result[:banner_end]
    assert "CANONICAL ROLE: architect" in header
    assert "SESSION NAME:   architect_x" in header


def test_prepend_role_banner_is_idempotent() -> None:
    """Re-running prepend on content that already starts with a banner
    must NOT stack a second banner. Otherwise replayed kickoffs would
    accumulate fences on every write."""
    body = "original kickoff body"
    once = prepend_role_banner(body, session_name="worker_x", role="worker")
    twice = prepend_role_banner(once, session_name="worker_x", role="worker")

    assert once == twice
    assert once.count("=" * 70) == 2


def test_prepend_role_banner_missing_role_returns_body_unchanged() -> None:
    body = "short prompt"
    assert prepend_role_banner(body, session_name="x", role="") == body
    assert prepend_role_banner(body, session_name="", role="worker") == body


def test_render_role_banner_worker_includes_execute_dont_ask_line() -> None:
    """Workers default to checking in for a "nod" between outline and execute,
    which silently stalls tasks. The banner counters that bias for workers
    only — architects/reviewers should NOT carry "execute, don't ask"
    guidance because their job legitimately includes asking."""
    text = render_role_banner("task-coffeeboardnm-1", "worker")

    assert "execute" in text.lower()
    assert "don't pause to ask" in text.lower() or "don't ask" in text.lower()


def test_render_role_banner_non_worker_omits_execute_dont_ask_line() -> None:
    for role in ("architect", "reviewer", "operator-pm", "heartbeat-supervisor"):
        text = render_role_banner(f"{role}_test", role)
        assert "execute. don't pause to ask" not in text.lower(), (
            f"role={role} unexpectedly carries the worker-only execute-don't-ask line"
        )
