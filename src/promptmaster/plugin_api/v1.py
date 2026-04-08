from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

ProviderFactory = Callable[[], object]
RuntimeFactory = Callable[[], object]
HeartbeatBackendFactory = Callable[[], object]
SchedulerBackendFactory = Callable[[], object]
AgentProfileFactory = Callable[[], object]
ObserverHandler = Callable[["HookContext"], None]
FilterHandler = Callable[["HookContext"], "HookFilterResult | None"]


@dataclass(slots=True)
class HookContext:
    hook_name: str
    payload: Any
    root_dir: Path
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class HookFilterResult:
    action: str = "allow"
    payload: Any = None
    reason: str | None = None


@dataclass(slots=True)
class PromptMasterPlugin:
    name: str
    api_version: str = "1"
    version: str = "0.1.0"
    description: str = ""
    capabilities: tuple[str, ...] = ()
    providers: dict[str, ProviderFactory] = field(default_factory=dict)
    runtimes: dict[str, RuntimeFactory] = field(default_factory=dict)
    heartbeat_backends: dict[str, HeartbeatBackendFactory] = field(default_factory=dict)
    scheduler_backends: dict[str, SchedulerBackendFactory] = field(default_factory=dict)
    agent_profiles: dict[str, AgentProfileFactory] = field(default_factory=dict)
    observers: dict[str, list[ObserverHandler]] = field(default_factory=dict)
    filters: dict[str, list[FilterHandler]] = field(default_factory=dict)
