from pollypm.heartbeats.local import LocalHeartbeatBackend
from pollypm.plugin_api.v1 import Capability, PollyPMPlugin

plugin = PollyPMPlugin(
    name="local_heartbeat",
    capabilities=(Capability(kind="heartbeat", name="local"),),
    heartbeat_backends={"local": LocalHeartbeatBackend},
)
