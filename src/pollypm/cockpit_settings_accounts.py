"""Settings/accounts section helpers for the cockpit UI.

Contract:
- Input: one normalized account row from ``SettingsData.accounts``.
- Output: account-detail markup plus the explicit action-button contract for
  the Settings -> Accounts section.
- Invariants: this module owns account-section rendering; ``cockpit_ui``
  owns event wiring and service calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from rich.markup import escape as _escape

from pollypm.tz import format_relative, format_time


@dataclass(frozen=True, slots=True)
class SettingsAccountAction:
    button_id: str
    label: str
    variant: str = "default"


SETTINGS_ACCOUNT_ACTIONS: tuple[SettingsAccountAction, ...] = (
    SettingsAccountAction(
        button_id="settings-account-add-claude",
        label="Add Claude",
        variant="primary",
    ),
    SettingsAccountAction(
        button_id="settings-account-add-codex",
        label="Add Codex",
    ),
    SettingsAccountAction(
        button_id="settings-account-refresh-usage",
        label="Refresh Usage",
    ),
    SettingsAccountAction(
        button_id="settings-account-make-controller",
        label="Make Controller",
    ),
    SettingsAccountAction(
        button_id="settings-account-reassign-sessions",
        label="Reassign Sessions",
    ),
    SettingsAccountAction(
        button_id="settings-account-remove",
        label="Remove",
        variant="error",
    ),
)


def render_settings_account_detail(selected: Mapping[str, object]) -> str:
    sep = "[dim]" + "\u2500" * 40 + "[/dim]"
    dot = str(selected.get("status_dot") or "\u25cf")
    colour = str(selected.get("status_colour") or "#6b7a88")
    lines = [
        f"[{colour}]{dot}[/{colour}] [b]{_escape(str(selected.get('key') or ''))}[/b]"
        f"  [dim]({_escape(str(selected.get('provider') or ''))})[/dim]",
        sep,
        f"[dim]Email:[/dim]      {_escape(str(selected.get('email') or '-'))}",
        f"[dim]Logged in:[/dim]  {'yes' if selected.get('logged_in') else 'no'}",
        f"[dim]Health:[/dim]     {_escape(str(selected.get('health') or '-'))}",
        f"[dim]Plan:[/dim]       {_escape(str(selected.get('plan') or '-'))}",
        f"[dim]Usage:[/dim]      {_escape(str(selected.get('usage_summary') or '-'))}",
        f"[dim]Controller:[/dim] {'yes' if selected.get('is_controller') else 'no'}",
        f"[dim]Failover:[/dim]   {_render_failover(selected.get('failover_pos'))}",
        f"[dim]Home:[/dim]       {_escape(str(selected.get('home') or '-'))}",
        f"[dim]Isolation:[/dim]  {_escape(str(selected.get('isolation_status') or '-'))}",
        f"[dim]Storage:[/dim]    {_escape(str(selected.get('auth_storage') or '-'))}",
    ]
    if selected.get("remaining_pct") is not None:
        lines.append(f"[dim]Remaining:[/dim]  {selected['remaining_pct']}%")
    if selected.get("used_pct") is not None:
        lines.append(f"[dim]Used:[/dim]       {selected['used_pct']}%")
    if selected.get("period_label"):
        lines.append(
            f"[dim]Window:[/dim]     {_escape(str(selected['period_label']))}"
        )
    if selected.get("reset_at"):
        lines.append(
            f"[dim]Resets:[/dim]     {_escape(str(selected['reset_at']))}"
        )
    sampled = _render_usage_updated_at(selected.get("usage_updated_at"))
    if sampled:
        lines.append(f"[dim]Sampled:[/dim]    {sampled}")
    if selected.get("available_at"):
        lines.append(
            f"[dim]Available:[/dim]  {_escape(str(selected['available_at']))}"
        )
    if selected.get("access_expires_at"):
        lines.append(
            f"[dim]Expires:[/dim]    {_escape(str(selected['access_expires_at']))}"
        )
    if selected.get("reason"):
        lines.extend(
            [sep, f"[dim]Reason:[/dim]     {_escape(str(selected['reason']))}"]
        )
    usage_raw_text = str(selected.get("usage_raw_text") or "").strip()
    if usage_raw_text:
        snippet = usage_raw_text.splitlines()[:6]
        if snippet:
            lines.append(sep)
            lines.append("[dim]Latest usage snapshot:[/dim]")
            lines.extend(f"  {_escape(line)}" for line in snippet)
    return "\n".join(lines)


def _render_usage_updated_at(value: object) -> str:
    if not value:
        return ""
    iso = str(value)
    absolute = format_time(iso)
    relative = format_relative(iso)
    if absolute and relative:
        return f"{absolute} · {relative}"
    return absolute or relative


def _render_failover(value: object) -> str:
    if value in (None, "", 0):
        return "no"
    return f"#{value}"


__all__ = [
    "SETTINGS_ACCOUNT_ACTIONS",
    "SettingsAccountAction",
    "render_settings_account_detail",
]
