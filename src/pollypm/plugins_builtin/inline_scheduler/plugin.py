from pollypm.plugin_api.v1 import Capability, PollyPMPlugin
from pollypm.schedulers.inline import InlineSchedulerBackend

plugin = PollyPMPlugin(
    name="inline_scheduler",
    capabilities=(Capability(kind="scheduler", name="inline"),),
    scheduler_backends={"inline": InlineSchedulerBackend},
)
