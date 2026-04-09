from pollypm.plugin_api.v1 import PollyPMPlugin
from pollypm.providers.claude import ClaudeAdapter

plugin = PollyPMPlugin(
    name="claude",
    capabilities=("provider",),
    providers={"claude": ClaudeAdapter},
)
