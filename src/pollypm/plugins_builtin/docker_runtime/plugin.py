from pollypm.plugin_api.v1 import PollyPMPlugin
from pollypm.runtimes.docker import DockerRuntimeAdapter

plugin = PollyPMPlugin(
    name="docker_runtime",
    capabilities=("runtime",),
    runtimes={"docker": DockerRuntimeAdapter},
)
