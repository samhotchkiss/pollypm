"""Compatibility re-exports for the core agent profile plugin.

The defaults live in :mod:`pollypm.agent_profiles.defaults` so core modules
do not import the built-in plugin package.
"""

from pollypm.agent_profiles.defaults import (
    StaticPromptProfile,
    _POLLY_OPERATOR_GUIDE_PATH,
    heartbeat_prompt,
    polly_prompt,
    reviewer_prompt,
    triage_prompt,
    worker_prompt,
)

__all__ = [
    "StaticPromptProfile",
    "_POLLY_OPERATOR_GUIDE_PATH",
    "heartbeat_prompt",
    "polly_prompt",
    "reviewer_prompt",
    "triage_prompt",
    "worker_prompt",
]
