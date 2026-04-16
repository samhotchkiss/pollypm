from pollypm.plugin_api.v1 import Capability, PollyPMPlugin


def _factory(**kwargs):
    from pollypm.session_services.tmux import TmuxSessionService
    return TmuxSessionService(**kwargs)


plugin = PollyPMPlugin(
    name="tmux_session_service",
    capabilities=(Capability(kind="session_service", name="tmux"),),
    session_services={"tmux": _factory},
)
