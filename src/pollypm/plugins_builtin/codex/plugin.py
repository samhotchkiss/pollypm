from pollypm.plugin_api.v1 import PollyPMPlugin
from pollypm.providers.codex import CodexAdapter

plugin = PollyPMPlugin(
    name="codex",
    capabilities=("provider",),
    providers={"codex": CodexAdapter},
)
