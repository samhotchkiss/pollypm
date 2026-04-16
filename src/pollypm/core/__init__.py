"""PollyPM core rail.

The :class:`CoreRail` owns the long-lived rail responsibilities —
config, state store, plugin host, and (eventually) startup / shutdown
orchestration plus an event bus. Subsystems (Supervisor, heartbeat,
cockpit, job workers, …) register with the rail and it drives their
lifecycles in a deterministic order.

Today this is scaffolding only — :class:`Supervisor` still owns the
actual boot sequence. Subsequent decomposition steps will lift more
responsibility onto the rail.
"""

from __future__ import annotations

from pollypm.core.rail import CoreRail, Startable

__all__ = ["CoreRail", "Startable"]
