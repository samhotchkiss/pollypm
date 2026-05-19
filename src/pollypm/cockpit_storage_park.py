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

    Returns ``True`` when the break-pane actually ran (or no duplicate
    blocked it), ``False`` when the helper refused to break because a
    live duplicate already owns the canonical name.

    Behaviour:
      * If the storage closet already holds a live window of the same
        name → skip ``break-pane``, kill ``source_pane_id`` (it would
        otherwise leak as an unparented pane after the cockpit reclaims
        its real estate), emit ``cockpit.park_skipped_existing`` audit,
        and return ``False``.  Callers MUST treat this as a successful
        park: the storage window is already the persistent home.
      * If only dead-named duplicates exist → kill the dead windows so
        break-pane re-occupies the canonical name, then break.
      * No duplicates → break unconditionally.

    The helper centralizes the #1631/#1635 fix so every break-pane site
    in the cockpit gets the same dup-check.  See module docstring for
    the full list of historical bypass sites.
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
        # Persistent home already owns the canonical name.  Refuse the
        # break-pane.  The caller's surviving cockpit pane (the one we
        # would have parked) is now an orphan duplicate of the storage
        # window's process — kill it so it doesn't pile up as garbage.
        try:
            tmux.kill_pane(source_pane_id)
        except Exception:  # noqa: BLE001
            pass
        if audit_emit is not None:
            try:
                audit_emit(
                    "cockpit.park_skipped_existing",
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
                        "reason": "live_existing_storage_window",
                    },
                )
            except Exception:  # noqa: BLE001
                pass
        return False

    for window in dead_existing:
        try:
            tmux.kill_window(f"{storage_session}:{getattr(window, 'index', '')}")
        except Exception:  # noqa: BLE001
            continue

    tmux.break_pane(source_pane_id, storage_session, window_name)
    return True
