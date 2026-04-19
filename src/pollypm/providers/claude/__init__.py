"""``pollypm.providers.claude`` — the Claude provider package.

Phase B of #397 consolidated every Claude-specific helper that used
to live scattered across :mod:`pollypm.accounts`,
:mod:`pollypm.onboarding`, :mod:`pollypm.provider_sdk`, and the single
``providers/claude.py`` module into this subpackage.

Two public classes are exported:

* :class:`ClaudeProvider` — the Phase B implementation of
  :class:`pollypm.acct.ProviderAdapter`. Registered via
  ``[project.entry-points."pollypm.provider"]`` in ``pyproject.toml``
  as the ``claude`` provider.
* :class:`ClaudeAdapter` — the runtime-launch adapter loaded by the
  plugin host. Existed before Phase B; re-exported from this package
  so legacy imports (``from pollypm.providers.claude import
  ClaudeAdapter``) continue to resolve.

The small helper modules (:mod:`.detect`, :mod:`.env`, :mod:`.login`,
:mod:`.probe`, :mod:`.usage_parse`) are the internal decomposition;
other modules in PollyPM can import from them directly — nothing is
package-private. The legacy functions in ``onboarding`` / ``accounts``
now delegate to these helpers via back-compat shims.
"""

from __future__ import annotations

from .adapter import ClaudeAdapter
from .provider import ClaudeProvider

__all__ = ["ClaudeAdapter", "ClaudeProvider"]
