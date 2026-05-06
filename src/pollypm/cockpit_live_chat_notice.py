"""Transient cockpit notices for live chat failures.

The cockpit-send-key bridge runs in a short-lived CLI process, while the
visible rail is a separate Textual process. This module keeps the tiny shared
state contract for non-input live-chat notices in one place.
"""

from __future__ import annotations

import time
from collections.abc import Mapping

LIVE_CHAT_NETWORK_DEAD_PROMPT_ACTIVE_KEY = "live_chat_network_dead_prompt_active"
LIVE_CHAT_NETWORK_DEAD_NOTICE_MESSAGE_KEY = "live_chat_network_dead_notice_message"
LIVE_CHAT_NETWORK_DEAD_NOTICE_UNTIL_KEY = "live_chat_network_dead_notice_until"
LIVE_CHAT_NETWORK_DEAD_NOTICE_TTL_SECONDS = 10.0
LIVE_CHAT_NETWORK_DEAD_TMUX_DISPLAY_MS = "10000"
LIVE_CHAT_NETWORK_DEAD_TMUX_MESSAGE = (
    "PollyPM chat failed: network unreachable. "
    "Retry after checking connection."
)
LIVE_CHAT_NETWORK_DEAD_RAIL_MESSAGE = "Chat failed: retry/check net"


def install_live_chat_network_dead_notice(
    router: object,
    *,
    now: float | None = None,
) -> None:
    """Persist a transient rail notice without touching the live chat input."""
    try:
        load_state = getattr(router, "_load_state")
        write_state = getattr(router, "_write_state")
        state = load_state()
        if not isinstance(state, dict):
            return
        deadline = (time.time() if now is None else now) + (
            LIVE_CHAT_NETWORK_DEAD_NOTICE_TTL_SECONDS
        )
        state[LIVE_CHAT_NETWORK_DEAD_NOTICE_MESSAGE_KEY] = (
            LIVE_CHAT_NETWORK_DEAD_RAIL_MESSAGE
        )
        state[LIVE_CHAT_NETWORK_DEAD_NOTICE_UNTIL_KEY] = deadline
        state.pop(LIVE_CHAT_NETWORK_DEAD_PROMPT_ACTIVE_KEY, None)
        write_state(state)
    except Exception:  # noqa: BLE001
        return
    _bump_state_epoch()


def current_live_chat_network_dead_notice(
    state: Mapping[str, object],
    *,
    now: float | None = None,
) -> str | None:
    message = state.get(LIVE_CHAT_NETWORK_DEAD_NOTICE_MESSAGE_KEY)
    until = state.get(LIVE_CHAT_NETWORK_DEAD_NOTICE_UNTIL_KEY)
    if not isinstance(message, str) or not message:
        return None
    try:
        deadline = float(until)
    except (TypeError, ValueError):
        return None
    if deadline <= (time.time() if now is None else now):
        return None
    return message


def live_chat_network_dead_notice_present(state: Mapping[str, object]) -> bool:
    return (
        LIVE_CHAT_NETWORK_DEAD_NOTICE_MESSAGE_KEY in state
        or LIVE_CHAT_NETWORK_DEAD_NOTICE_UNTIL_KEY in state
    )


def clear_live_chat_network_dead_notice(router: object) -> None:
    try:
        load_state = getattr(router, "_load_state")
        write_state = getattr(router, "_write_state")
        state = load_state()
        if not isinstance(state, dict):
            return
        changed = False
        for key in (
            LIVE_CHAT_NETWORK_DEAD_NOTICE_MESSAGE_KEY,
            LIVE_CHAT_NETWORK_DEAD_NOTICE_UNTIL_KEY,
            LIVE_CHAT_NETWORK_DEAD_PROMPT_ACTIVE_KEY,
        ):
            if key in state:
                state.pop(key, None)
                changed = True
        if not changed:
            return
        write_state(state)
    except Exception:  # noqa: BLE001
        return
    _bump_state_epoch()


def _bump_state_epoch() -> None:
    try:
        from pollypm.state_epoch import bump

        bump()
    except Exception:  # noqa: BLE001
        return
