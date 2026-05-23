from __future__ import annotations

from pathlib import Path

from pollypm.capacity import ProactiveFailoverDecision
from pollypm.models import (
    AccountConfig,
    PollyPMConfig,
    PollyPMSettings,
    ProjectSettings,
    ProviderKind,
    SessionConfig,
)
from pollypm.plugins_builtin.core_recurring.maintenance import (
    _apply_proactive_controller_failover,
)


class _MsgStore:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.alerts: list[tuple[str, str, str, str]] = []
        self.cleared: list[tuple[str, str, str]] = []

    def append_event(self, *, scope, sender, subject, payload):  # noqa: ANN001
        self.events.append(
            {
                "scope": scope,
                "sender": sender,
                "subject": subject,
                "payload": payload,
            }
        )

    def upsert_alert(self, session_name, alert_type, severity, message):  # noqa: ANN001
        self.alerts.append((session_name, alert_type, severity, message))

    def clear_alert(self, session_name, alert_type, *, who_cleared="system"):  # noqa: ANN001
        self.cleared.append((session_name, alert_type, who_cleared))


def _config(tmp_path: Path) -> PollyPMConfig:
    return PollyPMConfig(
        project=ProjectSettings(
            name="Test",
            root_dir=tmp_path,
            base_dir=tmp_path / ".pollypm",
            logs_dir=tmp_path / ".pollypm/logs",
            snapshots_dir=tmp_path / ".pollypm/snapshots",
            state_db=tmp_path / ".pollypm/state.db",
        ),
        pollypm=PollyPMSettings(
            controller_account="claude_main",
            failover_enabled=True,
            failover_accounts=["claude_backup"],
            failover_usage_threshold_pct=85,
        ),
        accounts={
            "claude_main": AccountConfig(
                name="claude_main",
                provider=ProviderKind.CLAUDE,
                home=tmp_path / "homes" / "claude_main",
            ),
            "claude_backup": AccountConfig(
                name="claude_backup",
                provider=ProviderKind.CLAUDE,
                home=tmp_path / "homes" / "claude_backup",
            ),
        },
        sessions={
            "operator": SessionConfig(
                name="operator",
                role="operator-pm",
                provider=ProviderKind.CLAUDE,
                account="claude_main",
                cwd=tmp_path,
            )
        },
        projects={},
    )


def test_proactive_failover_switches_operator_and_emits_audit_event(tmp_path: Path) -> None:
    config = _config(tmp_path)
    msg_store = _MsgStore()
    switched: list[tuple[str, str]] = []

    summary = _apply_proactive_controller_failover(
        tmp_path / "pollypm.toml",
        config,
        ProactiveFailoverDecision(
            action="switch",
            primary_account="claude_main",
            current_account="claude_main",
            selected_account="claude_backup",
            reason="used_pct_threshold",
            threshold_pct=85,
            candidates_evaluated=1,
        ),
        msg_store=msg_store,
        switcher=lambda session_name, account_name: switched.append(
            (session_name, account_name)
        ),
    )

    assert switched == [("operator", "claude_backup")]
    assert summary["applied"] is True
    assert msg_store.events[-1]["subject"] == "account.failover.proactive"
    assert msg_store.events[-1]["payload"] == {
        "from": "claude_main",
        "to": "claude_backup",
        "reason": "used_pct_threshold",
        "threshold": 85,
        "current": "claude_main",
        "message": (
            "Proactive account failover claude_main -> claude_backup "
            "(used_pct_threshold, threshold=85%)"
        ),
    }
    assert msg_store.alerts[-1] == (
        "pollypm",
        "proactive_failover_active",
        "info",
        (
            "Primary account claude_main is at or above 85% usage; "
            "operator is using claude_backup."
        ),
    )
    assert (
        "pollypm",
        "proactive_failover_no_capacity",
        "auto:account.usage_refresh",
    ) in msg_store.cleared


def test_proactive_failover_alerts_when_no_account_is_under_threshold(tmp_path: Path) -> None:
    config = _config(tmp_path)
    msg_store = _MsgStore()

    summary = _apply_proactive_controller_failover(
        tmp_path / "pollypm.toml",
        config,
        ProactiveFailoverDecision(
            action="alert",
            primary_account="claude_main",
            current_account="claude_main",
            selected_account=None,
            reason="no_failover_account_below_threshold",
            threshold_pct=85,
            candidates_evaluated=1,
        ),
        msg_store=msg_store,
        switcher=lambda *_args: None,
    )

    assert summary["action"] == "alert"
    assert msg_store.alerts[-1][0:3] == (
        "pollypm",
        "proactive_failover_no_capacity",
        "warn",
    )
    assert "no failover account is below" in msg_store.alerts[-1][3]
    assert msg_store.events[-1]["payload"]["to"] is None


def test_proactive_failover_return_clears_active_alert(tmp_path: Path) -> None:
    config = _config(tmp_path)
    msg_store = _MsgStore()
    switched: list[tuple[str, str]] = []

    summary = _apply_proactive_controller_failover(
        tmp_path / "pollypm.toml",
        config,
        ProactiveFailoverDecision(
            action="return",
            primary_account="claude_main",
            current_account="claude_backup",
            selected_account="claude_main",
            reason="primary_below_threshold",
            threshold_pct=85,
            candidates_evaluated=0,
        ),
        msg_store=msg_store,
        switcher=lambda session_name, account_name: switched.append(
            (session_name, account_name)
        ),
    )

    assert summary["applied"] is True
    assert switched == [("operator", "claude_main")]
    assert (
        "pollypm",
        "proactive_failover_active",
        "auto:account.usage_refresh",
    ) in msg_store.cleared
    assert msg_store.events[-1]["payload"]["from"] == "claude_backup"
    assert msg_store.events[-1]["payload"]["to"] == "claude_main"
