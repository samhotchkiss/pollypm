from pollypm.plugin_api.v1 import Capability, PollyPMPlugin
from pollypm.providers.codex import CodexAdapter

plugin = PollyPMPlugin(
    name="codex",
    capabilities=(Capability(kind="provider", name="codex"),),
    providers={"codex": CodexAdapter},
)
