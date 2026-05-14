"""Shared FastAPI dependencies for the route modules.

Owns the ``ConfigDep`` annotated alias so endpoints can ``def
endpoint(config: ConfigDep)`` without each module importing the same
plumbing. The provider for ``ConfigDep`` is wired in
:mod:`pollypm.web_api.app` via ``dependency_overrides``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from pollypm.config import PollyPMConfig


def _config_provider() -> PollyPMConfig:
    """Default provider — overridden by the app factory at startup.

    Raising here makes a misconfigured app fail loudly during the
    first request rather than silently loading the user's real
    config.
    """
    raise RuntimeError(
        "PollyPMConfig provider not configured; the app factory must "
        "override `_config_provider` via `app.dependency_overrides`."
    )


ConfigDep = Annotated[PollyPMConfig, Depends(_config_provider)]


__all__ = ["ConfigDep", "_config_provider"]
