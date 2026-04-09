from pollypm.plugin_api.v1 import PollyPMPlugin
from pollypm.runtimes.local import LocalRuntimeAdapter

plugin = PollyPMPlugin(
    name="local_runtime",
    capabilities=("runtime",),
    runtimes={"local": LocalRuntimeAdapter},
)
