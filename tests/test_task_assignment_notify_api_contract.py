"""Task-assignment notification boundary contract tests (#939 / #1365).

The shared :mod:`pollypm.task_assignment_notify` module is the sanctioned
cross-boundary surface. Core runtime modules and peer plugins route through
it instead of importing :mod:`task_assignment_notify.handlers.sweep`,
:mod:`task_assignment_notify.resolver`, or the plugin compatibility
``api`` module directly.

Two shapes are pinned here:

1. **API completeness** — every symbol that runtime callers currently
   need is callable through the shared module and the plugin
   compatibility API.

2. **Boundary enforcement** — no file under ``src/pollypm/`` outside
   the plugin itself imports from
``task_assignment_notify.handlers.*`` or
``task_assignment_notify.resolver``. The companion
:mod:`tests.test_plugin_boundary_conformance` only catches
plugin-to-plugin private imports; this test extends that contract
to the core-to-plugin direction the issue (#939) flagged. The work
layer is stricter after #1364: it must not import this plugin at all,
and non-plugin runtime modules route task-assignment notifications
through the core event bus after #1363. Peer plugins route through the
shared module after #1365.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from pollypm import task_assignment_notify as shared_api
from pollypm.plugins_builtin.task_assignment_notify import api
from pollypm.plugins_builtin.task_assignment_notify import resolver


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src" / "pollypm"
PLUGIN_ROOT = (
    SRC_ROOT / "plugins_builtin" / "task_assignment_notify"
)


# Every public symbol the shared module and compatibility API must expose.
_PUBLIC_SURFACE: tuple[tuple[str, object, str], ...] = (
    ("DEDUPE_WINDOW_SECONDS", shared_api, "DEDUPE_WINDOW_SECONDS"),
    (
        "RECENT_SWEEPER_PING_SECONDS",
        shared_api,
        "RECENT_SWEEPER_PING_SECONDS",
    ),
    (
        "SWEEPER_PING_CONTEXT_ENTRY_TYPE",
        shared_api,
        "SWEEPER_PING_CONTEXT_ENTRY_TYPE",
    ),
    ("load_runtime_services", shared_api, "load_runtime_services"),
    ("notify", shared_api, "notify"),
    (
        "clear_alerts_for_cancelled_task",
        shared_api,
        "clear_alerts_for_cancelled_task",
    ),
    (
        "clear_no_session_alert_for_task",
        shared_api,
        "clear_no_session_alert_for_task",
    ),
    (
        "auto_claim_enabled_for_project",
        shared_api,
        "auto_claim_enabled_for_project",
    ),
    ("build_event_for_task", shared_api, "build_event_for_task"),
    ("close_quietly", shared_api, "close_quietly"),
    ("open_project_work_service", shared_api, "open_project_work_service"),
    ("record_sweeper_ping", shared_api, "record_sweeper_ping"),
    ("recover_dead_claims", shared_api, "recover_dead_claims"),
)


@pytest.mark.parametrize(
    "public_name,source_module,source_name",
    _PUBLIC_SURFACE,
    ids=[entry[0] for entry in _PUBLIC_SURFACE],
)
def test_public_surface_is_complete(
    public_name: str, source_module: object, source_name: str,
) -> None:
    """Each name is reachable through ``api`` and the shared module.

    A failure means either:

    * the public surface lost a name a core module relies on, or
    * the plugin compatibility API drifted from the shared surface.

    Either way, fix the shared surface first — core must not be patched
    to chase the plugin's internals."""
    assert hasattr(api, public_name), (
        f"task_assignment_notify.api is missing public name "
        f"{public_name!r}"
    )
    assert hasattr(source_module, source_name), (
        f"shared implementation {source_module.__name__}.{source_name} "
        f"is missing"
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
    """The compatibility API resolves shared functions at call time."""
    assert resolver is shared_api
    sentinel = object()

    def fake_load_runtime_services(*_a: object, **_k: object) -> object:
        return sentinel

    monkeypatch.setattr(shared_api, "load_runtime_services", fake_load_runtime_services)
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
    the shared module. This catches both peer plugins (which
    :mod:`tests.test_plugin_boundary_conformance` also covers) and
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
        "symbols from ``pollypm.task_assignment_notify``, not from "
        "``handlers.*`` / ``resolver``. Offenders:\n  - "
        + "\n  - ".join(offenders)
    )


_FORBIDDEN_WORK_PLUGIN_IMPORT_PATTERN = re.compile(
    r"^\s*(?:"
    r"from\s+pollypm\.plugins_builtin\.task_assignment_notify(?:\.|\s)"
    r"|import\s+pollypm\.plugins_builtin\.task_assignment_notify(?:\.|\s|$)"
    r")",
    re.MULTILINE,
)


def test_work_layer_does_not_import_task_assignment_notify_plugin() -> None:
    """``pollypm.work`` publishes neutral events instead of calling this
    plugin directly (#1364)."""
    offenders: list[str] = []
    for source_file in (SRC_ROOT / "work").rglob("*.py"):
        text = source_file.read_text(encoding="utf-8")
        for match in _FORBIDDEN_WORK_PLUGIN_IMPORT_PATTERN.finditer(text):
            line_no = text[: match.start()].count("\n") + 1
            rel = source_file.relative_to(REPO_ROOT).as_posix()
            offenders.append(f"{rel}:{line_no}: {match.group(0)}")
    assert offenders == [], (
        "pollypm.work must not import task_assignment_notify directly. "
        "Publish a work-layer event and let the plugin subscribe instead. "
        "Offenders:\n  - " + "\n  - ".join(offenders)
    )


def _imports_task_assignment_notify_plugin(source_file: Path) -> bool:
    module = "pollypm.plugins_builtin.task_assignment_notify"
    tree = ast.parse(source_file.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == module or node.module.startswith(f"{module}."):
                return True
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == module or alias.name.startswith(f"{module}."):
                    return True
    return False


def test_non_plugin_sources_do_not_import_task_assignment_notify_plugin() -> None:
    """Core and peer plugins use the shared module, not this plugin package."""
    offenders: list[str] = []
    for source_file in _iter_source_files():
        if _is_inside_plugin(source_file):
            continue
        if _imports_task_assignment_notify_plugin(source_file):
            offenders.append(source_file.relative_to(REPO_ROOT).as_posix())
    assert offenders == []


def test_peer_plugin_callers_use_shared_surface() -> None:
    """Peer plugins that still call notifier-owned helpers use shared code."""
    targets = (
        SRC_ROOT / "plugins_builtin" / "core_recurring" / "sweeps.py",
    )
    for target in targets:
        text = target.read_text(encoding="utf-8")
        assert (
            "pollypm.task_assignment_notify"
            in text
        ), (
            f"{target.relative_to(REPO_ROOT).as_posix()} should import "
            f"from pollypm.task_assignment_notify (#1365)"
        )
