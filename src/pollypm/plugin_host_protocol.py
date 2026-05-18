"""Structural protocol for the slice of :class:`ExtensionHost` consumed
by :mod:`pollypm.plugin_validate`.

This leaf module exists to break the static ``plugin_host`` <->
``plugin_validate`` 2-cycle tracked in #1367:

- ``plugin_host`` top-imports :func:`plugin_validate.validate_plugin` to
  gate each freshly-loaded plugin during ``ExtensionHost._install``.
- ``plugin_validate`` used to import ``ExtensionHost`` (under
  ``TYPE_CHECKING``) purely to annotate the two convenience entry points
  that scan an already-built host. Even though that edge was lazy at
  runtime, an AST cycle scan still pairs the two modules.

By depending on the structural :class:`ExtensionHostLike` protocol
defined here, ``plugin_validate`` no longer needs to know about
``plugin_host`` at all — the import graph collapses to a clean
``plugin_host`` -> ``plugin_validate`` -> ``plugin_host_protocol``
chain. Both validation entry points work with anything that quacks like
the host (real :class:`ExtensionHost`, integration-test fakes, future
alternate hosts).

Keep this module dependency-free apart from standard library imports.
"""

from __future__ import annotations

from typing import Protocol

from pollypm.plugin_api.v1 import PollyPMPlugin


class ExtensionHostLike(Protocol):
    """Structural slice of :class:`pollypm.plugin_host.ExtensionHost`.

    Captures the surface :mod:`pollypm.plugin_validate` calls when
    sweeping a host's plugins: enumerate loaded plugins, remove ones
    that fail validation, and record the structured error so surfaces
    (CLI / cockpit) can display it.
    """

    def plugins(self) -> dict[str, PollyPMPlugin]: ...

    def remove_plugin(self, name: str) -> None: ...

    def _record_error(
        self,
        message: str,
        *,
        plugin: str | None = ...,
        stage: str = ...,
    ) -> None: ...
