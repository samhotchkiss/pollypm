"""Compatibility shim for shared task-assignment notification helpers."""

from __future__ import annotations

import sys

from pollypm import task_assignment_notify as _shared
from pollypm.task_assignment_notify import *  # noqa: F401,F403


sys.modules[__name__] = _shared
