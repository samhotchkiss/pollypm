from promptmaster.heartbeats.local import LocalHeartbeatBackend
from promptmaster.plugin_api.v1 import PromptMasterPlugin

plugin = PromptMasterPlugin(
    name="local_heartbeat",
    capabilities=("heartbeat",),
    heartbeat_backends={"local": LocalHeartbeatBackend},
)
