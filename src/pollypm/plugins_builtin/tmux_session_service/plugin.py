from pollypm.plugin_api.v1 import PollyPMPlugin


def _factory(**kwargs):
    from pollypm.session_services.tmux import TmuxSessionService
    return TmuxSessionService(**kwargs)


plugin = PollyPMPlugin(
    name="tmux_session_service",
    capabilities=("session_service",),
    session_services={"tmux": _factory},
)
