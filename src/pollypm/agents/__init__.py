"""Agent prompt-template assets.

This package houses prompt-template fragments that are loaded as data
by various dispatchers (tier-4 authority section, etc.). Treat the
markdown files inside as program inputs, not documentation — the
loader functions resolve paths relative to this package, so moving a
file requires updating the loader.

Today the only consumer is :mod:`pollypm.agents.tier4_authority`,
which loads ``tier4_authority.md`` to splice the broadened-authority
section into Polly's system prompt at tier-4 dispatch time.
"""

from __future__ import annotations
