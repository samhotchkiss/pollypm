"""Tests for worker-guide auto-injection (wg02 / #239).

Extends M05's memory-injection builder: every session whose
``session.role == "worker"`` gets the canonical worker guide prepended
under a ``## Worker Protocol`` section. Non-worker sessions (PM,
reviewer, supervisor, triage) get exactly today's behavior.

Acceptance (from #239):

- A new worker session's system prompt contains the worker guide
  verbatim.
- Non-worker sessions do NOT get the worker guide.
- Token budget is respected — rendered injection stays within the
  documented cap (~2K tokens for the guide alone).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pollypm.memory_prompts import (
    WORKER_PROTOCOL_HEADING,
    build_worker_protocol_injection,
    load_worker_guide_text,
    prepend_memory_injection,
    prepend_worker_protocol,
)


# ---------------------------------------------------------------------------
# load_worker_guide_text
# ---------------------------------------------------------------------------


def test_load_worker_guide_text_returns_the_repo_doc():
    """In the editable-install dev environment, the loader must find
    docs/worker-guide.md in the repo tree."""
    text = load_worker_guide_text()
    assert text, "worker guide not found on disk"
    # Signature strings from the guide.
    assert "Worker Guide" in text
    assert "pm task done" in text
    assert "What NOT to do" in text


# ---------------------------------------------------------------------------
# build_worker_protocol_injection
# ---------------------------------------------------------------------------


def test_injection_empty_for_non_worker_roles():
    """PM, reviewer, supervisor, triage, operator, None — all get ""."""
    for role in ["pm", "reviewer", "supervisor", "triage", "operator", "", None]:
        out = build_worker_protocol_injection(session_role=role)
        assert out == "", f"unexpected injection for role={role!r}: {out!r}"


def test_injection_non_empty_for_worker_role():
    out = build_worker_protocol_injection(session_role="worker")
    assert out.startswith(WORKER_PROTOCOL_HEADING)
    # Contains load-bearing signal strings from the guide.
    assert "pm task claim" in out
    assert "pm task done" in out


def test_injection_accepts_explicit_guide_text_for_isolation():
    """Tests pin a known payload without needing the real doc on disk."""
    payload = "# Worker Guide\n\nBody goes here."
    out = build_worker_protocol_injection(
        session_role="worker",
        guide_text=payload,
    )
    assert out.startswith(WORKER_PROTOCOL_HEADING + "\n")
    assert "Body goes here." in out


def test_injection_empty_when_guide_missing():
    """Blank guide text → empty injection. Session startup must not
    choke on a missing doc in a slim install."""
    out = build_worker_protocol_injection(
        session_role="worker",
        guide_text="",
    )
    assert out == ""


def test_injection_has_single_trailing_newline():
    out = build_worker_protocol_injection(
        session_role="worker",
        guide_text="# Worker Guide\n\nBody.",
    )
    # Exactly one trailing newline so prepend functions add exactly
    # one blank line of separation.
    assert out.endswith("\n")
    assert not out.endswith("\n\n")


# ---------------------------------------------------------------------------
# prepend_worker_protocol composition
# ---------------------------------------------------------------------------


def test_prepend_worker_protocol_noop_on_empty_injection():
    assert prepend_worker_protocol("persona", "") == "persona"


def test_prepend_worker_protocol_prepends_with_separator():
    injection = f"{WORKER_PROTOCOL_HEADING}\n\nbody\n"
    result = prepend_worker_protocol("persona prompt", injection)
    assert result.startswith(WORKER_PROTOCOL_HEADING)
    assert result.endswith("persona prompt")
    # Blank-line separation between injection and persona.
    assert "\n\npersona prompt" in result


def test_compose_worker_protocol_then_memory_then_persona():
    """The session service composes the two injections in a fixed
    order: worker protocol → memory → persona. A worker session's
    final prompt reads top-to-bottom: protocol, memory, persona."""
    persona = "You are a worker in project X."
    memory = "## What you should know\n\n- remember: Y\n"
    protocol = build_worker_protocol_injection(
        session_role="worker",
        guide_text="# Worker Guide\n\nBody.",
    )

    with_memory = prepend_memory_injection(persona, memory)
    combined = prepend_worker_protocol(with_memory, protocol)

    # Order check: protocol heading, then memory heading, then persona.
    i_protocol = combined.find(WORKER_PROTOCOL_HEADING)
    i_memory = combined.find("## What you should know")
    i_persona = combined.find("You are a worker")
    assert 0 <= i_protocol < i_memory < i_persona, combined


# ---------------------------------------------------------------------------
# Token / character budget
# ---------------------------------------------------------------------------


def test_worker_guide_under_documented_token_budget():
    """Spec claims the guide is ~2K tokens. At 4 chars/token that's
    ~8K chars. We give headroom for the H2 wrapper and check against
    a 10K-char ceiling. A guide that grows past that should trigger
    an explicit decision (trim or raise the budget)."""
    text = load_worker_guide_text()
    if not text:
        pytest.skip("worker guide not available in this install")
    # Raw guide size bound.
    assert len(text) < 10_000, (
        f"worker-guide.md is {len(text)} chars (~{len(text)//4} tokens); "
        f"spec budget is ~2K tokens (~8K chars). Either trim the guide "
        f"or raise the budget here + in the spec."
    )


def test_worker_protocol_injection_under_budget():
    """Rendered injection (heading + guide + trailing newlines) must
    stay within the same budget — the H2 wrapper is tiny."""
    out = build_worker_protocol_injection(session_role="worker")
    if not out:
        pytest.skip("worker guide not available in this install")
    assert len(out) < 10_100  # + a few bytes for the heading


# ---------------------------------------------------------------------------
# Integration: TmuxSessionService._inject_memory_into_prompt routes the
# worker protocol for role=worker and skips it otherwise.
# ---------------------------------------------------------------------------


class _FakeProject:
    def __init__(self, tmp_path: Path):
        self.root_dir = tmp_path
        self.name = "fakeproj"


class _FakeConfig:
    def __init__(self, tmp_path: Path):
        self.project = _FakeProject(tmp_path)


@pytest.fixture
def tmux_service(tmp_path: Path):
    """A TmuxSessionService wired with a scratch project — no tmux
    actually runs; we only exercise the pure injection method."""
    from pollypm.session_services.tmux import TmuxSessionService

    config = _FakeConfig(tmp_path)
    store = object()  # never touched by _inject_memory_into_prompt
    return TmuxSessionService(config=config, store=store)


def test_tmux_service_injects_worker_protocol_for_worker(tmux_service):
    persona = "You are a worker persona."
    out = tmux_service._inject_memory_into_prompt(
        initial_input=persona,
        session_role="worker",
        task_title=None,
        task_description=None,
        user_id="operator",
    )
    assert WORKER_PROTOCOL_HEADING in out
    assert "pm task done" in out
    assert persona in out
    # Protocol appears above the persona.
    assert out.index(WORKER_PROTOCOL_HEADING) < out.index(persona)


@pytest.mark.parametrize("role", ["pm", "reviewer", "supervisor", "triage"])
def test_tmux_service_skips_worker_protocol_for_other_roles(tmux_service, role):
    persona = f"You are a {role}."
    out = tmux_service._inject_memory_into_prompt(
        initial_input=persona,
        session_role=role,
        task_title=None,
        task_description=None,
        user_id="operator",
    )
    # No Worker Protocol section for non-worker roles.
    assert WORKER_PROTOCOL_HEADING not in out, (
        f"role {role!r} unexpectedly got worker protocol"
    )
    # Persona survives unchanged (modulo memory injection, which is
    # empty for a scratch project with no memories).
    assert persona in out


def test_tmux_service_injects_nothing_for_missing_guide(tmux_service, monkeypatch):
    """If the guide can't be located, worker sessions still launch —
    they just don't get the protocol section. Defensive resilience."""
    import pollypm.memory_prompts as mp

    monkeypatch.setattr(mp, "load_worker_guide_text", lambda: "")

    persona = "You are a worker persona."
    out = tmux_service._inject_memory_into_prompt(
        initial_input=persona,
        session_role="worker",
        task_title=None,
        task_description=None,
        user_id="operator",
    )
    # No crash, no protocol, persona intact.
    assert WORKER_PROTOCOL_HEADING not in out
    assert persona in out
