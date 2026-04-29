"""Public-API contract tests for ``task_assignment_notify`` (#939).

The plugin's :mod:`task_assignment_notify.api` module is the
sanctioned cross-boundary surface — both peer plugins (e.g.
``core_recurring``) and core runtime modules (work transitions,
cockpit rendering, heartbeat recovery) route through it instead of
reaching into :mod:`task_assignment_notify.handlers.sweep` or
:mod:`task_assignment_notify.resolver` directly.

Two shapes are pinned here:

1. **API completeness** — every symbol that core / peer plugins
   currently need is callable through ``api`` and resolves to the
   underlying private implementation. Removing a public name (or
   silently changing what it points at) must fail this test.

2. **Boundary enforcement** — no file under ``src/pollypm/`` outside
   the plugin itself imports from
   ``task_assignment_notify.handlers.*`` or
   ``task_assignment_notify.resolver``. The companion
   :mod:`tests.test_plugin_boundary_conformance` only catches
   plugin-to-plugin private imports; this test extends that contract
   to the core-to-plugin direction the issue (#939) flagged.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from pollypm.plugins_builtin.task_assignment_notify import api
from pollypm.plugins_builtin.task_assignment_notify import resolver
from pollypm.plugins_builtin.task_assignment_notify.handlers import sweep


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src" / "pollypm"
PLUGIN_ROOT = (
    SRC_ROOT / "plugins_builtin" / "task_assignment_notify"
)


# Every public symbol the plugin must expose, paired with the
# underlying private implementation it must trampoline to. New core
# callers must extend this list rather than imports from internals.
_PUBLIC_SURFACE: tuple[tuple[str, object, str], ...] = (
    ("DEDUPE_WINDOW_SECONDS", resolver, "DEDUPE_WINDOW_SECONDS"),
    ("RECENT_SWEEPER_PING_SECONDS", sweep, "RECENT_SWEEPER_PING_SECONDS"),
    (
        "SWEEPER_PING_CONTEXT_ENTRY_TYPE",
        sweep,
        "SWEEPER_PING_CONTEXT_ENTRY_TYPE",
    ),
    ("load_runtime_services", resolver, "load_runtime_services"),
    ("notify", resolver, "notify"),
    (
        "clear_alerts_for_cancelled_task",
        resolver,
        "clear_alerts_for_cancelled_task",
    ),
    (
        "clear_no_session_alert_for_task",
        resolver,
        "clear_no_session_alert_for_task",
    ),
    ("auto_claim_enabled_for_project", sweep, "_auto_claim_enabled_for_project"),
    ("build_event_for_task", sweep, "_build_event_for_task"),
    ("close_quietly", sweep, "_close_quietly"),
    ("open_project_work_service", sweep, "_open_project_work_service"),
    ("record_sweeper_ping", sweep, "_record_sweeper_ping"),
    ("recover_dead_claims", sweep, "_recover_dead_claims"),
)


@pytest.mark.parametrize(
    "public_name,source_module,source_name",
    _PUBLIC_SURFACE,
    ids=[entry[0] for entry in _PUBLIC_SURFACE],
)
def test_public_surface_is_complete(
    public_name: str, source_module: object, source_name: str,
) -> None:
    """Each name listed in the contract is reachable through ``api``
    and the underlying private symbol still exists.

    A failure means either:

    * the public surface lost a name a core module relies on, or
    * the plugin renamed/removed an internal symbol without updating
      the trampoline.

    Either way, fix the surface first — core must not be patched to
    chase the plugin's internals."""
    assert hasattr(api, public_name), (
        f"task_assignment_notify.api is missing public name "
        f"{public_name!r}"
    )
    assert hasattr(source_module, source_name), (
        f"backing implementation {source_module.__name__}.{source_name} "
        f"is missing — promote the new name into api.py instead of "
        f"removing it"
    )


def test_public_surface_listed_in_all() -> None:
    """``__all__`` documents the contract — drift between contract
    and listing is a silent reduction in surface."""
    expected = {entry[0] for entry in _PUBLIC_SURFACE}
    actual = set(api.__all__)
    missing = expected - actual
    extra = actual - expected
    assert not missing, (
        f"api.__all__ missing public names: {sorted(missing)}"
    )
    # Extras are not strictly a regression, but if you add a new
    # name to __all__ you must also add it to _PUBLIC_SURFACE so the
    # contract test pins the trampoline.
    assert not extra, (
        f"api.__all__ exports names not pinned by _PUBLIC_SURFACE: "
        f"{sorted(extra)}. Add them to the contract."
    )


def test_trampolines_resolve_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """The trampoline pattern (api.py docstring) requires that
    monkeypatching the source module propagates through the public
    surface. Pin the behaviour so refactors don't accidentally cache
    the bound function at import time."""
    sentinel = object()

    def fake_load_runtime_services(*_a: object, **_k: object) -> object:
        return sentinel

    monkeypatch.setattr(
        resolver, "load_runtime_services", fake_load_runtime_services,
    )
    assert api.load_runtime_services() is sentinel


# ---------------------------------------------------------------------------
# Boundary enforcement: core must not import plugin internals
# ---------------------------------------------------------------------------


_FORBIDDEN_PRIVATE_MODULE_PATTERN = re.compile(
    r"from\s+pollypm\.plugins_builtin\.task_assignment_notify"
    r"\.(?:handlers(?:\.[a-z_]+)?|resolver)\b",
)


def _iter_source_files() -> list[Path]:
    out: list[Path] = []
    for path in SRC_ROOT.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        out.append(path)
    return out


def _is_inside_plugin(path: Path) -> bool:
    try:
        path.relative_to(PLUGIN_ROOT)
    except ValueError:
        return False
    return True


def test_no_core_or_peer_imports_from_plugin_internals() -> None:
    """Anything outside ``task_assignment_notify`` must import via
    the ``api`` module. This catches both peer plugins (which
    :mod:`tests.test_plugin_boundary_conformance` already covers) and
    core runtime modules (the gap #939 cited).

    A failure means a caller — most likely something under
    ``pollypm/work``, ``pollypm/cockpit_*``, or
    ``pollypm/heartbeats`` — added back a direct import of
    ``handlers.sweep`` / ``resolver``. Promote whatever symbol it
    needs into ``api.py`` first, then update the caller.
    """
    offenders: list[str] = []
    for source_file in _iter_source_files():
        if _is_inside_plugin(source_file):
            continue
        text = source_file.read_text(encoding="utf-8")
        for match in _FORBIDDEN_PRIVATE_MODULE_PATTERN.finditer(text):
            line_no = text[: match.start()].count("\n") + 1
            rel = source_file.relative_to(REPO_ROOT).as_posix()
            offenders.append(f"{rel}:{line_no}: {match.group(0)}")
    assert offenders == [], (
        "Core / peer modules must import task_assignment_notify "
        "symbols from its public ``api`` module, not from "
        "``handlers.*`` / ``resolver``. Offenders:\n  - "
        + "\n  - ".join(offenders)
    )


def test_known_core_callers_use_public_api() -> None:
    """Spot-check that the three core callers cited in #939 import
    from ``api`` specifically — guards against a future refactor
    that leaves the boundary technically clean (no private import)
    but routes through some other ad-hoc shim."""
    targets = (
        SRC_ROOT / "work" / "service_transition_manager.py",
        SRC_ROOT / "cockpit_tasks.py",
        SRC_ROOT / "heartbeats" / "local.py",
    )
    for target in targets:
        text = target.read_text(encoding="utf-8")
        assert (
            "pollypm.plugins_builtin.task_assignment_notify.api"
            in text
        ), (
            f"{target.relative_to(REPO_ROOT).as_posix()} should import "
            f"from task_assignment_notify.api (#939)"
        )
