from pollypm.plugin_api.v1 import Capability, PollyPMPlugin
from pollypm.runtimes.docker import DockerRuntimeAdapter

plugin = PollyPMPlugin(
    name="docker_runtime",
    capabilities=(Capability(kind="runtime", name="docker"),),
    runtimes={"docker": DockerRuntimeAdapter},
)
