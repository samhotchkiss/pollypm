"""Critical error alert + desktop notification bridge.

Contract:
- Inputs: ``logging.LogRecord`` objects at ``ERROR`` or higher.
- Outputs: durable ``error_log`` alerts in the store plus best-effort desktop
  notifications on supported hosts.
- Side effects: writes alert rows via the shared Store singleton and shells out
  to platform notification tools when available.
- Invariants: one module owns critical-error fanout; callers only install the
  handler and keep logging normally.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from typing import TYPE_CHECKING, Protocol, Sequence

if TYPE_CHECKING:
    from pollypm.store.protocol import Store


_SESSION_NAME = "error_log"
_ALERT_SEVERITY = "critical"
_MAX_MESSAGE_LENGTH = 280


@dataclass(frozen=True, slots=True)
class CriticalErrorNotification:
    """Normalized notification payload derived from one log record."""

    alert_type: str
    title: str
    message: str


class DesktopNotifier(Protocol):
    """Narrow desktop-notification contract for critical errors."""

    name: str

    def is_available(self) -> bool: ...

    def notify(self, *, title: str, body: str) -> None: ...


class MacOsDesktopNotifier:
    name = "macos"

    def is_available(self) -> bool:
        return sys.platform == "darwin" and shutil.which("osascript") is not None

    def notify(self, *, title: str, body: str) -> None:
        script = (
            'display notification "' + _escape_applescript(body) + '" '
            'with title "' + _escape_applescript(title) + '" '
            'sound name "Glass"'
        )
        subprocess.run(
            ["osascript", "-e", script],
            check=False,
            timeout=2.0,
            capture_output=True,
        )


class LinuxNotifySendNotifier:
    name = "notify-send"

    def is_available(self) -> bool:
        return sys.platform.startswith("linux") and shutil.which("notify-send") is not None

    def notify(self, *, title: str, body: str) -> None:
        subprocess.run(
            ["notify-send", "--app-name", "PollyPM", title, body],
            check=False,
            timeout=2.0,
            capture_output=True,
        )


def build_critical_error_notification(
    record: logging.LogRecord,
) -> CriticalErrorNotification | None:
    """Convert an ``ERROR``/``CRITICAL`` log record into an alert payload."""
    if record.levelno < logging.ERROR:
        return None
    message = _normalize_message(record.getMessage())
    if not message:
        return None
    return CriticalErrorNotification(
        alert_type=f"critical_error:{_record_signature(record, message)}",
        title=_notification_title(message),
        message=message,
    )


def load_critical_error_store(config_path: Path | None = None) -> "Store":
    """Resolve the shared Store singleton for alert writes."""
    from pollypm.config import DEFAULT_CONFIG_PATH, load_config
    from pollypm.store.registry import get_store

    config = load_config(config_path or DEFAULT_CONFIG_PATH)
    return get_store(config)


class CriticalErrorNotificationHandler(logging.Handler):
    """Mirror critical log records into durable alerts + desktop banners."""

    def __init__(
        self,
        *,
        config_path: Path | None = None,
        store_loader=load_critical_error_store,
        notifiers: Sequence[DesktopNotifier] | None = None,
    ) -> None:
        super().__init__(level=logging.ERROR)
        self.config_path = config_path
        self._store_loader = store_loader
        self._notifiers = tuple(
            notifiers
            if notifiers is not None
            else (MacOsDesktopNotifier(), LinuxNotifySendNotifier())
        )
        self._sent_signatures: set[str] = set()
        self._sent_lock = threading.Lock()
        self._emit_guard = threading.local()

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self._emit_guard, "active", False):
            return
        notification = build_critical_error_notification(record)
        if notification is None:
            return
        self._emit_guard.active = True
        try:
            self._write_alert(notification)
            if self._mark_first_delivery(notification.alert_type):
                self._send_desktop(notification)
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._emit_guard.active = False

    def _write_alert(self, notification: CriticalErrorNotification) -> None:
        store = self._store_loader(self.config_path)
        store.upsert_alert(
            _SESSION_NAME,
            notification.alert_type,
            _ALERT_SEVERITY,
            notification.message,
        )

    def _mark_first_delivery(self, alert_type: str) -> bool:
        with self._sent_lock:
            if alert_type in self._sent_signatures:
                return False
            self._sent_signatures.add(alert_type)
            return True

    def _send_desktop(self, notification: CriticalErrorNotification) -> None:
        for notifier in self._notifiers:
            try:
                if not notifier.is_available():
                    continue
                notifier.notify(
                    title=notification.title,
                    body=notification.message,
                )
            except Exception:  # noqa: BLE001
                continue


def _notification_title(message: str) -> str:
    lower = message.lower()
    if any(token in lower for token in ("capacity", "exhausted", "auth", "signed out", "relogin")):
        return "PollyPM: Account issue"
    if any(token in lower for token in ("pane dead", "session died", "missing window", "worker died")):
        return "PollyPM: Session issue"
    return "PollyPM: Critical error"


def _normalize_message(message: str) -> str:
    text = " ".join((message or "").split())
    if len(text) <= _MAX_MESSAGE_LENGTH:
        return text
    return text[: _MAX_MESSAGE_LENGTH - 1] + "..."


def _record_signature(record: logging.LogRecord, message: str) -> str:
    payload = f"{record.name}\n{message}".encode("utf-8", "replace")
    return hashlib.sha1(payload).hexdigest()[:12]


def _escape_applescript(raw: str) -> str:
    return raw.replace("\\", "\\\\").replace('"', '\\"')


__all__ = [
    "CriticalErrorNotification",
    "CriticalErrorNotificationHandler",
    "DesktopNotifier",
    "LinuxNotifySendNotifier",
    "MacOsDesktopNotifier",
    "build_critical_error_notification",
    "load_critical_error_store",
]
