"""Plugin wiring for ``human_notify``.

Registers a listener on the ``TaskAssignmentEvent`` bus, assembles
the adapter chain (macOS + webhook + cockpit + anything registered
via the ``pollypm.human_notifier`` entry-point group), and fans
out each ``ActorType.HUMAN`` event through the dispatcher.

No new job handlers or roster entries — human-addressed pushes
ride the same synchronous bus hook ``task_assignment_notify`` uses
for worker/architect pings. That means a human push arrives in the
same tick as a worker push does, not on a separate cadence.
"""

from __future__ import annotations

import logging
from typing import Any

from pollypm.plugin_api.v1 import Capability, PluginAPI, PollyPMPlugin
from pollypm.plugins_builtin.human_notify.cockpit import CockpitNotifyAdapter
from pollypm.plugins_builtin.human_notify.dispatcher import dispatch
from pollypm.plugins_builtin.human_notify.macos import MacOsNotifyAdapter
from pollypm.plugins_builtin.human_notify.webhook import (
    from_config as _webhook_from_config,
)
from pollypm.work import task_assignment as _task_assignment_bus

logger = logging.getLogger(__name__)

# Initialized in ``_initialize`` — guarded by a ``None`` check so
# events delivered before initialize runs (e.g. during plugin-host
# test teardown) are silently dropped rather than crashing.
_ADAPTERS: list[Any] | None = None


def _load_adapters(api: PluginAPI) -> list[Any]:
    """Assemble the default adapter chain.

    Order matches "most specific first, fallback last" — the
    dispatcher runs every adapter regardless of order, but stable
    ordering makes log lines predictable in the test suite.
    """
    adapters: list[Any] = []

    # 1. macOS Notification Center — always tried on Darwin.
    adapters.append(MacOsNotifyAdapter())

    # 2. Webhook — opt-in via config. Constructed even when no URL
    #    is set so ``is_available()`` can signal "skip" uniformly.
    webhook_cfg = _resolve_webhook_config(api)
    adapters.append(_webhook_from_config(webhook_cfg))

    # 3. Third-party adapters via entry-point group. Registered
    #    plugins bring their own ``is_available`` / ``notify`` — we
    #    append verbatim, no Protocol check beyond what Python's
    #    ``runtime_checkable`` already enforces at dispatch time.
    adapters.extend(_load_entrypoint_adapters())

    # 4. Cockpit fallback — always last so any earlier adapter that
    #    delivered successfully still leaves a cockpit trace.
    adapters.append(CockpitNotifyAdapter(_resolve_store(api)))

    return adapters


def _resolve_webhook_config(api: PluginAPI) -> dict:
    """Pull ``[human_notify.webhook]`` from pollypm.toml if the API exposes it.

    ``PluginAPI`` doesn't yet surface raw-TOML access, so we read
    from the config object if present. Returns ``{}`` on any
    resolution failure so the webhook adapter just disables itself.
    """
    try:
        config = getattr(api, "config", None)
        if config is None:
            return {}
        raw = getattr(config, "human_notify", None)
        if raw is None:
            # Fall through to direct TOML lookup for installs that
            # haven't added a typed config field yet.
            return _read_webhook_from_toml(config)
        webhook = getattr(raw, "webhook", None)
        if webhook is None:
            return {}
        return dict(webhook) if hasattr(webhook, "keys") else {}
    except Exception:  # noqa: BLE001
        logger.debug("human_notify: webhook config lookup failed", exc_info=True)
        return {}


def _read_webhook_from_toml(config: Any) -> dict:
    """Fallback: parse the raw pollypm.toml for ``[human_notify.webhook]``.

    Typed-config plumbing lags user-facing settings; we accept a
    TOML block today so users don't wait on a config-schema commit
    to get webhooks working.
    """
    try:
        import tomllib
    except ImportError:
        return {}
    config_path = getattr(config, "config_path", None)
    if config_path is None:
        return {}
    try:
        with open(config_path, "rb") as fh:
            raw = tomllib.load(fh)
    except Exception:  # noqa: BLE001
        return {}
    section = raw.get("human_notify", {})
    if not isinstance(section, dict):
        return {}
    webhook = section.get("webhook", {})
    return webhook if isinstance(webhook, dict) else {}


def _resolve_store(api: PluginAPI) -> Any | None:
    """Get a ``StateStore``-like handle for the cockpit fallback.

    Tries the unified Store first (per #349), falls back to the
    legacy ``StateStore`` on the config's state_db path.
    """
    try:
        from pollypm.store.registry import get_store

        config = getattr(api, "config", None)
        if config is not None:
            return get_store(config)
    except Exception:  # noqa: BLE001
        pass
    try:
        from pollypm.storage.state import StateStore

        config = getattr(api, "config", None)
        if config is None:
            return None
        return StateStore(config.project.state_db)
    except Exception:  # noqa: BLE001
        return None


def _load_entrypoint_adapters() -> list[Any]:
    """Iterate ``pollypm.human_notifier`` entry points and instantiate.

    Third-party packages register their adapter class under this
    group; we call the class with no args (the Protocol doesn't
    mandate constructor signature) and silently skip on any
    construction failure so one broken plugin can't take down the
    default chain.
    """
    try:
        from importlib.metadata import entry_points
    except ImportError:
        return []
    found: list[Any] = []
    try:
        eps = entry_points(group="pollypm.human_notifier")
    except Exception:  # noqa: BLE001
        return []
    for ep in eps:
        try:
            cls = ep.load()
            found.append(cls())
        except Exception:  # noqa: BLE001
            logger.warning(
                "human_notify: entry-point %r failed to load", ep.name,
                exc_info=True,
            )
    return found


def _in_process_listener(event) -> None:
    """Bus subscriber — dispatch HUMAN events; no-op for everything else.

    The dispatcher guards the actor_type filter itself; this
    listener just passes everything through. That keeps the filter
    logic in one place and makes the dispatcher unit-testable
    without a live bus.
    """
    if _ADAPTERS is None:
        return
    try:
        dispatch(event, _ADAPTERS)
    except Exception:  # noqa: BLE001
        logger.exception(
            "human_notify: dispatch failed for %s",
            getattr(event, "task_id", "?"),
        )


def _initialize(api: PluginAPI) -> None:
    """Wire the listener + adapter chain once per process.

    Idempotent: the underlying bus's ``register_listener`` de-dupes
    on identity, so multiple initialize calls (as in the plugin-host
    test harness) don't stack duplicates.
    """
    global _ADAPTERS
    _ADAPTERS = _load_adapters(api)
    _task_assignment_bus.register_listener(_in_process_listener)
    n_adapters = len(_ADAPTERS)
    adapter_word = "adapter" if n_adapters == 1 else "adapters"
    logger.info(
        "human_notify: initialized with %d %s: %s",
        n_adapters,
        adapter_word,
        ", ".join(getattr(a, "name", "?") for a in _ADAPTERS),
    )


plugin = PollyPMPlugin(
    name="human_notify",
    capabilities=(
        # ``hook`` is the Plugin-API-v1 capability kind for plugins
        # that observe an event bus rather than registering a factory
        # (provider, runtime, etc.). The adapter chain is internal
        # implementation — each adapter's third-party variant ships
        # as its own plugin via the ``pollypm.human_notifier``
        # entry-point group, not as a top-level capability here.
        Capability(kind="hook", name="human_notify.task_assignment"),
    ),
    initialize=_initialize,
)


__all__ = ["plugin"]
