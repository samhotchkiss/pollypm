from pollypm.plugin_api.v1 import PollyPMPlugin
from pollypm.schedulers.inline import InlineSchedulerBackend

plugin = PollyPMPlugin(
    name="inline_scheduler",
    capabilities=("scheduler",),
    scheduler_backends={"inline": InlineSchedulerBackend},
)
