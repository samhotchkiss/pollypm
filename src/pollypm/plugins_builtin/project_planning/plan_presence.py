"""Compatibility shim for the shared plan-presence gate.

The predicate moved to :mod:`pollypm.plan_presence` so sibling plugins can
consume it without importing ``project_planning`` internals. Keep this module
as an alias for older imports and monkeypatch paths.
"""

from __future__ import annotations

import sys

from pollypm import plan_presence as _shared
from pollypm.plan_presence import *  # noqa: F401,F403

sys.modules[__name__] = _shared
