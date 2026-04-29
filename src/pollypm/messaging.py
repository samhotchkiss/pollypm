"""Thin notification shim over the durable alerts system.

This module used to back the legacy inbox subsystem (threads, messages,
state machine). Everything the rest of the codebase cared about — "tell
the user that X happened" — is now served by
:meth:`pollypm.storage.state.StateStore.upsert_alert` plus
``record_event`` for audit trail.

Callers are primarily the ``itsalive`` plugin for deploy-lifecycle
notifications. Functions here are intentionally small wrappers so the
call sites read naturally ("notify deploy complete" not "upsert_alert
with severity info and these exact kwargs").
"""
from __future__ import annotations

from pathlib import Path


_SESSION = "itsalive"


def _alert(
    project_root: Path,
    *,
    alert_type: str,
    severity: str,
    message: str,
    event: str = "",
) -> None:
    """Raise a durable alert and record an audit event.

    Tolerant of missing state infrastructure — callers are notification
    paths and must never fail the work they were reporting on. Looks up
    the per-project config on a best-effort basis so alerts land in the
    right state DB even when a global config is absent.
    """
    try:
        from pollypm.config import DEFAULT_CONFIG_PATH, load_config

        config_path = project_root / "pollypm.toml"
        if not config_path.exists():
            config_path = DEFAULT_CONFIG_PATH
        config = load_config(config_path)
        # #349: writers land on the unified ``messages`` table via Store.
        from pollypm.store.registry import get_store

        store = get_store(config)
        try:
            store.upsert_alert(_SESSION, alert_type, severity, message)
            if event:
                from pollypm.events import (
                    activity_summary,
                )

                store.append_event(
                    scope=_SESSION,
                    sender=_SESSION,
                    subject=event,
                    payload={
                        "message": activity_summary(
                            summary=message,
                            severity=(
                                "critical" if severity in {"critical", "error"}
                                else "recommendation" if severity in {"warn", "warning"}
                                else "routine"
                            ),
                            verb=event,
                            subject=alert_type,
                        ),
                        "alert_type": alert_type,
                        "severity": severity,
                    },
                )
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()
    except Exception:  # noqa: BLE001 - notification must not break callers
        pass


def notify_deploy_verification_required(
    project_root: Path, *, subdomain: str, email: str, expires_at: str,
) -> None:
    """A pending deploy needs the user to click the verification email."""
    _alert(
        project_root,
        alert_type="itsalive_verification_required",
        severity="warn",
        message=(
            f"Deploy for {subdomain}.itsalive.co awaits verification — "
            f"email sent to {email}, expires {expires_at}."
        ),
        event="deploy_pending",
    )


def notify_deploy_expired(project_root: Path, *, subdomain: str, expires_at: str) -> None:
    """A pending deploy's verification window expired before the user clicked."""
    _alert(
        project_root,
        alert_type="itsalive_verification_expired",
        severity="warn",
        message=(
            f"Deploy for {subdomain}.itsalive.co expired at {expires_at}. "
            "Open the deploy panel and retry the deploy."
        ),
        event="deploy_expired",
    )


def notify_deploy_complete(project_root: Path, *, subdomain: str, domain: str) -> None:
    """Finalisation succeeded; the site is live."""
    _alert(
        project_root,
        alert_type="itsalive_deploy_complete",
        severity="info",
        message=f"https://{domain} is live — deploy for {subdomain} completed.",
        event="deploy_complete",
    )
