from pollypm.heartbeats.local import LocalHeartbeatBackend
from pollypm.plugin_api.v1 import PollyPMPlugin

plugin = PollyPMPlugin(
    name="local_heartbeat",
    capabilities=("heartbeat",),
    heartbeat_backends={"local": LocalHeartbeatBackend},
)
