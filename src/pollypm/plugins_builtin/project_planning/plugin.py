"""Project Planning plugin — architect + 5 critic personas.

Declarative scaffold for the opinionated planning pipeline (see
``docs/planner-plugin-spec.md``). The six agent profiles are registered
through the ``agent_profiles`` factory field on ``PollyPMPlugin``, the
same pattern ``core_agent_profiles`` uses. Flow templates and gates are
hosted as content (see the ``[content]`` block in the manifest) and
loaded through the plugin's ``initialize(api)`` callback at runtime.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pollypm.plugin_api.v1 import Capability, PollyPMPlugin

if TYPE_CHECKING:
    from pollypm.agent_profiles.base import AgentProfileContext
    from pollypm.plugin_api.v1 import PluginAPI


PROFILES_DIR = Path(__file__).resolve().parent / "profiles"


PROFILE_NAMES: tuple[str, ...] = (
    "architect",
    "critic_simplicity",
    "critic_maintainability",
    "critic_user",
    "critic_operational",
    "critic_security",
)


class MarkdownPromptProfile:
    """Agent profile that reads a markdown prompt file at build time.

    The profile file lives under ``profiles/<name>.md`` relative to this
    plugin. The file is read fresh on each ``build_prompt`` call so
    hot-editing a persona during development takes effect without
    restarting the rail. If the file vanishes (e.g. a shipping glitch)
    we return ``None`` and let the session service decide how to degrade.

    Implemented as a plain class (not ``@dataclass(slots=True)``) because
    plugin modules are loaded via ``importlib.util.spec_from_file_location``
    without being added to ``sys.modules``; under Python 3.14 that trips
    an internal ``cls.__module__`` lookup inside ``dataclasses``.
    """

    __slots__ = ("name", "path")

    def __init__(self, name: str, path: Path) -> None:
        self.name = name
        self.path = path

    def build_prompt(self, context: "AgentProfileContext") -> str | None:
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return None
        return text.strip() or None


def _profile_factory(name: str):
    path = PROFILES_DIR / f"{name}.md"
    return lambda: MarkdownPromptProfile(name=name, path=path)


def initialize(api: "PluginAPI") -> None:
    """Startup hook.

    The full wiring (content-path discovery for flows + gates, CLI
    subcommand mount, project.created hook, provider-policy overrides)
    lands in pp10. For pp01 this is an intentional stub so the plugin
    surface and the ``initialize`` contract are in place without
    changing runtime behaviour yet.
    """
    # Best-effort heartbeat event so the plugin's presence is visible in
    # the events log even while the rest of the wiring is stubbed out.
    api.emit_event("initialize.stub", {"profiles": list(PROFILE_NAMES)})


plugin = PollyPMPlugin(
    name="project_planning",
    version="0.1.0",
    description=(
        "Opinionated architecture-planning plugin: architect + 5 critic "
        "personas, multi-stage planning flows, and gates for autonomous "
        "project decomposition."
    ),
    capabilities=tuple(
        Capability(kind="agent_profile", name=name) for name in PROFILE_NAMES
    ),
    agent_profiles={name: _profile_factory(name) for name in PROFILE_NAMES},
    initialize=initialize,
)
