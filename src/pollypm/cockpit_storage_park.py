"""Idempotent ``tmux break-pane`` into the storage-closet (#1631 follow-up).

PollyPM has historically had multiple call sites that ``tmux break-pane``
a cockpit right-pane back into the storage closet under a fixed window
name (``architect-<project>``, ``pm-operator``, ``worker-<project>``,
etc.).  When such a window already existed in storage — typically because
a prior park collision left the canonical name occupied — the bare
``break-pane`` silently created a SECOND window with the same name.  The
next rail mount then had two same-named candidates to choose between,
and the user-visible damage from #1631 (conversation wipe on PM Chat
re-entry) followed.

#1635 fixed this for one call site (`_park_mounted_session`), but three
other paths still bypass the idempotency:

* ``cockpit_window_manager.park_live_to_storage`` — invoked from the
  ``show_static`` flow when ``park=`` is supplied.
* ``core/console_window.py`` — invoked when the cockpit console window
  exits and the surviving worker pane is parked back.
* ``cockpit_rail.ensure_cockpit_layout`` — invoked when only the worker
  pane survived a rail crash and needs parking back before the rail can
  be respawned.

This module exposes :func:`safe_break_pane_to_storage`, a pure helper
that all four paths can share.  It encapsulates the dup-check logic so
the mount-side selector (``_select_storage_window_for_mount``) never
sees more than one live window per name, eliminating the wipe surface.

The helper is intentionally tmux-shape agnostic: callers pass a
``tmux`` client plus a small audit-emit callback, and the helper does
the rest.  Pure-function design makes testing without tmux trivial.

Live-duplicate policy (#1955, 2026-05-20)
-----------------------------------------
The original helper, on finding a same-named LIVE window in storage,
killed the caller's ``source_pane_id`` and refused the break-pane —
treating the storage window as the persistent home.

In practice this is wrong for the rail-navigation case: the cockpit
pane is the user's active conversation (they just unmounted from it by
clicking a different rail item), and any pre-existing live storage
window with the same canonical name is a stale orphan from a previous
session.  Killing the cockpit pane wipes the conversation the user
just had open.

The helper now kills the storage orphan instead and breaks the
cockpit pane into storage under the canonical name.  The next mount
picks up the user's real conversation.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

# Live-provider definitions mirror ``cockpit_rail._storage_window_is_live``
# and ``_select_storage_window_for_mount``.  Keeping the literal set
# duplicated here (rather than importing from cockpit_rail) avoids the
# import cycle that ``cockpit_window_manager`` would otherwise hit.
_LIVE_PROVIDER_COMMANDS: frozenset[str] = frozenset({"node", "claude", "codex"})


def _window_is_live(window: Any) -> bool:
    """Return True iff ``window``'s pane looks like a live provider.

    Mirrors :meth:`cockpit_rail.CockpitRouter._storage_window_is_live`.
    A window whose pane reports a version-string command (e.g.
    ``2.1.144``) is treated as live — those panes are the Claude CLI
    after its first turn, and they hold real conversation state.
    """
    if getattr(window, "pane_dead", False):
        return False
    cmd = getattr(window, "pane_current_command", "") or ""
    if cmd in _LIVE_PROVIDER_COMMANDS:
        return True
    if cmd and all(c.isdigit() or c == "." for c in cmd):
        return True
    return False


def safe_break_pane_to_storage(
    tmux: Any,
    *,
    source_pane_id: str,
    storage_session: str,
    window_name: str,
    audit_emit: Callable[[str, str, dict[str, Any]], None] | None = None,
    subject: str | None = None,
) -> bool:
    """Break ``source_pane_id`` into ``storage_session`` only if safe.

    Returns ``True`` when the break-pane actually ran, ``False`` only
    when ``break-pane`` itself raised.

    Behaviour:
      * If the storage closet already holds a live window of the same
        name → that window is an ORPHAN (the user can only be conversing
        with one pane at a time; ``source_pane_id`` is the cockpit's
        live mount, which is by definition the user's current session).
        Kill the orphan, then break-pane ``source_pane_id`` into storage
        under ``window_name``.  Emits ``cockpit.park_killed_orphan``
        audit before the kill so forensics can correlate.
      * If only dead-named duplicates exist → kill the dead windows so
        break-pane re-occupies the canonical name, then break.
      * No duplicates → break unconditionally.

    Why the live-duplicate path changed (#1631 → #1955 fix):
    The original #1631 helper assumed the storage window held the
    user's conversation and skipped the break-pane (killing
    ``source_pane_id`` as collateral).  That was correct when the
    cockpit had spawned a fresh placeholder mid-park-collision.  It is
    WRONG when the user has been actively typing into the cockpit
    pane — which is the rail-navigation case: the cockpit pane IS the
    user's session, and the storage duplicate is the stale orphan.
    Trust the cockpit pane (it is by construction the active mount)
    and kill the storage orphan instead.  See ``feedback_destructive
    _refusal`` memory — the cockpit pane is the source of truth at the
    moment of the park because the rail just unmounted from it.

    The helper centralizes the #1631/#1635/#1955 logic so every
    break-pane site in the cockpit gets the same handling.  See module
    docstring for the full list of historical bypass sites.
    """
    try:
        storage_windows = tmux.list_windows(storage_session)
    except Exception:  # noqa: BLE001
        storage_windows = []

    existing_same_name = [
        w for w in storage_windows
        if getattr(w, "name", None) == window_name
    ]
    live_existing = [w for w in existing_same_name if _window_is_live(w)]
    dead_existing = [w for w in existing_same_name if not _window_is_live(w)]

    if live_existing:
        # Live duplicate in storage is an ORPHAN — the user has been
        # interacting with ``source_pane_id`` (the cockpit mount), so
        # the storage window cannot be the same conversation.  Kill
        # the orphan(s) so the user's actual conversation can claim
        # the canonical window name on break-pane below.
        if audit_emit is not None:
            try:
                audit_emit(
                    "cockpit.park_killed_orphan",
                    "warn",
                    {
                        "subject": subject or window_name,
                        "window_name": window_name,
                        "storage_session": storage_session,
                        "source_pane_id": source_pane_id,
                        "live_duplicate_indices": [
                            getattr(w, "index", None) for w in live_existing
                        ],
                        "dead_duplicate_indices": [
                            getattr(w, "index", None) for w in dead_existing
                        ],
                        "reason": "killed_orphan_to_preserve_active_mount",
                    },
                )
            except Exception:  # noqa: BLE001
                pass
        for window in live_existing:
            try:
                tmux.kill_window(
                    f"{storage_session}:{getattr(window, 'index', '')}"
                )
            except Exception:  # noqa: BLE001
                continue

    for window in dead_existing:
        try:
            tmux.kill_window(f"{storage_session}:{getattr(window, 'index', '')}")
        except Exception:  # noqa: BLE001
            continue

    try:
        tmux.break_pane(source_pane_id, storage_session, window_name)
    except Exception:  # noqa: BLE001
        return False
    return True
