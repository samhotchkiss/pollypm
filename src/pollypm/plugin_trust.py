"""Shared trust notice for third-party PollyPM extensions."""

from __future__ import annotations

import sys

_THIRD_PARTY_EXTENSION_WARNING_EMITTED = False
_THIRD_PARTY_EXTENSION_WARNING = (
    "Warning: third-party PollyPM plugins and providers run with full user "
    "privileges; review code before installing. See docs/plugin-trust.md."
)


def warn_third_party_extension_trust_once() -> bool:
    """Print the third-party extension trust warning once per process."""
    global _THIRD_PARTY_EXTENSION_WARNING_EMITTED
    if _THIRD_PARTY_EXTENSION_WARNING_EMITTED:
        return False
    _THIRD_PARTY_EXTENSION_WARNING_EMITTED = True
    print(_THIRD_PARTY_EXTENSION_WARNING, file=sys.stderr)
    return True


__all__ = ["warn_third_party_extension_trust_once"]
