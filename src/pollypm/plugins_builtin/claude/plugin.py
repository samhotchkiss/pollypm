from pollypm.plugin_api.v1 import Capability, PollyPMPlugin
from pollypm.providers.claude import ClaudeAdapter

plugin = PollyPMPlugin(
    name="claude",
    capabilities=(Capability(kind="provider", name="claude"),),
    providers={"claude": ClaudeAdapter},
)
