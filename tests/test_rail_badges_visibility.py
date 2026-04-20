"""Tests for er03 — badge providers + visibility predicates.

See docs/extensible-rail-spec.md §3/§4 and issue #223.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pollypm.cockpit_rail import (
    CockpitItem,
    _rows_for_registration,
    _visibility_passes,
)
from pollypm.plugin_api.v1 import (
    PanelSpec,
    RailAPI,
    RailContext,
    RailRegistry,
)


def _handler(ctx: RailContext) -> PanelSpec:
    return PanelSpec(widget=None)


def _mk_registration(registry: RailRegistry, **kwargs):
    api = RailAPI(plugin_name=kwargs.pop("plugin_name", "t"), registry=registry)
    return api.register_item(
        section=kwargs.pop("section", "workflows"),
        index=kwargs.pop("index", 30),
        label=kwargs.pop("label", "Activity"),
        handler=kwargs.pop("handler", _handler),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Badge providers.
# ---------------------------------------------------------------------------


def test_badge_provider_returning_int_appends_count_to_label() -> None:
    registry = RailRegistry()
    reg = _mk_registration(
        registry, label="Activity",
        badge_provider=lambda ctx: 3,
    )
    rows = _rows_for_registration(reg, RailContext())
    assert len(rows) == 1
    assert rows[0].label == "Activity (3)"


def test_badge_provider_returning_string_appends_to_label() -> None:
    registry = RailRegistry()
    reg = _mk_registration(
        registry, label="Queue",
        badge_provider=lambda ctx: "hot",
    )
    rows = _rows_for_registration(reg, RailContext())
    assert rows[0].label == "Queue (hot)"


def test_badge_provider_returning_none_leaves_label_untouched() -> None:
    registry = RailRegistry()
    reg = _mk_registration(
        registry, label="Activity",
        badge_provider=lambda ctx: None,
    )
    rows = _rows_for_registration(reg, RailContext())
    assert rows[0].label == "Activity"


def test_badge_provider_returning_zero_leaves_label_untouched() -> None:
    # Zero-count badge is conventionally hidden (don't show "Activity (0)").
    registry = RailRegistry()
    reg = _mk_registration(
        registry, label="Activity",
        badge_provider=lambda ctx: 0,
    )
    rows = _rows_for_registration(reg, RailContext())
    assert rows[0].label == "Activity"


def test_badge_provider_raising_does_not_crash_renderer(caplog) -> None:
    registry = RailRegistry()

    def _crash(ctx: RailContext):
        raise RuntimeError("boom")

    reg = _mk_registration(
        registry, label="Activity",
        badge_provider=_crash,
    )

    with caplog.at_level(logging.ERROR, logger="pollypm.cockpit"):
        rows = _rows_for_registration(reg, RailContext())

    assert rows[0].label == "Activity"  # no badge appended
    assert any("badge_provider raised" in rec.message for rec in caplog.records)


def test_badge_not_duplicated_when_label_provider_already_baked_count() -> None:
    """If label_provider already returns 'Inbox (3)', the badge_provider's
    duplicate (3) should not be appended again."""
    registry = RailRegistry()
    reg = _mk_registration(
        registry, label="Inbox",
        label_provider=lambda ctx: "Inbox (3)",
        badge_provider=lambda ctx: 3,
    )
    rows = _rows_for_registration(reg, RailContext())
    assert rows[0].label == "Inbox (3)"


# ---------------------------------------------------------------------------
# Visibility predicates.
# ---------------------------------------------------------------------------


def test_visibility_always_returns_true() -> None:
    registry = RailRegistry()
    reg = _mk_registration(registry, visibility="always")
    assert _visibility_passes(reg, RailContext()) is True


def test_visibility_callable_returning_false_hides_item() -> None:
    registry = RailRegistry()
    reg = _mk_registration(registry, visibility=lambda ctx: False)
    assert _visibility_passes(reg, RailContext()) is False


def test_visibility_callable_returning_true_shows_item() -> None:
    registry = RailRegistry()
    reg = _mk_registration(registry, visibility=lambda ctx: True)
    assert _visibility_passes(reg, RailContext()) is True


def test_visibility_callable_raising_hides_item_and_logs(caplog) -> None:
    registry = RailRegistry()

    def _crash(ctx: RailContext):
        raise ValueError("nope")

    reg = _mk_registration(registry, visibility=_crash)
    with caplog.at_level(logging.ERROR, logger="pollypm.cockpit"):
        assert _visibility_passes(reg, RailContext()) is False
    assert any("visibility predicate raised" in rec.message for rec in caplog.records)


def test_visibility_has_feature_respects_registered_features() -> None:
    registry = RailRegistry()
    reg = _mk_registration(
        registry, visibility="has_feature", feature_name="magic",
    )
    ctx_on = RailContext(extras={"features": frozenset({"magic"})})
    ctx_off = RailContext(extras={"features": frozenset()})
    assert _visibility_passes(reg, ctx_on) is True
    assert _visibility_passes(reg, ctx_off) is False


def test_visibility_has_feature_without_features_extras_hides() -> None:
    registry = RailRegistry()
    reg = _mk_registration(
        registry, visibility="has_feature", feature_name="missing",
    )
    assert _visibility_passes(reg, RailContext()) is False


# ---------------------------------------------------------------------------
# Evaluation on every rail-refresh tick.
# ---------------------------------------------------------------------------


def test_badge_reevaluated_each_tick() -> None:
    registry = RailRegistry()
    counter = {"n": 0}

    def _badge(ctx: RailContext):
        counter["n"] += 1
        return counter["n"]

    reg = _mk_registration(registry, label="Tick", badge_provider=_badge)
    rows1 = _rows_for_registration(reg, RailContext())
    rows2 = _rows_for_registration(reg, RailContext())
    rows3 = _rows_for_registration(reg, RailContext())
    assert rows1[0].label == "Tick (1)"
    assert rows2[0].label == "Tick (2)"
    assert rows3[0].label == "Tick (3)"
    assert counter["n"] == 3


def test_visibility_reevaluated_each_tick() -> None:
    registry = RailRegistry()
    state = {"visible": True}

    def _vis(ctx: RailContext) -> bool:
        return state["visible"]

    reg = _mk_registration(registry, visibility=_vis)
    assert _visibility_passes(reg, RailContext()) is True
    state["visible"] = False
    assert _visibility_passes(reg, RailContext()) is False
    state["visible"] = True
    assert _visibility_passes(reg, RailContext()) is True
