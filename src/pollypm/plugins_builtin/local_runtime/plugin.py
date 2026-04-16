from pollypm.plugin_api.v1 import Capability, PollyPMPlugin
from pollypm.runtimes.local import LocalRuntimeAdapter

plugin = PollyPMPlugin(
    name="local_runtime",
    capabilities=(Capability(kind="runtime", name="local"),),
    runtimes={"local": LocalRuntimeAdapter},
)
