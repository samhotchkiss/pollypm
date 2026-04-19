"""Feature-owned CLI modules.

Contract:
- Inputs: Typer root apps plus CLI arguments/options.
- Outputs: registered command groups and side-effectful command handlers.
- Side effects: command registration only at import time; runtime effects
  happen inside individual command functions.
- Invariants: feature logic lives beside its command surface instead of
  accreting in ``pollypm.cli``.
"""

