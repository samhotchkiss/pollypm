"""Single source of truth for "where does agent-X's identity live" (#763).

Every agent assigned to a project should resolve its role guide to an
**absolute path inside that project's ``.pollypm/`` directory** — never
a path into PollyPM's install tree, never a relative path that depends
on the session's working directory. This module provides the one
helper every reader (upgrade-notice, supervisor, session-manager,
doctor) should go through.

Resolution policy:

1. **Project-local fork** under ``<project>/.pollypm/project-guides/<role>.md``
   — written by ``pm project init-guide`` (#733) or by
   :func:`materialize_role_guides` (below) on project scaffold.
2. **Built-in shipped guide** under
   ``pollypm/plugins_builtin/.../<role>.md`` — absolute path to the
   package-shipped markdown file. Used only when a project-local copy
   is absent AND materialization has never run for that project.

The materialization step closes the gap: on every
:func:`ensure_project_scaffold` call (and on ``pm project new``), we
copy the current built-in guides into ``<project>/.pollypm/project-guides/``
with a ``forked_from: <sha>`` header so downstream code can always
count on an in-project absolute path existing.

Subsumes the path-resolution parts of #755, #756, #762 that previously
lived in ``upgrade_notice._ROLE_GUIDES``.
"""

from __future__ import annotations

import logging
from pathlib import Path


logger = logging.getLogger(__name__)


# Public set of roles that have a materializable guide. operator-pm
# (Polly) isn't here because Polly is global-only by design — the
# operator guide lives in the PollyPM package and is shared across
# projects; there's no "polly per project."
MATERIALIZABLE_ROLES: tuple[str, ...] = ("architect", "worker", "reviewer")


def role_guide_path(project_path: Path, role: str) -> Path:
    """Return the absolute path of ``role``'s guide for this project.

    Never returns a path outside the project's ``.pollypm/`` directory
    for the three project-scoped roles. Operator-PM (Polly) returns
    the built-in package-shipped path because the operator guide is
    global — every project shares one Polly. Unknown roles fall back
    to the worker guide.

    Resolution:

    - Project-local fork present → return that path.
    - Project-local fork absent → **create it now** via
      :func:`materialize_role_guides` so the call-site always gets an
      in-project path. If materialization fails (e.g. the project
      directory doesn't exist on disk yet), fall back to the built-in
      absolute path so the caller still has a usable target.
    """
    from pollypm.project_guides import (
        SUPPORTED_PROJECT_GUIDE_ROLES,
        built_in_guide_source_path,
        normalize_project_guide_role,
        project_guide_path as _project_guide_path,
    )

    if role == "operator-pm":
        return _built_in_operator_guide_path()

    try:
        normalized = normalize_project_guide_role(role)
    except ValueError:
        normalized = "worker"
    if normalized not in SUPPORTED_PROJECT_GUIDE_ROLES:
        normalized = "worker"

    try:
        local = _project_guide_path(project_path, normalized)
    except Exception:  # noqa: BLE001
        local = None

    if local is not None and local.is_file():
        return local.resolve()

    # Try to materialize on demand so the caller gets an in-project
    # path even if scaffold hasn't run yet. Idempotent.
    try:
        materialize_role_guides(project_path, roles=(normalized,))
        if local is not None and local.is_file():
            return local.resolve()
    except Exception:  # noqa: BLE001
        pass

    # Last resort: the shipped built-in. Always absolute.
    try:
        built_in = built_in_guide_source_path(normalized)
    except Exception:  # noqa: BLE001
        built_in = None
    if built_in is not None and built_in.exists():
        return built_in.resolve()

    # Pathological fallback: return the (possibly non-existent) local
    # path so at least a consistent string is returned.
    if local is not None:
        return local
    return project_path / ".pollypm" / "project-guides" / f"{normalized}.md"


def _built_in_operator_guide_path() -> Path:
    """Absolute path to the shipped operator-pm (Polly) guide.

    Polly is global-only — there's no per-project operator guide — so
    this always returns the package-shipped path.
    """
    base = Path(__file__).resolve().parent
    return (
        base
        / "plugins_builtin"
        / "core_agent_profiles"
        / "profiles"
        / "polly-operator-guide.md"
    )


def materialize_role_guides(
    project_path: Path,
    *,
    roles: tuple[str, ...] = MATERIALIZABLE_ROLES,
    force: bool = False,
) -> list[Path]:
    """Copy built-in role guides into ``<project>/.pollypm/project-guides/``.

    Called by :func:`pollypm.projects.ensure_project_scaffold` on every
    project init so the in-project absolute path to each role guide is
    always present. Idempotent by default: skips a role whose target
    file already exists. Pass ``force=True`` to re-materialize even
    when the file exists (used on PollyPM upgrade when a guide's
    upstream content has changed).

    Returns the list of file paths that were actually written (empty
    when all targets already existed and ``force=False``).
    """
    from pollypm.project_guides import (
        built_in_guide_fork_ref,
        built_in_guide_source_path,
        built_in_guide_text,
        normalize_project_guide_role,
        project_guide_path as _project_guide_path,
        project_guides_dir,
    )

    project_path = Path(project_path)
    if not project_path.exists():
        # Don't create a project directory here; that's scaffold's job.
        # Materialize-on-demand during path resolution may hit this
        # when the project is virtual / missing on disk — that's fine.
        return []

    guides_dir = project_guides_dir(project_path)
    try:
        guides_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning(
            "materialize_role_guides: cannot create %s: %s", guides_dir, exc,
        )
        return []

    written: list[Path] = []
    for role in roles:
        try:
            normalized = normalize_project_guide_role(role)
        except ValueError:
            continue
        target = _project_guide_path(project_path, normalized)
        if target.exists() and not force:
            continue
        try:
            content = built_in_guide_text(normalized)
            source_path = built_in_guide_source_path(normalized)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "materialize_role_guides: cannot load built-in for %s: %s",
                normalized, exc,
            )
            continue
        forked_from = built_in_guide_fork_ref(
            normalized,
            content=content,
            source_path=source_path,
        )
        front_matter = f"---\nforked_from: {forked_from}\n---\n\n"
        try:
            target.write_text(front_matter + content.rstrip() + "\n")
        except OSError as exc:
            logger.warning(
                "materialize_role_guides: failed to write %s: %s", target, exc,
            )
            continue
        written.append(target)
    return written


__all__ = [
    "MATERIALIZABLE_ROLES",
    "role_guide_path",
    "materialize_role_guides",
]
