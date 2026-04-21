from __future__ import annotations

import logging
from pathlib import Path

from pollypm.error_log import install
from pollypm.error_notifications import (
    CriticalErrorNotificationHandler,
    build_critical_error_notification,
)


def test_build_critical_error_notification_ignores_warning() -> None:
    record = logging.LogRecord(
        name="demo",
        level=logging.WARNING,
        pathname=__file__,
        lineno=10,
        msg="just a warning",
        args=(),
        exc_info=None,
    )

    assert build_critical_error_notification(record) is None


def test_handler_upserts_alert_and_sends_desktop_notification() -> None:
    store_calls: list[tuple[str, str, str, str]] = []
    desktop_calls: list[tuple[str, str]] = []

    class FakeStore:
        def upsert_alert(
            self,
            session_name: str,
            alert_type: str,
            severity: str,
            message: str,
        ) -> None:
            store_calls.append((session_name, alert_type, severity, message))

    class FakeNotifier:
        name = "fake"

        def is_available(self) -> bool:
            return True

        def notify(self, *, title: str, body: str) -> None:
            desktop_calls.append((title, body))

    handler = CriticalErrorNotificationHandler(
        store_loader=lambda _config_path=None: FakeStore(),
        notifiers=(FakeNotifier(),),
    )
    record = logging.LogRecord(
        name="pollypm.supervisor",
        level=logging.ERROR,
        pathname=__file__,
        lineno=23,
        msg="Account main exhausted - switch to backup",
        args=(),
        exc_info=None,
    )

    handler.emit(record)

    assert len(store_calls) == 1
    session_name, alert_type, severity, message = store_calls[0]
    assert session_name == "error_log"
    assert alert_type.startswith("critical_error:")
    assert severity == "critical"
    assert message == "Account main exhausted - switch to backup"
    assert desktop_calls == [
        (
            "PollyPM: Account issue",
            "Account main exhausted - switch to backup",
        )
    ]


def test_handler_dedupes_repeated_desktop_notifications() -> None:
    desktop_calls: list[tuple[str, str]] = []

    class FakeStore:
        def upsert_alert(self, *_args) -> None:
            return None

    class FakeNotifier:
        name = "fake"

        def is_available(self) -> bool:
            return True

        def notify(self, *, title: str, body: str) -> None:
            desktop_calls.append((title, body))

    handler = CriticalErrorNotificationHandler(
        store_loader=lambda _config_path=None: FakeStore(),
        notifiers=(FakeNotifier(),),
    )
    record = logging.LogRecord(
        name="pollypm.supervisor",
        level=logging.ERROR,
        pathname=__file__,
        lineno=54,
        msg="worker session died while switching providers",
        args=(),
        exc_info=None,
    )

    handler.emit(record)
    handler.emit(record)

    assert desktop_calls == [
        (
            "PollyPM: Session issue",
            "worker session died while switching providers",
        )
    ]


def test_install_adds_notification_handler_once(tmp_path: Path) -> None:
    root = logging.getLogger()
    old_handlers = list(root.handlers)
    old_level = root.level
    try:
        root.handlers = []
        install(process_label="test", path=tmp_path / "errors.log")
        install(process_label="test", path=tmp_path / "errors.log")
        assert sum(
            1
            for handler in root.handlers
            if getattr(handler, "_pollypm_error_handler", False)
        ) == 1
        assert sum(
            1
            for handler in root.handlers
            if getattr(handler, "_pollypm_error_notification_handler", False)
        ) == 1
    finally:
        for handler in list(root.handlers):
            try:
                handler.close()
            except Exception:  # noqa: BLE001
                pass
        root.handlers = old_handlers
        root.setLevel(old_level)
