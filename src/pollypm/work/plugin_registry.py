"""Central plugin registry for work service components.

Holds references to the active WorkService, GateRegistry, SyncManager,
and SessionManager instances.  Each slot must be explicitly registered
before it can be accessed; accessing an unregistered slot raises a clear
error instead of returning None silently.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pollypm.work.gates import GateRegistry
    from pollypm.work.session_manager import SessionManager
    from pollypm.work.service import WorkService
    from pollypm.work.sync import SyncManager


class PluginNotRegisteredError(RuntimeError):
    """Raised when a plugin slot is accessed before registration."""


class PluginRegistry:
    """Central registry for work service plugins."""

    def __init__(self) -> None:
        self._work_service: WorkService | None = None
        self._gate_registry: GateRegistry | None = None
        self._sync_manager: SyncManager | None = None
        self._session_manager: SessionManager | None = None

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_work_service(self, service: WorkService) -> None:
        """Register the active WorkService implementation."""
        self._work_service = service

    def register_gate_registry(self, registry: GateRegistry) -> None:
        """Register the active GateRegistry."""
        self._gate_registry = registry

    def register_sync_manager(self, manager: SyncManager) -> None:
        """Register the active SyncManager."""
        self._sync_manager = manager

    def register_session_manager(self, manager: SessionManager) -> None:
        """Register the active SessionManager."""
        self._session_manager = manager

    # ------------------------------------------------------------------
    # Access (raises if not registered)
    # ------------------------------------------------------------------

    @property
    def work_service(self) -> WorkService:
        """Return the registered WorkService or raise."""
        if self._work_service is None:
            raise PluginNotRegisteredError(
                "No WorkService registered. "
                "Call register_work_service() before accessing."
            )
        return self._work_service

    @property
    def gate_registry(self) -> GateRegistry:
        """Return the registered GateRegistry or raise."""
        if self._gate_registry is None:
            raise PluginNotRegisteredError(
                "No GateRegistry registered. "
                "Call register_gate_registry() before accessing."
            )
        return self._gate_registry

    @property
    def sync_manager(self) -> SyncManager:
        """Return the registered SyncManager or raise."""
        if self._sync_manager is None:
            raise PluginNotRegisteredError(
                "No SyncManager registered. "
                "Call register_sync_manager() before accessing."
            )
        return self._sync_manager

    @property
    def session_manager(self) -> SessionManager:
        """Return the registered SessionManager or raise."""
        if self._session_manager is None:
            raise PluginNotRegisteredError(
                "No SessionManager registered. "
                "Call register_session_manager() before accessing."
            )
        return self._session_manager


# ---------------------------------------------------------------------------
# Configuration loader
# ---------------------------------------------------------------------------


def configure_work_plugins(
    config: object | None = None,
    db_path: object | None = None,
    project_path: object | None = None,
) -> PluginRegistry:
    """Read config, instantiate plugins, wire them up, return the registry.

    Handles missing config gracefully by using sensible defaults:
    - WorkService: SQLiteWorkService with the given db_path (or in-memory)
    - GateRegistry: built-in gates, optionally with project_path for customs
    - SyncManager: empty (no adapters)
    - SessionManager: not registered (requires tmux client)

    Parameters
    ----------
    config:
        A PollyPMConfig instance, or None for defaults.
    db_path:
        Path to the SQLite database file.  When None a temporary in-memory
        equivalent is used (tmp dir).
    project_path:
        Root of the project tree, used for gate/flow discovery.
    """
    from pathlib import Path
    import tempfile

    from pollypm.work.gates import GateRegistry
    from pollypm.work.sqlite_service import SQLiteWorkService
    from pollypm.work.sync import SyncManager

    registry = PluginRegistry()

    # Resolve db_path
    if db_path is None:
        _tmp = Path(tempfile.mkdtemp()) / "work.db"
        resolved_db = _tmp
    elif isinstance(db_path, (str, Path)):
        resolved_db = Path(db_path)
    else:
        resolved_db = Path(str(db_path))

    resolved_project: Path | None = None
    if project_path is not None:
        resolved_project = Path(str(project_path))

    # WorkService
    svc = SQLiteWorkService(db_path=resolved_db, project_path=resolved_project)
    registry.register_work_service(svc)

    # GateRegistry
    gate_reg = GateRegistry(project_path=resolved_project)
    registry.register_gate_registry(gate_reg)

    # SyncManager (no adapters by default)
    sync_mgr = SyncManager()
    registry.register_sync_manager(sync_mgr)

    # SessionManager is NOT registered by default because it requires
    # a TmuxClient and a running tmux session.  Callers that need it
    # should register it explicitly.

    return registry
