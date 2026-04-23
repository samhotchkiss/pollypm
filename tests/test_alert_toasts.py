"""Alert-toast overlay tests — live awareness in the cockpit TUI.

Drives :class:`pollypm.cockpit_ui.PollyInboxApp` via ``Pilot`` so the
toast layer is exercised on the same App Sam uses most. The notifier is
shared infrastructure (mounted on every cockpit App) so covering one
host is enough — the helper is tested directly in :func:`poll_now`
tests below too.

The tests bypass the live SQLite poll by monkeypatching
``AlertNotifier._fetch_alerts`` to return synthetic records. That keeps
them fast (<1s each) and independent of the StateStore schema.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest

from pollypm.work.sqlite_service import SQLiteWorkService


# ---------------------------------------------------------------------------
# Fixtures — reuse the minimal-config shape from test_cockpit_inbox_ui
# ---------------------------------------------------------------------------


def _write_minimal_config(project_path: Path, config_path: Path) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "[project]\n"
        f'tmux_session = "pollypm-test"\n'
        f'workspace_root = "{project_path.parent}"\n'
        "\n"
        f'[projects.demo]\n'
        f'key = "demo"\n'
        f'name = "Demo"\n'
        f'path = "{project_path}"\n'
    )


def _seed_project(project_path: Path) -> list[str]:
    db_path = project_path / ".pollypm" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    svc = SQLiteWorkService(db_path=db_path, project_path=project_path)
    try:
        ids: list[str] = []
        for title, body in [
            ("Smoke subject", "Smoke body"),
        ]:
            t = svc.create(
                title=title,
                description=body,
                type="task",
                project="demo",
                flow_template="chat",
                roles={"requester": "user", "operator": "polly"},
                priority="normal",
                created_by="polly",
            )
            ids.append(t.task_id)
        return ids
    finally:
        svc.close()


def _load_config_compatible(config_path: Path) -> bool:
    try:
        from pollypm.config import load_config
        cfg = load_config(config_path)
        return "demo" in getattr(cfg, "projects", {})
    except Exception:  # noqa: BLE001
        return False


@dataclass
class _FakeAlert:
    """Stand-in for :class:`pollypm.storage.state.AlertRecord`.

    The notifier only reads ``alert_id``, ``severity``, ``message``,
    ``session_name``, ``alert_type``, ``updated_at`` — a dataclass with
    the same attrs is enough to exercise every code path.
    """
    alert_id: int
    severity: str = "warn"
    message: str = "state drift detected"
    session_name: str = "demo:polly"
    alert_type: str = "state_drift"
    status: str = "open"
    created_at: str = "2026-04-17T00:00:00Z"
    updated_at: str = "2026-04-17T00:00:00Z"


@pytest.fixture
def inbox_env(tmp_path: Path):
    project_path = tmp_path / "demo"
    project_path.mkdir()
    (project_path / ".git").mkdir()
    config_path = tmp_path / "pollypm.toml"
    _write_minimal_config(project_path, config_path)
    ids = _seed_project(project_path)
    return {
        "config_path": config_path,
        "project_path": project_path,
        "task_ids": ids,
    }


@pytest.fixture
def inbox_app(inbox_env):
    if not _load_config_compatible(inbox_env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_ui import PollyInboxApp
    return PollyInboxApp(inbox_env["config_path"])


def _run(coro):
    asyncio.run(coro)


def _install_fake_alerts(notifier, alerts: list[_FakeAlert]) -> None:
    """Swap ``notifier._fetch_alerts`` to return ``alerts``.

    We also reset ``_seen_alert_ids`` so priming (which happens in
    ``__init__`` *before* the fake is installed) doesn't pre-dedup the
    rows we're about to feed in.
    """
    notifier._fetch_alerts = lambda: list(alerts)
    notifier._seen_alert_ids = set()


# ---------------------------------------------------------------------------
# 1. Toast renders for a new alert
# ---------------------------------------------------------------------------


def test_toast_renders_for_new_alert(inbox_env, inbox_app) -> None:
    async def body() -> None:
        async with inbox_app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            notifier = inbox_app._alert_notifier
            assert notifier is not None, "notifier must attach on mount"

            _install_fake_alerts(notifier, [
                _FakeAlert(alert_id=1, severity="warn", message="state drift on demo"),
            ])
            mounted = notifier.poll_now()
            await pilot.pause()

            assert len(mounted) == 1
            toast = mounted[0]
            # Rendered text surfaces the message + severity icon.
            rendered = str(toast.render())
            assert "state drift" in rendered
            # Severity class is applied up front.
            assert toast.has_class("severity-warn")
            # Visible in the container's child list.
            assert toast in notifier.visible_toasts
    _run(body())


def test_warn_toast_width_respects_narrow_host(inbox_env) -> None:
    if not _load_config_compatible(inbox_env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_ui import PollyCockpitApp

    app = PollyCockpitApp(inbox_env["config_path"])

    async def body() -> None:
        async with app.run_test(size=(30, 24)) as pilot:
            await pilot.pause()
            notifier = app._alert_notifier
            assert notifier is not None
            _install_fake_alerts(
                notifier,
                [
                    _FakeAlert(
                        alert_id=11,
                        severity="warn",
                        message="[Alert] Additional work remains — \x1b[2C\x1b[38;5;246m?\x1b[1Cfor\x1b[1Cshortcuts\x1b[39m",
                    ),
                ],
            )
            mounted = notifier.poll_now()
            await pilot.pause()
            assert len(mounted) == 1
            toast = mounted[0]
            assert toast.has_class("severity-warn")
            assert toast.size.width <= 28
            rendered = str(toast.render())
            assert "Additional work remains" in rendered
            assert "\x1b" not in rendered

    _run(body())


# ---------------------------------------------------------------------------
# 2. Auto-dismisses after timeout
# ---------------------------------------------------------------------------


def test_toast_auto_dismisses_after_timeout(inbox_env, inbox_app) -> None:
    """The set_timer callback removes the widget once the window elapses.

    We don't wait the real 8s — instead we construct a toast with a
    tiny ``timeout_seconds`` and pump the event loop.
    """
    async def body() -> None:
        async with inbox_app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            notifier = inbox_app._alert_notifier
            assert notifier is not None

            # Shrink the default so pilot.pause() captures the
            # dismiss within a single test heartbeat. 0.1s gives the
            # Textual scheduler enough slack that this test isn't
            # flaky when the CPU is contended by a parallel test
            # suite; the dismiss assertion still catches any real
            # regression in the timer hookup.
            from pollypm.cockpit_ui import AlertToast
            original_default = AlertToast.DEFAULT_TIMEOUT_SECONDS
            AlertToast.DEFAULT_TIMEOUT_SECONDS = 0.1
            try:
                _install_fake_alerts(notifier, [
                    _FakeAlert(alert_id=7, severity="error", message="auth broken"),
                ])
                mounted = notifier.poll_now()
                assert len(mounted) == 1
                toast = mounted[0]
                # Let Textual finish mounting before we poll the timer.
                await pilot.pause()
                assert toast in notifier.visible_toasts

                # Wait several multiples of the shortened timer so
                # scheduler jitter under load doesn't race us.
                await asyncio.sleep(0.5)
                await pilot.pause()

                # After the timer fires, the toast is flagged dismissed
                # (display=False) + scheduled for removal from the DOM.
                # ``visible_toasts`` hides dismissed widgets so callers
                # can react synchronously.
                assert toast not in notifier.visible_toasts
                assert toast.display is False
            finally:
                AlertToast.DEFAULT_TIMEOUT_SECONDS = original_default
    _run(body())


# ---------------------------------------------------------------------------
# 3. Dedup — same alert_id doesn't toast twice
# ---------------------------------------------------------------------------


def test_dedup_same_alert_id_no_double_toast(inbox_env, inbox_app) -> None:
    async def body() -> None:
        async with inbox_app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            notifier = inbox_app._alert_notifier
            assert notifier is not None

            alert = _FakeAlert(alert_id=42, message="persona swap detected")
            _install_fake_alerts(notifier, [alert])

            first_round = notifier.poll_now()
            await pilot.pause()
            assert len(first_round) == 1

            # Second poll with the same alert_id → no new mounts.
            second_round = notifier.poll_now()
            await pilot.pause()
            assert second_round == []

            # Exactly one live toast.
            assert len(notifier.visible_toasts) == 1
    _run(body())


# ---------------------------------------------------------------------------
# 4. Stacking cap — 3 max; a 4th evicts the oldest
# ---------------------------------------------------------------------------


def test_stack_caps_at_three_fourth_evicts_oldest(inbox_env, inbox_app) -> None:
    async def body() -> None:
        async with inbox_app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            notifier = inbox_app._alert_notifier
            assert notifier is not None

            # Feed three alerts, confirm three live toasts.
            _install_fake_alerts(notifier, [
                _FakeAlert(alert_id=1, message="one"),
                _FakeAlert(alert_id=2, message="two"),
                _FakeAlert(alert_id=3, message="three"),
            ])
            notifier.poll_now()
            await pilot.pause()
            assert len(notifier.visible_toasts) == 3
            oldest = notifier.visible_toasts[0]
            assert "one" in str(oldest.render())

            # Fourth alert — oldest evicts.
            notifier._fetch_alerts = lambda: [
                _FakeAlert(alert_id=1, message="one"),
                _FakeAlert(alert_id=2, message="two"),
                _FakeAlert(alert_id=3, message="three"),
                _FakeAlert(alert_id=4, message="four"),
            ]
            notifier.poll_now()
            await pilot.pause()
            live = notifier.visible_toasts
            assert len(live) == 3
            assert all("one" not in str(t.render()) for t in live)
            assert any("four" in str(t.render()) for t in live)
    _run(body())


# ---------------------------------------------------------------------------
# 5. Severity styling — warn vs error
# ---------------------------------------------------------------------------


def test_severity_styling_differs_warn_vs_error(inbox_env, inbox_app) -> None:
    async def body() -> None:
        async with inbox_app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            notifier = inbox_app._alert_notifier
            assert notifier is not None

            _install_fake_alerts(notifier, [
                _FakeAlert(alert_id=1, severity="warn", message="capacity low"),
                _FakeAlert(alert_id=2, severity="error", message="no session"),
            ])
            notifier.poll_now()
            await pilot.pause()

            toasts = notifier.visible_toasts
            assert len(toasts) == 2
            by_id = {t.alert_id: t for t in toasts}
            assert by_id[1].has_class("severity-warn")
            assert not by_id[1].has_class("severity-error")
            assert by_id[2].has_class("severity-error")
            assert not by_id[2].has_class("severity-warn")
    _run(body())


# ---------------------------------------------------------------------------
# 6. ``a`` keybinding routes to the alerts view
# ---------------------------------------------------------------------------


def test_a_keybinding_triggers_view_alerts_action(tmp_path: Path) -> None:
    """Verify the shared action dispatches to Metrics via ``_palette_nav``.

    The inbox App binds ``a`` to archive, so this test uses the
    PollyMetricsApp variant — which has a priority ``a`` binding only
    when no local ``a`` exists — and asserts the stub ``_palette_nav``
    fires.
    """
    project_path = tmp_path / "demo"
    project_path.mkdir()
    (project_path / ".git").mkdir()
    config_path = tmp_path / "pollypm.toml"
    _write_minimal_config(project_path, config_path)
    _seed_project(project_path)
    if not _load_config_compatible(config_path):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")

    calls: list[str] = []

    async def body() -> None:
        # Use PollyActivityFeedApp — it binds ``a`` to ``view_alerts`` and
        # has no other conflicting ``a`` action.
        from pollypm.cockpit_ui import PollyActivityFeedApp
        import pollypm.cockpit_ui as ui

        original_nav = ui._palette_nav

        def fake_nav(app, target, *, is_project=False):
            calls.append(target)

        ui._palette_nav = fake_nav
        try:
            app = PollyActivityFeedApp(config_path)
            async with app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                await pilot.press("a")
                await pilot.pause()
            assert calls == ["metrics"]
        finally:
            ui._palette_nav = original_nav
    _run(body())


# ---------------------------------------------------------------------------
# 7a. Operational alerts never toast (#765)
# ---------------------------------------------------------------------------


def test_operational_alert_types_are_suppressed_from_toasts(inbox_env, inbox_app) -> None:
    """Heartbeat classification signals (suspected_loop, stabilize_failed,
    needs_followup) are operational — they should appear in the activity
    log / alert list but MUST NOT pop as toasts. Toasts are an
    interruption primitive reserved for user-actionable events. See #765.
    """
    async def body() -> None:
        async with inbox_app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            notifier = inbox_app._alert_notifier
            assert notifier is not None

            _install_fake_alerts(notifier, [
                _FakeAlert(
                    alert_id=101,
                    severity="warn",
                    alert_type="suspected_loop",
                    message="same snapshot for 3 heartbeats",
                ),
                _FakeAlert(
                    alert_id=102,
                    severity="warn",
                    alert_type="stabilize_failed",
                    message="stabilize step not converging",
                ),
                _FakeAlert(
                    alert_id=103,
                    severity="warn",
                    alert_type="needs_followup",
                    message="follow-up queued",
                ),
                # Non-operational alert should still toast — prove the
                # filter doesn't swallow real action-required signals.
                _FakeAlert(
                    alert_id=104,
                    severity="error",
                    alert_type="auth_broken",
                    message="account auth failure",
                ),
            ])
            mounted = notifier.poll_now()
            await pilot.pause()

            # Only the non-operational alert produced a toast.
            assert len(mounted) == 1
            toast = mounted[0]
            assert "auth failure" in str(toast.render())
            # Operational alerts were still marked seen (so the next
            # poll doesn't re-consider them).
            assert len(notifier._seen_alert_ids) == 4
    _run(body())


def test_is_operational_alert_helper() -> None:
    """Unit test for the operational-type detector — covers the
    canonical set plus a couple of negative cases."""
    from pollypm.cockpit_alerts import _is_operational_alert
    assert _is_operational_alert("suspected_loop") is True
    assert _is_operational_alert("stabilize_failed") is True
    assert _is_operational_alert("needs_followup") is True
    assert _is_operational_alert("auth_broken") is False
    assert _is_operational_alert("persona_swap_detected") is False
    assert _is_operational_alert("") is False
    assert _is_operational_alert("random_type") is False


# ---------------------------------------------------------------------------
# 7. Esc on a toast dismisses it
# ---------------------------------------------------------------------------


def test_toast_dismiss_removes_widget(inbox_env, inbox_app) -> None:
    """Calling the toast's dismiss action unmounts it immediately.

    Textual's Pilot keypress goes to the focused widget; toasts don't
    auto-focus (by design — we don't want to steal the list's focus).
    Instead we exercise the public dismiss path directly, matching how
    the Esc binding (scoped to the toast) and the on_click handler both
    route into ``action_dismiss_toast``.
    """
    async def body() -> None:
        async with inbox_app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            notifier = inbox_app._alert_notifier
            assert notifier is not None

            _install_fake_alerts(notifier, [
                _FakeAlert(alert_id=11, severity="warn", message="plan missing"),
            ])
            mounted = notifier.poll_now()
            await pilot.pause()
            assert len(mounted) == 1
            toast = mounted[0]
            assert toast.is_mounted

            # Same path the Esc binding + on_click handler use.
            toast.action_dismiss_toast()
            await pilot.pause()
            assert toast.display is False
            assert toast not in notifier.visible_toasts
    _run(body())
