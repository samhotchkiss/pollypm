"""Persisted undo/history metadata for cockpit settings screens.

Contract:
- Inputs: a label plus a restore callable, or a structured history
  event with payload.
- Outputs: a 24h undo record and a rolling JSON history at
  ``~/.pollypm/settings-history.json``.
- Side effects: reads/writes the history file atomically.
- Invariants: callers can ask this helper whether an undo window is
  still open without owning expiry math, and can derive rationale text
  from the latest relevant settings event.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence
from uuid import uuid4

from pollypm.atomic_io import atomic_write_json


_HISTORY_FILENAME = "settings-history.json"
_MAX_HISTORY_AGE = timedelta(hours=24)
_MAX_HISTORY_ENTRIES = 200


@dataclass(slots=True)
class UndoAction:
    label: str
    expires_at: datetime
    apply: Callable[[], None]
    entry_id: str = ""
    kind: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SettingsHistoryEntry:
    entry_id: str
    kind: str
    label: str
    created_at: datetime
    expires_at: datetime
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "kind": self.kind,
            "label": self.label,
            "created_at": self.created_at.astimezone(timezone.utc).isoformat(),
            "expires_at": self.expires_at.astimezone(timezone.utc).isoformat(),
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SettingsHistoryEntry" | None:
        try:
            entry_id = str(raw.get("entry_id") or "").strip() or uuid4().hex
            kind = str(raw.get("kind") or "").strip()
            label = str(raw.get("label") or "").strip()
            created_at = _parse_datetime(raw.get("created_at"))
            expires_at = _parse_datetime(raw.get("expires_at"))
        except (TypeError, ValueError):
            return None
        if not kind or not label or created_at is None or expires_at is None:
            return None
        payload = raw.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}
        return cls(
            entry_id=entry_id,
            kind=kind,
            label=label,
            created_at=created_at,
            expires_at=expires_at,
            payload=dict(payload),
        )


def settings_history_path(home: Path | None = None) -> Path:
    base = home or Path.home()
    return base / ".pollypm" / _HISTORY_FILENAME


def make_undo_action(
    label: str,
    apply: Callable[[], None],
    *,
    hours: int = 24,
    entry_id: str = "",
    kind: str = "",
    payload: dict[str, Any] | None = None,
) -> UndoAction:
    return UndoAction(
        label=label,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=hours),
        apply=apply,
        entry_id=entry_id,
        kind=kind,
        payload=dict(payload or {}),
    )


def undo_expired(action: UndoAction | None) -> bool:
    return action is None or datetime.now(timezone.utc) > action.expires_at


def undo_expires_text(action: UndoAction) -> str:
    return action.expires_at.astimezone(timezone.utc).strftime("%H:%M UTC")


def record_settings_history(
    kind: str,
    label: str,
    payload: dict[str, Any] | None = None,
    *,
    hours: int = 24,
    path: Path | None = None,
) -> SettingsHistoryEntry:
    created_at = datetime.now(timezone.utc)
    entry = SettingsHistoryEntry(
        entry_id=uuid4().hex,
        kind=kind,
        label=label,
        created_at=created_at,
        expires_at=created_at + timedelta(hours=hours),
        payload=dict(payload or {}),
    )
    history = _prune_history(load_settings_history(path), now=created_at)
    history.append(entry)
    _write_history(history, path=path)
    return entry


def load_settings_history(path: Path | None = None) -> list[SettingsHistoryEntry]:
    history_path = path or settings_history_path()
    try:
        raw = json.loads(history_path.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    entries: list[SettingsHistoryEntry] = []
    for item in raw:
        if isinstance(item, dict):
            entry = SettingsHistoryEntry.from_dict(item)
            if entry is not None:
                entries.append(entry)
    return _prune_history(entries)


def consume_settings_history(entry_id: str, path: Path | None = None) -> None:
    if not entry_id:
        return
    history = [entry for entry in load_settings_history(path) if entry.entry_id != entry_id]
    _write_history(history, path=path)


def latest_settings_history_entry(
    *,
    kind: str | None = None,
    path: Path | None = None,
) -> SettingsHistoryEntry | None:
    history = load_settings_history(path)
    for entry in reversed(history):
        if kind is None or entry.kind == kind:
            return entry
    return None


def history_rationale_for_account(
    account_key: str,
    *,
    entries: Sequence[SettingsHistoryEntry] | None = None,
    path: Path | None = None,
    default_account: str | None = None,
) -> str | None:
    if not account_key:
        return None
    history = list(entries) if entries is not None else load_settings_history(path)
    for entry in reversed(history):
        payload = entry.payload
        stamp = entry.created_at.astimezone(timezone.utc).strftime("%H:%M UTC")
        if entry.kind in {"session.switch", "manual_switch"}:
            to_account = str(payload.get("to_account") or payload.get("account") or "")
            from_account = str(payload.get("from_account") or payload.get("previous_account") or "")
            session_name = str(payload.get("session_name") or "session")
            if account_key == to_account:
                return f"Recent manual switch: {session_name} moved to {to_account} at {stamp}."
            if account_key == from_account:
                return f"Recent manual switch: {session_name} moved away from {from_account} at {stamp}."
        if entry.kind in {"account.failover", "failover"}:
            if account_key == str(payload.get("account") or ""):
                state = "enabled" if bool(payload.get("enabled")) else "disabled"
                return f"Recent failover {state} for {account_key} at {stamp}."
        if entry.kind in {"account.controller", "default", "account.default"}:
            if account_key == str(payload.get("account") or ""):
                return f"Default account set to {account_key} at {stamp}."
    if default_account:
        if account_key == default_account:
            return f"Default account from config: {default_account}."
        return f"Default account from config: {default_account}."
    return "No recent manual-switch or failover event recorded."


def history_rationale_for_project(
    project_key: str,
    *,
    entries: Sequence[SettingsHistoryEntry] | None = None,
    path: Path | None = None,
) -> str | None:
    if not project_key:
        return None
    history = list(entries) if entries is not None else load_settings_history(path)
    for entry in reversed(history):
        payload = entry.payload
        stamp = entry.created_at.astimezone(timezone.utc).strftime("%H:%M UTC")
        if entry.kind == "project.tracked" and project_key == str(payload.get("project_key") or ""):
            state = "tracked" if bool(payload.get("enabled")) else "paused"
            return f"Project {project_key} was marked {state} at {stamp}."
    return None


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _prune_history(
    entries: Sequence[SettingsHistoryEntry],
    *,
    now: datetime | None = None,
) -> list[SettingsHistoryEntry]:
    current = now or datetime.now(timezone.utc)
    pruned = [
        entry
        for entry in entries
        if entry.expires_at > current and entry.created_at <= current
    ]
    if len(pruned) > _MAX_HISTORY_ENTRIES:
        pruned = pruned[-_MAX_HISTORY_ENTRIES:]
    return pruned


def _write_history(
    entries: Sequence[SettingsHistoryEntry],
    *,
    path: Path | None = None,
) -> None:
    history_path = path or settings_history_path()
    atomic_write_json(history_path, [entry.to_dict() for entry in entries])
