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


def _on_project_created(context) -> None:
    """Observer fired when a project is registered via ``pm project new``.

    Records a lightweight event through the plugin host state store so
    the rail can surface "project X is ready for planning" affordances.
    The actual user prompt + task creation live in
    ``cli/project.py::new_cmd`` so the user touchpoint stays in the CLI
    (observers run silently, which is wrong for a prompt).
    """
    # Observers are best-effort; swallow any error so a crash here never
    # blocks the CLI from finishing.
    import logging
    try:
        payload = context.payload if hasattr(context, "payload") else {}
        logging.getLogger(__name__).info(
            "project_planning: project.created observed (payload=%s)",
            payload,
        )
    except Exception:  # noqa: BLE001
        pass


def initialize(api: "PluginAPI") -> None:
    """Startup hook.

    Records the plugin's initialise event and logs the set of registered
    profiles. Project-level wiring is:

    * CLI subcommands (``pm project plan`` / ``replan`` / ``new``) —
      mounted by ``pollypm.cli`` as ``project_app``.
    * ``project.created`` observer — registered on the plugin instance
      directly (see ``observers=`` below) so it fires for every
      ``run_observers("project.created", ...)`` emission.
    """
    api.emit_event(
        "initialize",
        {
            "profiles": list(PROFILE_NAMES),
            "cli_mounted": True,
            "observers": ["project.created"],
        },
    )


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
    observers={
        "project.created": [_on_project_created],
    },
    initialize=initialize,
)
