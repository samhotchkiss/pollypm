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
    """Observer fired when a project is registered.

    Issue #255: auto-fires the ``plan_project`` flow for every new
    project so users don't silently skip the architect + 5-critic +
    Magic pipeline by using ``pm add-project`` instead of
    ``pm project new``.

    Precedence of suppression (any one suppresses):

    * ``payload.skip_plan=True`` — the CLI was invoked with
      ``--skip-plan``.
    * ``config.planner.auto_on_project_created=False`` — user has
      globally disabled auto-fire. A one-line inbox nudge is logged
      in that case so the user can still find the project and run
      ``pm project plan <name>`` manually.

    Failures here are swallowed: the observer runs after
    ``register_project`` has already succeeded, and a crash in task
    creation must never undo the registration.
    """
    import logging

    log = logging.getLogger(__name__)

    try:
        payload = context.payload if hasattr(context, "payload") else {}
        if not isinstance(payload, dict):
            return
        project_key = payload.get("project_key")
        project_path_raw = payload.get("path")
        if not project_key or not project_path_raw:
            log.debug(
                "project_planning: project.created missing project_key or path (%s)",
                payload,
            )
            return
        skip_plan_flag = bool(payload.get("skip_plan", False))

        # Config lookup: the observer doesn't get a PluginAPI, so we
        # re-read the global config here. Callers may pass an explicit
        # ``config_path`` via the event metadata (test harnesses do);
        # otherwise we fall through to ``DEFAULT_CONFIG_PATH``. Missing
        # config (fresh install, or a test ExtensionHost with no
        # pollypm.toml) means default ``auto_on_project_created=True``.
        auto_fire = True
        try:
            from pathlib import Path as _Path2
            from pollypm.config import DEFAULT_CONFIG_PATH, load_config, resolve_config_path

            metadata = getattr(context, "metadata", None) or {}
            override_cfg = metadata.get("config_path") if isinstance(metadata, dict) else None
            if override_cfg:
                cfg_path = _Path2(override_cfg)
            else:
                cfg_path = resolve_config_path(DEFAULT_CONFIG_PATH)
            if cfg_path.exists():
                cfg = load_config(cfg_path)
                auto_fire = bool(cfg.planner.auto_on_project_created)
        except Exception as exc:  # noqa: BLE001
            log.debug(
                "project_planning: config read failed, defaulting auto_fire=True (%s)",
                exc,
            )

        if skip_plan_flag:
            log.info(
                "project_planning: project.created for '%s' — auto-fire "
                "skipped via --skip-plan flag.", project_key,
            )
            return

        if not auto_fire:
            # Per spec, surface a one-line nudge so the user can still
            # find the project and opt in manually. We emit a WARNING
            # so it shows up in the default log handler; a future
            # iteration can route this to the inbox service directly.
            log.warning(
                "project_planning: new project '%s' was registered but "
                "auto-planning is disabled ([planner] auto_on_project_created=false). "
                "Run `pm project plan %s` to design the architecture.",
                project_key, project_key,
            )
            return

        # Auto-fire: create a plan_project task on the project's work
        # service. Local import to avoid pulling SQLite deps into code
        # paths that don't need them.
        from pathlib import Path as _Path
        from pollypm.work.sqlite_service import SQLiteWorkService

        project_path = _Path(project_path_raw)
        db_path = project_path / ".pollypm" / "state.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with SQLiteWorkService(
            db_path=db_path, project_path=project_path,
        ) as svc:
            task = svc.create(
                title=f"Plan project {project_key}",
                description=(
                    f"Auto-created on project.created. Run the architect "
                    f"+ 5-critic planning pipeline on {project_key}."
                ),
                type="task",
                project=project_key,
                flow_template="plan_project",
                roles={"architect": "architect"},
                priority="high",
            )
        log.info(
            "project_planning: auto-fired plan_project task %s for '%s'.",
            task.task_id, project_key,
        )
    except Exception as exc:  # noqa: BLE001
        # Observers are best-effort.
        try:
            log.warning(
                "project_planning: project.created observer failed: %s", exc,
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
