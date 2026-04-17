"""Import-boundary guardrails for the Supervisor decomposition.

These tests encode "what's no longer allowed" after the Supervisor split
(issues #179, #182, #186, #187). CI runs them on every PR so regressing
into any of the old cross-module reach-through patterns fails loudly.

Guardrails
----------

1. **Supervisor construction lives inside the core rail / service facade.**
   Direct ``from pollypm.supervisor import Supervisor`` is allowed only in
   :mod:`pollypm.core`, the ``service_api`` facade, and a tightly scoped
   allow-list of integration points (:data:`_SUPERVISOR_IMPORT_ALLOWLIST`
   below). Everyone else must go through :mod:`pollypm.service_api`.

2. **No private-attribute reach-through on Supervisor-like objects.**
   Patterns like ``supervisor._launch_by_session(...)`` or
   ``sup._window_map()`` bypass the public API. Public methods exist for
   each of these; use them.

3. **No direct SQL on ``StateStore._conn`` / ``SQLiteWorkService._conn``.**
   Those connections are private — callers use the typed accessor methods
   the stores expose. Only the owning module may touch ``_conn``.

Allow-list format
-----------------

Each guardrail maintains its own ``frozenset`` of POSIX-style paths
(relative to the project root). Entries are **temporary** — every entry
should come with a ``TODO`` comment pointing at the issue that will
remove it. The companion ``*_has_no_stale_entries`` tests fail if an
allow-listed file no longer trips the rule, so the list tightens
automatically as code migrates.

Adding a new entry is an active choice: reviewers should ask "why is
this unavoidable?" before merging.
"""

from __future__ import annotations

import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Supervisor direct-import allow-list
# ---------------------------------------------------------------------------

# Allow-list of files that may ``from pollypm.supervisor import Supervisor``.
# Each entry is a POSIX-style path relative to the project root.
#
# The Supervisor decomposition (#179) moved the public surface to
# :mod:`pollypm.service_api.v1`. As Steps 6/8 migrated TUI/CLI/inbox/plugin
# callers, entries came off the list. The remaining entries are internal
# integration points that still need Supervisor directly; each is TODO-tagged.
_SUPERVISOR_IMPORT_ALLOWLIST: frozenset[str] = frozenset(
    {
        # Facade: this IS the sanctioned wrapper. Stays until a future v1.1
        # facade refactor absorbs it.
        "src/pollypm/service_api/v1.py",
        # TODO(#179+): Remaining internal integration points that still need
        # a direct Supervisor — migrate each onto CoreRail / service_api as
        # the rail grows (tracked under the decomposition meta-issue).
        "src/pollypm/heartbeats/api.py",
        "src/pollypm/job_runner.py",
        "src/pollypm/plugins_builtin/core_recurring/plugin.py",
        "src/pollypm/schedulers/base.py",
        "src/pollypm/session_intelligence.py",
        "src/pollypm/workers.py",
    }
)

_SUPERVISOR_IMPORT_PATTERN = re.compile(
    r"^\s*from\s+pollypm\.supervisor\s+import\s+[^\n]*\bSupervisor\b",
    re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Supervisor private-method reach-through
# ---------------------------------------------------------------------------

# Allow-list of files that may reach into ``supervisor._foo`` style private
# attributes. These are either owning modules or tightly scoped bridges.
_SUPERVISOR_REACH_ALLOWLIST: frozenset[str] = frozenset(
    {
        # Supervisor owns its own private attributes — self._foo is fine.
        "src/pollypm/supervisor.py",
    }
)

# Match ``<name>._<lowercase-start>`` where ``<name>`` begins with a
# lowercase ``s`` (variable convention) and contains ``up`` — i.e.
# ``supervisor._window_map``, ``sup._launch_by_session``,
# ``self.supervisor._foo``. Capitalized forms like ``Supervisor._foo``
# are assumed to be documentation references to the class itself and
# are skipped. False positives can be allow-listed with a TODO.
_SUPERVISOR_REACH_PATTERN = re.compile(r"\bsup\w*\._[a-z]")


# ---------------------------------------------------------------------------
# Private SQLite connection reach-through
# ---------------------------------------------------------------------------

# Allow-list of files that may touch ``<X>._conn`` where X is a StateStore
# or SQLiteWorkService instance. Only the owning modules qualify.
_PRIVATE_CONN_ALLOWLIST: frozenset[str] = frozenset(
    {
        "src/pollypm/storage/state.py",
        "src/pollypm/work/sqlite_service.py",
    }
)

# Symbol names whose ``._conn`` attribute is the guarded private connection.
# ``jobs.JobQueue`` also exposes ``_conn`` but that's a different store and
# isn't part of this guardrail (separate tracking issue).
_PRIVATE_CONN_CLASSES = ("StateStore", "SQLiteWorkService")

# Regex: look for identifiers that plausibly name a StateStore /
# SQLiteWorkService instance and then do ``._conn.``. This is a heuristic —
# instance names like ``store``, ``state_store``, ``svc`` are the common
# cases. We couple the attribute match with a class-name presence check
# elsewhere in the file to reduce false positives on unrelated ``_conn``.
_PRIVATE_CONN_PATTERN = re.compile(r"\b\w+\._conn\.")


def _project_root() -> Path:
    """Find the project root by walking up from the test file."""
    here = Path(__file__).resolve()
    for candidate in (here, *here.parents):
        if (candidate / "pyproject.toml").exists():
            return candidate
    raise RuntimeError("Could not locate project root (no pyproject.toml found)")


def _iter_source_files(root: Path) -> list[Path]:
    src_root = root / "src" / "pollypm"
    return sorted(p for p in src_root.rglob("*.py"))


def _relative_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


# ---------------------------------------------------------------------------
# Guardrail 1: direct Supervisor imports
# ---------------------------------------------------------------------------


def test_supervisor_import_allowlist_matches_reality() -> None:
    """Every file with a direct Supervisor import must be on the allow-list.

    If this fails, either:

    - You added a new ``from pollypm.supervisor import Supervisor`` —
      prefer :mod:`pollypm.service_api` instead.
    - You intentionally need one (e.g. as part of a core-decomposition
      step). Add the file to ``_SUPERVISOR_IMPORT_ALLOWLIST`` with a
      TODO pointing at the issue that will remove it.
    """
    root = _project_root()
    offenders: list[str] = []
    for source_file in _iter_source_files(root):
        rel = _relative_posix(source_file, root)
        # Core is exempt — it owns Supervisor construction.
        if rel.startswith("src/pollypm/core/"):
            continue
        text = source_file.read_text(encoding="utf-8")
        if not _SUPERVISOR_IMPORT_PATTERN.search(text):
            continue
        if rel in _SUPERVISOR_IMPORT_ALLOWLIST:
            continue
        offenders.append(rel)

    assert not offenders, (
        "Direct `from pollypm.supervisor import Supervisor` is deprecated "
        "outside pollypm.core/. Migrate to pollypm.service_api, or (if "
        "unavoidable) add the file to _SUPERVISOR_IMPORT_ALLOWLIST with a "
        "TODO pointing at the issue that will remove it. Offenders:\n  - "
        + "\n  - ".join(offenders)
    )


def test_supervisor_import_allowlist_has_no_stale_entries() -> None:
    """Allow-list entries must correspond to real files that still import Supervisor.

    Keeps the allow-list honest as migrations land: once a caller is
    migrated, its entry must be removed so the boundary tightens
    automatically.
    """
    root = _project_root()
    stale: list[str] = []
    for rel in _SUPERVISOR_IMPORT_ALLOWLIST:
        path = root / rel
        if not path.exists():
            stale.append(f"{rel} (file missing)")
            continue
        text = path.read_text(encoding="utf-8")
        if not _SUPERVISOR_IMPORT_PATTERN.search(text):
            stale.append(f"{rel} (no Supervisor import found — shrink the list!)")

    assert not stale, (
        "Stale entries in _SUPERVISOR_IMPORT_ALLOWLIST — remove them "
        "(boundary tightening is the whole point):\n  - " + "\n  - ".join(stale)
    )


# ---------------------------------------------------------------------------
# Guardrail 2: Supervisor private-method reach-through
# ---------------------------------------------------------------------------


def test_no_supervisor_private_reach_through() -> None:
    """Callers must not reach into ``supervisor._<private>`` attributes.

    Public methods exist for each previously-reached-through helper
    (e.g. ``launch_by_session``, ``window_map``, ``write_snapshot``).
    If you genuinely need a private helper, promote it to public on
    :class:`Supervisor` first, then update the caller.
    """
    root = _project_root()
    offenders: list[tuple[str, int, str]] = []
    for source_file in _iter_source_files(root):
        rel = _relative_posix(source_file, root)
        if rel in _SUPERVISOR_REACH_ALLOWLIST:
            continue
        text = source_file.read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), start=1):
            # Skip comments and docstrings — they're allowed to reference
            # the old pattern (e.g. migration notes).
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            match = _SUPERVISOR_REACH_PATTERN.search(line)
            if match is None:
                continue
            # ``self._foo`` inside supervisor.py would be filtered by
            # allow-list, but we also skip ``self._`` and ``cls._`` globally —
            # those never match a "supervisor-like" identifier anyway.
            offenders.append((rel, line_number, line.strip()))

    assert not offenders, (
        "Private reach-through on a Supervisor-like object is forbidden "
        "outside Supervisor itself. Promote the helper to the public API "
        "and update the caller, or (if truly unavoidable) add the file "
        "to _SUPERVISOR_REACH_ALLOWLIST with a TODO. Offenders:\n  - "
        + "\n  - ".join(f"{path}:{lineno}: {content}" for path, lineno, content in offenders)
    )


# ---------------------------------------------------------------------------
# Guardrail 3: private SQLite connection reach-through
# ---------------------------------------------------------------------------


def test_no_private_sqlite_conn_access_outside_owning_modules() -> None:
    """``StateStore._conn`` / ``SQLiteWorkService._conn`` are private.

    Outside the module that owns the store, go through the public
    accessor methods (``execute``, typed queries, etc.). This keeps the
    schema and connection lifecycle owned by one place.

    The heuristic pairs a ``.\\_conn.`` attribute access with a filename-
    level presence of one of the owning class names (or an instance
    name that's clearly bound to such a store — ``store``, ``svc``).
    False positives can be added to ``_PRIVATE_CONN_ALLOWLIST`` with
    a TODO.
    """
    root = _project_root()
    offenders: list[tuple[str, int, str]] = []
    for source_file in _iter_source_files(root):
        rel = _relative_posix(source_file, root)
        if rel in _PRIVATE_CONN_ALLOWLIST:
            continue
        text = source_file.read_text(encoding="utf-8")
        # Fast exit: if none of the guarded class names appear and there's
        # no ``_conn`` at all in the file, skip.
        if "._conn" not in text:
            continue
        mentions_guarded_class = any(
            class_name in text for class_name in _PRIVATE_CONN_CLASSES
        )
        if not mentions_guarded_class:
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if _PRIVATE_CONN_PATTERN.search(line) is None:
                continue
            # Ignore attribute self-assignment / definition within
            # sibling files — a file that both mentions StateStore in a
            # type hint AND assigns to its own ``self._conn`` (e.g. a
            # subclass) is always going to trip this. Allow-list those
            # deliberately.
            if line.lstrip().startswith(("self._conn", "cls._conn")):
                # Only allowed when the file is itself an owning module;
                # those are in _PRIVATE_CONN_ALLOWLIST already, so any hit
                # here is a real violation.
                offenders.append((rel, line_number, line.strip()))
                continue
            offenders.append((rel, line_number, line.strip()))

    assert not offenders, (
        "Private SQLite connection access on StateStore / SQLiteWorkService "
        "is not allowed outside the owning modules. Use the public typed "
        "accessors, or (if truly unavoidable) add the file to "
        "_PRIVATE_CONN_ALLOWLIST with a TODO. Offenders:\n  - "
        + "\n  - ".join(f"{path}:{lineno}: {content}" for path, lineno, content in offenders)
    )
