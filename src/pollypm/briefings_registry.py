"""Core seam for surfacing morning-briefing inbox entries on the dashboard.

Contract:
- Inputs: a base directory + filter knobs (``status``, ``limit``).
- Outputs: an iterable of briefing-entry-shaped objects, newest first.
- Side effects: read-only — this seam never writes briefings.
- Invariants: when no provider is registered, returns an empty list so
  the dashboard renders without the briefing banner instead of erroring.

Boundary note:
This module is core — it must not import from ``pollypm.plugins_builtin``.
The actual briefing-inbox implementation lives in the ``morning_briefing``
plugin; it installs its ``list_briefings`` at plugin ``initialize`` time
via :func:`register_briefing_provider`. The dashboard reads through
:func:`list_briefings` so disabling the plugin downgrades the briefing
banner to "absent" rather than breaking imports.

Render / regenerate facade (added for #2059 round-9 Web API boundary fix):
Beyond the inbox-list provider above, this module also exposes a
per-type **render facade** so the Web API (and any other surface that
needs to render or regenerate a briefing) can do so without importing
``pollypm.plugins_builtin`` directly. See :class:`BriefingRenderProvider`
and :func:`register_briefing_render_provider`. The ``morning_briefing``
plugin installs its render provider in ``_initialize`` alongside
``register_briefing_provider``; the Web API routes look it up by type
name through :func:`get_briefing_render_provider`.

Mirrors the registration-seam pattern established in
:mod:`pollypm.approval_notifications` (see #1597 / #1363).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, runtime_checkable


logger = logging.getLogger(__name__)


@runtime_checkable
class BriefingEntryLike(Protocol):
    """Duck-typed briefing entry the dashboard renders.

    The dashboard only reads ``created_at`` (ISO string) and
    ``date_local`` (``YYYY-MM-DD``). Anything that exposes those two
    attributes is rendered correctly — third-party briefing plugins can
    swap their own dataclass in without depending on the
    ``morning_briefing`` plugin's :class:`BriefingEntry` type.
    """

    @property
    def created_at(self) -> str: ...

    @property
    def date_local(self) -> str: ...


BriefingProvider = Callable[..., Iterable[BriefingEntryLike]]


# Module-level registration slot. The ``morning_briefing`` plugin (or
# a replacement) installs its ``list_briefings`` here during plugin
# ``initialize``; when nothing is registered, ``list_briefings`` below
# returns an empty list so the dashboard's briefing banner is simply
# absent instead of raising.
_briefing_provider: BriefingProvider | None = None


def register_briefing_provider(provider: BriefingProvider | None) -> None:
    """Install (or clear) the default briefing-inbox provider.

    Called by the ``morning_briefing`` plugin during ``initialize`` so
    the dashboard can surface briefings without taking a hard import on
    the plugin tree. Pass ``None`` to clear (used by tests).

    ``provider`` must accept ``(base_dir, *, status=..., limit=...)``
    and return briefing entries newest-first. The expected signature
    matches :func:`pollypm.plugins_builtin.morning_briefing.inbox.list_briefings`
    exactly; mismatches are caught at call time and logged.
    """
    global _briefing_provider
    _briefing_provider = provider


def is_briefing_provider_registered() -> bool:
    """Return True iff a briefing-inbox provider is currently registered.

    Public availability probe so surfaces (web API, cockpit) can decide
    whether to advertise the briefing type without reaching into the
    module-private ``_briefing_provider`` slot. Mirrors host semantics:
    when the ``morning_briefing`` plugin is disabled via
    ``[plugins].disabled`` (or absent), no provider is registered and
    this returns ``False`` (Codex round-3 on PR #2059: route was reading
    the private slot to gate availability).
    """
    return _briefing_provider is not None


def list_briefings(
    base_dir: Path,
    *,
    status: str = "open",
    limit: int | None = None,
) -> list[BriefingEntryLike]:
    """Return briefings via the registered provider, or ``[]`` if none.

    Errors from the provider are logged and swallowed — a flaky plugin
    must never break the dashboard render. The dashboard treats "no
    entries" identically to "plugin not loaded".
    """
    provider = _briefing_provider
    if provider is None:
        return []
    try:
        return list(provider(base_dir, status=status, limit=limit))
    except Exception:  # noqa: BLE001
        logger.exception(
            "briefings_registry: provider failed (base_dir=%s, status=%s)",
            base_dir,
            status,
        )
        return []


# ---------------------------------------------------------------------------
# Render / regenerate facade (#2059 round-9 boundary fix)
# ---------------------------------------------------------------------------
#
# The Web API needs more than the "list inbox entries" seam above — it
# also renders the latest briefing body and force-regenerates on POST.
# Previously the Web API imported ``pollypm.plugins_builtin.morning_briefing``
# internals directly to do that, which violated the documented core →
# ``plugins_builtin`` boundary (only ``cli.py`` is the sanctioned edge —
# see ``cli.py:201-207``).
#
# This facade extends the registry so a plugin (today only
# ``morning_briefing``; tomorrow any briefing plugin) can install a
# render provider keyed by briefing-type name. The Web API consumes
# adapters by name through :func:`get_briefing_render_provider` and never
# learns the plugin's import path. Provider returns a typed
# :class:`BriefingArtifact`; the route translates that into its pydantic
# response model.


@dataclass(frozen=True, slots=True)
class BriefingArtifact:
    """Provider-returned briefing payload.

    Plain-data shape returned by :meth:`BriefingRenderProvider.render_last`
    and :meth:`BriefingRenderProvider.regenerate`. Kept deliberately
    minimal so plugins don't need to import the Web API's pydantic
    models — the route layer maps fields onto its public schema.

    Fields:
        date_local:   ``YYYY-MM-DD`` the briefing is for (local TZ).
        markdown:     Rendered briefing body the user sees.
        mode:         Plugin-defined render mode (e.g. ``"synthesized"``).
        generated_at: ISO 8601 timestamp. ``None`` if the plugin can't
                      recover it (e.g. legacy on-disk entry).
        metadata:     Free-form plugin metadata. The Web API surfaces
                      it verbatim under ``metadata`` so cockpit/UI can
                      consume plugin-specific fields without a schema
                      bump per plugin.
    """

    date_local: str
    markdown: str
    mode: str | None = None
    generated_at: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class BriefingRenderProvider(Protocol):
    """Render / regenerate adapter installed per briefing-type name.

    Implementations live in plugins. The ``morning_briefing`` plugin
    installs its provider at plugin ``initialize`` time so the Web API
    can render and regenerate without taking a hard import on the
    plugin module tree.

    Methods raise on bad config / provider failure — the route layer
    wraps exceptions into the API's typed error envelope.
    """

    def render_last(self, config: Any) -> BriefingArtifact | None:
        """Return the newest cached briefing, or ``None`` if there is none."""
        ...

    def regenerate(
        self,
        config: Any,
        project: str | None = None,
    ) -> BriefingArtifact:
        """Force-fire the briefing pipeline.

        ``project`` narrows scope when the briefing type supports it.
        Providers MUST raise ``ValueError`` when they do not support
        narrowing (the morning briefing today) — the route layer
        translates that into a typed 400.
        """
        ...


@dataclass(frozen=True, slots=True)
class BriefingRenderRegistration:
    """Provider + metadata for one briefing type.

    The Web API's ``GET /briefings`` discovery endpoint surfaces the
    ``description`` to clients and uses ``is_available`` to compute
    the per-request availability flag. Plugins supply both at
    :func:`register_briefing_render_provider` time so the registry is
    the single source of truth for what types exist, what they're for,
    and whether they can run right now (Codex round-10 on #2059 —
    previously the Web API maintained its own private ``_REGISTRY``
    keyed only on ``"morning"`` and ignored plugin-contributed types
    even though :func:`registered_briefing_render_types` exposed them).
    """

    provider: BriefingRenderProvider
    description: str
    is_available: Callable[[Any], bool]


def _default_is_available(_config: Any) -> bool:
    """Fallback availability check: a registered provider is available."""
    return True


_render_providers: dict[str, BriefingRenderRegistration] = {}


def register_briefing_render_provider(
    type_name: str,
    provider: BriefingRenderProvider | None,
    *,
    description: str = "",
    is_available: Callable[[Any], bool] | None = None,
) -> None:
    """Install (or clear) a render / regenerate provider for a briefing type.

    Plugins call this in their ``initialize`` hook. Passing ``provider=None``
    clears the slot (used by tests + the plugin-disable cleanup path).
    The Web API consumes providers via :func:`get_briefing_render_provider`
    and surfaces ``description`` / ``is_available`` through its
    discovery endpoint, keeping ``plugins_builtin`` out of the core
    import graph.

    ``description`` is the human-readable blurb returned by
    ``GET /briefings`` for this type. ``is_available`` is called with
    the per-request config and must return whether this type can render
    now (e.g. plugin enabled, backing store reachable). Both keyword
    args default to spec-compatible no-ops so legacy callers that only
    pass a provider keep working — though new types should supply them
    for the discovery endpoint to render usefully (Codex round-10 on
    #2059).
    """
    if provider is None:
        _render_providers.pop(type_name, None)
        return
    _render_providers[type_name] = BriefingRenderRegistration(
        provider=provider,
        description=description,
        is_available=is_available or _default_is_available,
    )


def get_briefing_render_provider(
    type_name: str,
) -> BriefingRenderProvider | None:
    """Return the provider installed for ``type_name``, or ``None``."""
    registration = _render_providers.get(type_name)
    return registration.provider if registration is not None else None


def get_briefing_render_registration(
    type_name: str,
) -> BriefingRenderRegistration | None:
    """Return the full registration record for ``type_name`` (with metadata).

    Used by surfaces that need the description / availability adapter
    (the Web API's ``GET /briefings``). Returns ``None`` for unknown
    type names.
    """
    return _render_providers.get(type_name)


def registered_briefing_render_types() -> tuple[str, ...]:
    """Return the briefing-type names that currently have a render provider."""
    return tuple(sorted(_render_providers))


# ---------------------------------------------------------------------------
# Per-request plugin-disable check (#2059 round-9)
# ---------------------------------------------------------------------------


def is_plugin_disabled_in_config(config: Any, plugin_name: str) -> bool:
    """Return True iff ``config.plugins.disabled`` contains ``plugin_name``.

    The Web API's ConfigDep reloads config per request (#2056), so
    surfaces that gate behavior on plugin-disabled state must re-check
    the **current** request config rather than the startup snapshot.
    Otherwise an operator who edits ``pollypm.toml`` to add
    ``morning_briefing`` to ``[plugins].disabled`` mid-run keeps seeing
    ``morning.available=true`` until ``pm serve`` restarts (Codex
    round-9 on #2059).

    Tolerant to objects that lack ``plugins`` / ``disabled`` (in-memory
    test configs) — returns ``False`` in that case.
    """
    plugins = getattr(config, "plugins", None)
    if plugins is None:
        return False
    disabled = getattr(plugins, "disabled", None) or ()
    return plugin_name in set(disabled)


__all__ = [
    "BriefingArtifact",
    "BriefingEntryLike",
    "BriefingProvider",
    "BriefingRenderProvider",
    "BriefingRenderRegistration",
    "get_briefing_render_provider",
    "get_briefing_render_registration",
    "is_briefing_provider_registered",
    "is_plugin_disabled_in_config",
    "list_briefings",
    "register_briefing_provider",
    "register_briefing_render_provider",
    "registered_briefing_render_types",
]
