"""Unit + integration tests for the pane-text classifier (issue #250).

Run with::

    HOME=/tmp/pytest-agent-pane-classifier uv run pytest \
        tests/test_pane_patterns.py -x

Covers:

* Each ``ClassifierRule`` in :mod:`pollypm.recovery.pane_patterns` has a
  positive and negative fixture so a regression on the regex surfaces
  immediately. Fixtures are derived from real session captures
  (sanitized).
* ``classify_pane`` returns rules in declaration order so single-shot
  callers can take the first hit.
* Integration: the ``pane.classify`` handler raises a
  ``pane:<rule>:<session>`` alert when matching pane text is captured,
  clears it on the next sweep when the text no longer matches, and
  emits an inbox task for the user-visible rules
  (``context_full`` / ``permission_prompt``).

DETECTION + ALERTS ONLY in this PR — the handler must not call
``send_keys`` into any session. Tests assert ``svc.sent`` stays empty
across the whole suite.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pollypm.plugins_builtin.core_recurring.plugin import (
    pane_text_classify_handler,
    plugin as core_plugin,
)
from pollypm.plugins_builtin.task_assignment_notify.resolver import (
    _RuntimeServices,
)
from pollypm.recovery.pane_patterns import (
    RULES,
    USER_VISIBLE_RULES,
    ClassifierRule,
    classify_pane,
    rule_by_name,
)
from pollypm.storage.state import StateStore
from pollypm.work.sqlite_service import SQLiteWorkService


# ---------------------------------------------------------------------------
# Fixtures — captured (sanitized) pane text per rule
# ---------------------------------------------------------------------------


CONTEXT_FULL_POSITIVE = """
[2m thinking ago]

I should write the implementation now.

⚠️ Context window is getting full — approaching context limit.
I need to summarize the conversation before continuing.
"""

CONTEXT_FULL_NEGATIVE = """
Working on the implementation. Reading src/pollypm/recovery/default.py.

The session is healthy and the context budget is fine.
"""


STUCK_ON_ERROR_POSITIVE = """
$ python -m pollypm.cli doctor
Traceback (most recent call last):
  File "/Users/sam/dev/pollypm/src/pollypm/cli.py", line 42, in main
    raise RuntimeError("config not found")
RuntimeError: config not found
"""

STUCK_ON_ERROR_NEGATIVE = """
Reviewing the diff. No errors so far. The reasoning error in
the previous attempt has been corrected; tests are passing.
"""


PERMISSION_PROMPT_POSITIVE = """
Bash command:
  rm -rf .pollypm/cache

Do you want to proceed?
1. Yes
2. No
"""

PERMISSION_PROMPT_NEGATIVE = """
The user asked me to clean the cache. I'll plan the steps and check
in before doing anything destructive — no permission prompt yet.
"""


THEME_TRUST_POSITIVE = """
╭──────────────────────────────────────────────╮
│  Select a theme                              │
│                                              │
│  > Default                                   │
│    Dark                                      │
│    Light                                     │
╰──────────────────────────────────────────────╯
"""

THEME_TRUST_TRUST_POSITIVE = """
Do you trust the files in this folder?

[Yes]  [No]
"""

THEME_TRUST_NEGATIVE = """
Looking at the theme code in src/pollypm/cockpit_ui.py — there's a
helper that selects a theme variant based on terminal capabilities.
"""


# ---------------------------------------------------------------------------
# Pure classifier — one positive + one negative per rule
# ---------------------------------------------------------------------------


class TestClassifyPane:
    """One positive + one negative per rule, plus shape assertions."""

    def test_context_full_positive(self) -> None:
        assert "context_full" in classify_pane(CONTEXT_FULL_POSITIVE)

    def test_context_full_negative(self) -> None:
        assert "context_full" not in classify_pane(CONTEXT_FULL_NEGATIVE)

    def test_stuck_on_error_positive(self) -> None:
        assert "stuck_on_error" in classify_pane(STUCK_ON_ERROR_POSITIVE)

    def test_stuck_on_error_negative(self) -> None:
        # The phrase "reasoning error" mid-sentence must not trip the
        # ^Error: anchored matcher — this is the documented false-
        # positive guard from the issue.
        assert "stuck_on_error" not in classify_pane(
            STUCK_ON_ERROR_NEGATIVE,
        )

    def test_permission_prompt_positive(self) -> None:
        assert "permission_prompt" in classify_pane(
            PERMISSION_PROMPT_POSITIVE,
        )

    def test_permission_prompt_negative(self) -> None:
        assert "permission_prompt" not in classify_pane(
            PERMISSION_PROMPT_NEGATIVE,
        )

    def test_theme_trust_modal_positive_theme(self) -> None:
        assert "theme_trust_modal" in classify_pane(THEME_TRUST_POSITIVE)

    def test_theme_trust_modal_positive_trust(self) -> None:
        assert "theme_trust_modal" in classify_pane(
            THEME_TRUST_TRUST_POSITIVE,
        )

    def test_theme_trust_modal_negative(self) -> None:
        assert "theme_trust_modal" not in classify_pane(
            THEME_TRUST_NEGATIVE,
        )

    def test_empty_pane_returns_no_matches(self) -> None:
        assert classify_pane("") == []
        assert classify_pane("   \n\t  ") == []

    def test_classify_returns_rules_in_declaration_order(self) -> None:
        # A pane that triggers multiple rules at once must report them
        # in the order ``RULES`` declares so a single-shot caller can
        # take the first element as "highest priority match".
        combined = (
            CONTEXT_FULL_POSITIVE
            + "\n"
            + STUCK_ON_ERROR_POSITIVE
            + "\n"
            + PERMISSION_PROMPT_POSITIVE
        )
        names = classify_pane(combined)
        order = [rule.name for rule in RULES]
        # Filter to only the rules that actually matched.
        observed = [n for n in order if n in names]
        assert names == observed

    def test_rule_by_name_round_trips(self) -> None:
        for rule in RULES:
            assert rule_by_name(rule.name) is rule
        assert rule_by_name("nonexistent") is None


class TestRulesShape:
    """Sanity checks on the rule table itself."""

    def test_every_rule_has_a_callable_matcher(self) -> None:
        for rule in RULES:
            assert isinstance(rule, ClassifierRule)
            assert callable(rule.matcher)
            assert rule.severity in {"warn", "error"}
            assert rule.name and ":" not in rule.name

    def test_user_visible_rules_are_subset_of_rules(self) -> None:
        names = {rule.name for rule in RULES}
        assert USER_VISIBLE_RULES.issubset(names)


# ---------------------------------------------------------------------------
# Integration — the handler raises + clears + emits inbox tasks
# ---------------------------------------------------------------------------


@dataclass
class FakeHandle:
    name: str


@dataclass
class FakeSessionService:
    """Mimics the pane-capture surface the handler reads.

    ``captures`` maps session name → captured text. ``sent`` records
    any send_keys-style writes — the test suite asserts this stays
    empty (DETECTION ONLY scope constraint)."""

    handles: list[FakeHandle]
    captures: dict[str, str] = field(default_factory=dict)
    sent: list[tuple[str, str]] = field(default_factory=list)

    def list(self) -> list[FakeHandle]:
        return list(self.handles)

    def capture(self, name: str, lines: int = 200) -> str:
        return self.captures.get(name, "")

    def send(self, name: str, text: str, *, press_enter: bool = True) -> None:
        # The handler must not call this in v1.
        self.sent.append((name, text))


def _patch_resolver(monkeypatch, tmp_path, svc, store, work_service=None):
    def _fake_loader(*, config_path=None):
        return _RuntimeServices(
            session_service=svc,
            state_store=store,
            work_service=work_service,
            project_root=tmp_path,
        )

    monkeypatch.setattr(
        "pollypm.plugins_builtin.task_assignment_notify.resolver.load_runtime_services",
        _fake_loader,
    )


class TestPaneClassifyHandlerRegistration:
    def test_handler_is_registered(self) -> None:
        from pollypm.jobs import JobHandlerRegistry
        from pollypm.plugin_api.v1 import JobHandlerAPI

        registry = JobHandlerRegistry()
        api = JobHandlerAPI(registry, plugin_name="core_recurring")
        core_plugin.register_handlers(api)
        assert "pane.classify" in registry.names()

    def test_roster_cadence_is_30_seconds(self) -> None:
        from pollypm.heartbeat import Roster
        from pollypm.heartbeat.roster import EverySchedule
        from pollypm.plugin_api.v1 import RosterAPI

        roster = Roster()
        api = RosterAPI(roster, plugin_name="core_recurring")
        core_plugin.register_roster(api)
        entries = {entry.handler_name: entry for entry in roster.entries}
        entry = entries.get("pane.classify")
        assert entry is not None, "pane.classify missing from roster"
        assert isinstance(entry.schedule, EverySchedule)
        assert int(entry.schedule.interval.total_seconds()) == 30


class TestPaneClassifyHandler:
    """Handler raises + clears + emits inbox tasks correctly."""

    def test_handler_raises_alert_for_matched_rule(
        self, tmp_path, monkeypatch,
    ) -> None:
        store = StateStore(tmp_path / "state.db")
        svc = FakeSessionService(
            handles=[FakeHandle("worker-demo")],
            captures={"worker-demo": STUCK_ON_ERROR_POSITIVE},
        )
        _patch_resolver(monkeypatch, tmp_path, svc, store)

        result = pane_text_classify_handler({})
        assert result["outcome"] == "swept"
        assert result["sessions_scanned"] == 1
        assert result["alerts_raised"] == 1
        assert result["match_counts"]["stuck_on_error"] == 1

        open_alerts = store.open_alerts()
        types = {a.alert_type for a in open_alerts}
        assert "pane:stuck_on_error" in types
        # Detection-only: the handler must not have sent any keys.
        assert svc.sent == []

    def test_alert_clears_when_text_no_longer_matches(
        self, tmp_path, monkeypatch,
    ) -> None:
        store = StateStore(tmp_path / "state.db")
        svc = FakeSessionService(
            handles=[FakeHandle("worker-demo")],
            captures={"worker-demo": STUCK_ON_ERROR_POSITIVE},
        )
        _patch_resolver(monkeypatch, tmp_path, svc, store)

        # First sweep raises.
        first = pane_text_classify_handler({})
        assert first["alerts_raised"] == 1

        # Now the pane is healthy — second sweep clears the alert.
        svc.captures["worker-demo"] = STUCK_ON_ERROR_NEGATIVE
        second = pane_text_classify_handler({})
        assert second["alerts_cleared"] >= 1
        open_types = {a.alert_type for a in store.open_alerts()}
        assert "pane:stuck_on_error" not in open_types
        assert svc.sent == []

    def test_user_visible_rule_emits_inbox_task(
        self, tmp_path, monkeypatch,
    ) -> None:
        store = StateStore(tmp_path / "state.db")
        work = SQLiteWorkService(db_path=tmp_path / "work.db")
        svc = FakeSessionService(
            handles=[FakeHandle("worker-demo")],
            captures={"worker-demo": CONTEXT_FULL_POSITIVE},
        )
        _patch_resolver(monkeypatch, tmp_path, svc, store, work_service=work)

        result = pane_text_classify_handler({})
        assert result["outcome"] == "swept"
        assert result["alerts_raised"] == 1
        assert result["inbox_items_emitted"] == 1

        # The created task carries the dedupe label so a second sweep
        # must not duplicate it.
        result2 = pane_text_classify_handler({})
        # Alert is already open; counter only ticks on first-fire.
        assert result2["alerts_raised"] == 0
        assert result2["inbox_items_emitted"] == 0
        assert svc.sent == []

    def test_non_user_visible_rule_does_not_emit_inbox(
        self, tmp_path, monkeypatch,
    ) -> None:
        store = StateStore(tmp_path / "state.db")
        work = SQLiteWorkService(db_path=tmp_path / "work.db")
        svc = FakeSessionService(
            handles=[FakeHandle("worker-demo")],
            captures={"worker-demo": STUCK_ON_ERROR_POSITIVE},
        )
        _patch_resolver(monkeypatch, tmp_path, svc, store, work_service=work)

        result = pane_text_classify_handler({})
        assert result["alerts_raised"] == 1
        # stuck_on_error is *not* in USER_VISIBLE_RULES — no inbox task.
        assert result["inbox_items_emitted"] == 0
        assert svc.sent == []

    def test_handler_skips_when_session_service_missing(
        self, tmp_path, monkeypatch,
    ) -> None:
        # Resolver returns None services — handler short-circuits.
        def _fake_loader(*, config_path=None):
            return _RuntimeServices(
                session_service=None,
                state_store=None,
                work_service=None,
                project_root=tmp_path,
            )

        monkeypatch.setattr(
            "pollypm.plugins_builtin.task_assignment_notify.resolver.load_runtime_services",
            _fake_loader,
        )
        result = pane_text_classify_handler({})
        assert result == {"outcome": "skipped", "reason": "services_unavailable"}

    def test_no_match_yields_no_alerts_no_inbox(
        self, tmp_path, monkeypatch,
    ) -> None:
        store = StateStore(tmp_path / "state.db")
        work = SQLiteWorkService(db_path=tmp_path / "work.db")
        svc = FakeSessionService(
            handles=[FakeHandle("worker-clean")],
            captures={"worker-clean": "Working normally. All good."},
        )
        _patch_resolver(monkeypatch, tmp_path, svc, store, work_service=work)

        result = pane_text_classify_handler({})
        assert result["sessions_scanned"] == 1
        assert result["alerts_raised"] == 0
        assert result["alerts_cleared"] == 0
        assert result["inbox_items_emitted"] == 0
        assert svc.sent == []
