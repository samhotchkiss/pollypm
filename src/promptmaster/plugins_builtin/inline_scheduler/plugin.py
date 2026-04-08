from promptmaster.plugin_api.v1 import PromptMasterPlugin
from promptmaster.schedulers.inline import InlineSchedulerBackend

plugin = PromptMasterPlugin(
    name="inline_scheduler",
    capabilities=("scheduler",),
    scheduler_backends={"inline": InlineSchedulerBackend},
)
