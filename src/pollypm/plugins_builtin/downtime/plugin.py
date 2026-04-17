"""Downtime Management plugin — explorer persona + 12h roster tick.

See ``docs/downtime-plugin-spec.md``. This plugin uses idle LLM budget
to run autonomous exploration (specs, speculative builds, doc audits,
security scans, alternative approaches). **Load-bearing principle:
nothing produced in downtime ever auto-deploys.** Every exploration
ends with an inbox message awaiting explicit user approval.

dt01 scope (this file's responsibilities):

* Register the ``explorer`` agent profile.
* Register the ``downtime.tick`` job handler.
* Register a recurring roster entry on the configured cadence
  (``@every 12h`` by default; configurable via ``[downtime].cadence``).

Flow template + validator (dt02), candidate sourcing (dt03), planner
integration (dt04), exploration handlers (dt05), inbox + apply routing
(dt06), CLI + config (dt07) ship in subsequent issues.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pollypm.plugin_api.v1 import (
    Capability,
    JobHandlerAPI,
    PollyPMPlugin,
    RosterAPI,
)
from pollypm.plugins_builtin.downtime.flow_validator import (
    DowntimeFlowValidationError,
    assert_downtime_flow_shape,
)
from pollypm.plugins_builtin.downtime.handlers.downtime_tick import (
    downtime_tick_handler,
)
from pollypm.plugins_builtin.downtime.settings import (
    DEFAULT_CADENCE,
    load_downtime_settings,
)

if TYPE_CHECKING:
    from pollypm.plugin_api.v1 import PluginAPI


_EXPLORER_PROFILE_PATH = Path(__file__).parent / "profiles" / "explorer.md"


class _MarkdownPromptProfile:
    """Agent profile that reads its persona from disk on demand.

    Mirrors ``project_planning``'s ``MarkdownPromptProfile`` — kept as a
    plain class (not ``@dataclass``) because plugin modules are loaded
    via ``importlib.util.spec_from_file_location`` without a
    ``sys.modules`` entry, which trips dataclass's module lookup under
    Python 3.14.
    """

    __slots__ = ("name", "path", "prompt")

    def __init__(self, name: str, path: Path) -> None:
        self.name = name
        self.path = path
        try:
            self.prompt = path.read_text(encoding="utf-8").strip()
        except OSError:
            self.prompt = ""

    def build_prompt(self, context) -> str | None:  # noqa: D401
        """Fresh read on every invocation so persona edits show up live."""
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return None
        return text.strip() or None


def _explorer_factory() -> _MarkdownPromptProfile:
    return _MarkdownPromptProfile(name="explorer", path=_EXPLORER_PROFILE_PATH)


def _register_handlers(api: JobHandlerAPI) -> None:
    # 12h cadence + per-tick guards means even a ~30s handler has plenty
    # of room. Keep max_attempts at 1 — a failed tick will fire again on
    # the next schedule anyway, and retries could double-schedule work.
    api.register_handler(
        "downtime.tick",
        downtime_tick_handler,
        max_attempts=1,
        timeout_seconds=60.0,
    )


def _register_roster(api: RosterAPI) -> None:
    """Register the recurring schedule using the configured cadence.

    Default is ``@every 12h`` per spec §3. Users can override via
    ``[downtime].cadence`` in ``pollypm.toml``; the plugin host calls
    ``register_roster`` once at bootstrap so the config read here is the
    single source of truth for the schedule.
    """
    cadence = DEFAULT_CADENCE
    try:
        from pollypm.config import DEFAULT_CONFIG_PATH, resolve_config_path

        config_path = resolve_config_path(DEFAULT_CONFIG_PATH)
        if config_path.exists():
            settings = load_downtime_settings(config_path)
            if settings.cadence:
                cadence = settings.cadence
    except Exception:
        # Roster registration must not crash the host over a bad config
        # — fall back to the default cadence and let the tick handler's
        # own config reload surface the error at run time.
        cadence = DEFAULT_CADENCE

    api.register_recurring(
        cadence,
        "downtime.tick",
        {},
        dedupe_key="downtime.tick",
    )


def _validate_bundled_flow() -> tuple[bool, str]:
    """Re-validate the bundled downtime_explore flow at plugin init.

    The flow-engine's own parser only checks generic graph shape; the
    never-auto-deploy rule (spec §10) is the downtime plugin's
    responsibility. Running the stricter validator here fails loudly if
    a bad edit slips into the shipped YAML.

    Returns ``(ok, detail)``. ``ok=False`` is reported via ``emit_event``
    so the plugin host's diagnostic surfaces pick it up; the plugin
    itself still loads — the tick handler refuses to schedule when the
    flow fails to resolve.
    """
    try:
        from pollypm.work.flow_engine import resolve_flow

        flow = resolve_flow("downtime_explore")
    except Exception as exc:  # noqa: BLE001
        return False, f"resolve_flow failed: {exc}"

    try:
        assert_downtime_flow_shape(flow)
    except DowntimeFlowValidationError as exc:
        return False, str(exc)
    return True, "ok"


def initialize(api: "PluginAPI") -> None:
    """Record the plugin's initialisation event.

    Keeps a lightweight marker in the state store so operators can see
    the downtime plugin loaded (and under which cadence). Also
    re-validates the bundled ``downtime_explore`` flow under the
    stricter never-auto-deploy rules (dt02 §10 — belt-and-suspenders).
    """
    try:
        from pollypm.config import DEFAULT_CONFIG_PATH, resolve_config_path

        config_path = resolve_config_path(DEFAULT_CONFIG_PATH)
        cadence = DEFAULT_CADENCE
        if config_path.exists():
            cadence = load_downtime_settings(config_path).cadence
    except Exception:
        cadence = DEFAULT_CADENCE

    flow_ok, flow_detail = _validate_bundled_flow()

    api.emit_event(
        "initialize",
        {
            "cadence": cadence,
            "handlers": ["downtime.tick"],
            "profile": "explorer",
            "flow_validator_ok": flow_ok,
            "flow_validator_detail": flow_detail,
        },
    )


plugin = PollyPMPlugin(
    name="downtime",
    version="0.1.0",
    description=(
        "Downtime management: use idle LLM budget for autonomous exploration. "
        "Every downtime task ends with an inbox message — nothing auto-deploys."
    ),
    capabilities=(
        Capability(kind="agent_profile", name="explorer"),
        Capability(kind="job_handler", name="downtime.tick"),
        Capability(kind="roster_entry", name="downtime.tick"),
    ),
    agent_profiles={"explorer": _explorer_factory},
    register_handlers=_register_handlers,
    register_roster=_register_roster,
    initialize=initialize,
)
