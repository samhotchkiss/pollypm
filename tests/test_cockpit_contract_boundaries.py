"""Cockpit modular-contract and tmux-boundary tests (#969).

The refactor target is:

    rail UI -> navigation state machine -> content resolver -> window manager

Only the final window-manager boundary should need concrete tmux pane/window
operations for cockpit right-pane work. The production allowlist below is
pragmatic because today's legacy cockpit and session code still contain direct
calls. The intended cockpit end-state is smaller:

* ``src/pollypm/cockpit_window_manager.py`` owns cockpit right-pane mutation.
* ``src/pollypm/tmux/client.py`` owns the low-level tmux command adapter.
* Tests should use protocol fakes instead of introducing new production
  call sites.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import inspect
from dataclasses import dataclass
from pathlib import Path

import pollypm.cockpit_contracts as contracts


REPO_ROOT = Path(__file__).resolve().parent.parent

TMUX_PANE_WINDOW_METHODS: frozenset[str] = frozenset(
    {
        "join_pane",
        "split_window",
        "kill_pane",
        "respawn_pane",
        "break_pane",
        "swap_pane",
        "list_panes",
        "list_windows",
    }
)

# Current production references. Each entry is temporary except the low-level
# adapter; remove entries as cockpit code moves behind CockpitWindowManager.
CURRENT_TMUX_BOUNDARY_ALLOWLIST: frozenset[str] = frozenset(
    {
        "src/pollypm/cli.py",
        "src/pollypm/cli_features/session_runtime.py",
        "src/pollypm/cockpit_inbox.py",
        "src/pollypm/cockpit_rail.py",
        "src/pollypm/cockpit_ui.py",
        "src/pollypm/cockpit_window_manager.py",
        "src/pollypm/core/console_window.py",
        "src/pollypm/job_runner.py",
        "src/pollypm/plugins_builtin/core_rail_items/plugin.py",
        "src/pollypm/plugins_builtin/task_assignment_notify/handlers/sweep.py",
        "src/pollypm/session_services/tmux.py",
        "src/pollypm/supervisor.py",
        "src/pollypm/tmux/client.py",
        "src/pollypm/work/session_manager.py",
    }
)

FORBIDDEN_CONTRACT_IMPORT_ROOTS: frozenset[str] = frozenset(
    {
        "pollypm.cockpit_ui",
        "pollypm.cockpit_rail",
        "pollypm.supervisor",
        "pollypm.tmux",
        "textual",
    }
)


@dataclass(frozen=True, slots=True)
class TmuxReference:
    path: str
    line: int
    symbol: str
    kind: str

    @property
    def summary(self) -> str:
        return f"{self.path}:{self.line}: {self.kind} {self.symbol}"


def _production_python_files() -> tuple[Path, ...]:
    src_root = REPO_ROOT / "src" / "pollypm"
    return tuple(sorted(src_root.rglob("*.py")))


def _relative(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _tmux_references(path: Path) -> tuple[TmuxReference, ...]:
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(path))
    rel = _relative(path)
    refs: list[TmuxReference] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in TMUX_PANE_WINDOW_METHODS:
            refs.append(TmuxReference(rel, node.lineno, node.attr, "attribute"))
        elif (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in TMUX_PANE_WINDOW_METHODS
        ):
            refs.append(TmuxReference(rel, node.lineno, node.name, "definition"))
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in TMUX_PANE_WINDOW_METHODS
        ):
            refs.append(TmuxReference(rel, node.lineno, node.value, "string"))

    return tuple(refs)


def _imported_roots(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.append(node.module)
    return tuple(roots)


def _export_shape(obj: object) -> tuple[str, tuple[str, ...]] | None:
    if dataclasses.is_dataclass(obj):
        return ("dataclass", tuple(field.name for field in dataclasses.fields(obj)))
    if inspect.isclass(obj) and hasattr(obj, "__members__"):
        members = getattr(obj, "__members__")
        return ("enum", tuple(sorted(members)))
    if inspect.isclass(obj) and getattr(obj, "_is_protocol", False):
        methods = tuple(
            sorted(
                name
                for name, member in obj.__dict__.items()
                if callable(member) and not name.startswith("_")
            )
        )
        return ("protocol", methods)
    return None


def test_contract_records_are_frozen_slot_dataclasses() -> None:
    dataclass_types = (
        contracts.NavigationRequest,
        contracts.NavigationResult,
        contracts.ContentPlan,
        contracts.PaneSnapshot,
        contracts.WindowSnapshot,
        contracts.RightPaneLifecycle,
        contracts.MountResult,
    )

    for cls in dataclass_types:
        assert dataclasses.is_dataclass(cls), cls.__name__
        assert getattr(cls, "__slots__", None), cls.__name__
        assert cls.__dataclass_params__.frozen, cls.__name__


def test_contracts_describe_the_four_modular_boundaries() -> None:
    assert hasattr(contracts.CockpitNavigationStateMachine, "navigate")
    assert hasattr(contracts.CockpitContentResolver, "resolve_content")
    assert hasattr(contracts.CockpitWindowManager, "capture")
    assert hasattr(contracts.CockpitWindowManager, "mount_content")
    assert hasattr(contracts.CockpitRightPaneLifecycleStore, "load_lifecycle")
    assert hasattr(contracts.CockpitRightPaneLifecycleStore, "save_lifecycle")


def test_mount_result_ok_is_false_for_failed_disposition() -> None:
    lifecycle = contracts.RightPaneLifecycle(
        state=contracts.RightPaneLifecycleState.ERROR
    )
    result = contracts.MountResult(
        disposition=contracts.MountDisposition.FAILED,
        lifecycle=lifecycle,
    )
    assert result.ok is False


def test_cockpit_modules_do_not_export_conflicting_contract_names() -> None:
    modules = [
        contracts,
        importlib.import_module("pollypm.cockpit_content"),
        importlib.import_module("pollypm.cockpit_navigation"),
        importlib.import_module("pollypm.cockpit_navigation_client"),
        importlib.import_module("pollypm.cockpit_state_store"),
    ]
    seen: dict[str, tuple[str, tuple[str, tuple[str, ...]]]] = {}
    conflicts: list[str] = []

    for module in modules:
        for name in getattr(module, "__all__", ()):
            obj = getattr(module, name)
            if getattr(obj, "__module__", module.__name__) != module.__name__:
                continue
            shape = _export_shape(obj)
            if shape is None:
                continue
            previous = seen.get(name)
            if previous is not None and previous[1] != shape:
                conflicts.append(
                    f"{name}: {previous[0]} {previous[1]} != "
                    f"{module.__name__} {shape}"
                )
                continue
            seen[name] = (module.__name__, shape)

    assert conflicts == []


def test_cockpit_contracts_keep_imports_light() -> None:
    path = REPO_ROOT / "src" / "pollypm" / "cockpit_contracts.py"
    imports = _imported_roots(path)

    offenders = sorted(
        imported
        for imported in imports
        for forbidden in FORBIDDEN_CONTRACT_IMPORT_ROOTS
        if imported == forbidden or imported.startswith(f"{forbidden}.")
    )
    assert not offenders, (
        "cockpit_contracts.py must stay pure and lightweight. "
        "Forbidden imports:\n  - " + "\n  - ".join(offenders)
    )


def test_tmux_pane_window_methods_stay_inside_allowlist() -> None:
    refs = [
        ref
        for path in _production_python_files()
        for ref in _tmux_references(path)
        if ref.path not in CURRENT_TMUX_BOUNDARY_ALLOWLIST
    ]
    assert not refs, (
        "Concrete tmux pane/window methods must not spread to new production "
        "files. Route cockpit use through CockpitWindowManager or update the "
        "documented allowlist with a removal condition. Offenders:\n  - "
        + "\n  - ".join(ref.summary for ref in refs)
    )


def test_tmux_boundary_allowlist_has_no_stale_entries() -> None:
    stale: list[str] = []
    for rel in sorted(CURRENT_TMUX_BOUNDARY_ALLOWLIST):
        path = REPO_ROOT / rel
        if not path.exists():
            stale.append(f"{rel} (file missing)")
            continue
        if not _tmux_references(path):
            stale.append(f"{rel} (no concrete tmux reference found)")

    assert not stale, (
        "Stale entries in CURRENT_TMUX_BOUNDARY_ALLOWLIST. Remove them so "
        "the boundary tightens as the refactor lands:\n  - " + "\n  - ".join(stale)
    )
