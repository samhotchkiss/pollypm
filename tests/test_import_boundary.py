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
   the stores expose. Only the owning module or its boundary-owned helper
   modules may touch ``_conn``.

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

import ast
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
        "src/pollypm/job_runner.py",
        "src/pollypm/plugins_builtin/core_recurring/plugin.py",
        "src/pollypm/session_intelligence.py",
        "src/pollypm/workers.py",
        # TODO(#2061-followup): pre-existing direct imports in two CLI/API
        # surfaces. ``cli_features/tier4.py`` reaches for Supervisor to
        # read the ``_STORAGE_CLOSET_SESSION_SUFFIX`` class constant; the
        # P3 chat-send route does the same. Both should migrate to a
        # public ``service_api`` accessor (sibling of the round-5 work
        # that moved sessions_admin off direct imports) — tracked
        # alongside #2061 follow-up. Adding them here keeps the guardrail
        # honest while #2061 round-5 lands the sessions_admin migration
        # without bundling unrelated refactors.
        "src/pollypm/cli_features/tier4.py",
        "src/pollypm/web_api/routes/chat_send.py",
    }
)

_SUPERVISOR_IMPORT_PATTERN = re.compile(
    r"^\s*from\s+pollypm\.supervisor\s+import\s+[^\n]*\bSupervisor\b",
    re.MULTILINE,
)
_SUPERVISOR_PRIVATE_IMPORT_PATTERN = re.compile(
    r"^\s*from\s+pollypm\.supervisor\s+import\s+[^\n#]*\b_[A-Za-z0-9_]+\b",
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

# The SQLite work-service split (#365) moved implementation into
# boundary-owned ``service_*.py`` helpers. Those modules are still part of
# the owning boundary even though they live beside ``sqlite_service.py``.
_PRIVATE_CONN_OWNING_PREFIXES: tuple[str, ...] = (
    "src/pollypm/work/service_",
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


def _is_private_conn_owner(rel: str) -> bool:
    if rel in _PRIVATE_CONN_ALLOWLIST:
        return True
    return any(rel.startswith(prefix) for prefix in _PRIVATE_CONN_OWNING_PREFIXES)


# ---------------------------------------------------------------------------
# Core -> built-in plugin import slices
# ---------------------------------------------------------------------------

_PLAN_PRESENCE_PLUGIN_MODULE = (
    "pollypm.plugins_builtin.project_planning.plan_presence"
)
_TASK_ASSIGNMENT_NOTIFY_API_MODULE = (
    "pollypm.plugins_builtin.task_assignment_notify.api"
)
# ``approval_notifications`` lives in core; it must resolve the OS
# adapter via :func:`pollypm.approval_notifications.register_default_os_adapter`
# instead of reaching into the optional plugin tree.
_APPROVAL_NOTIFICATIONS_FILE = "src/pollypm/approval_notifications.py"
_HUMAN_NOTIFY_PLUGIN_PREFIX = "pollypm.plugins_builtin.human_notify"
# The dashboard reads briefings through the
# :mod:`pollypm.briefings_registry` seam, not the morning_briefing
# plugin directly (see #1363, sibling of #1597).
_DASHBOARD_SECTION_FILE = "src/pollypm/cockpit_sections/dashboard.py"
_MORNING_BRIEFING_PLUGIN_PREFIX = "pollypm.plugins_builtin.morning_briefing"
# ``doctor`` invokes the ``agent_worktree.prune`` / ``log.rotate``
# maintenance handlers via :mod:`pollypm.maintenance_handlers_registry`
# so it never imports from the optional ``core_recurring`` plugin
# directly (see #1363, sibling of #1597 / #1621).
_DOCTOR_FILE = "src/pollypm/doctor.py"
_CORE_RECURRING_PLUGIN_PREFIX = "pollypm.plugins_builtin.core_recurring"
# The cockpit dashboard's activity panel and the full-screen activity
# inbox view both resolve a feed projector through
# :func:`pollypm.activity_projector_registry.build_activity_projector`
# instead of importing ``build_projector`` from the ``activity_feed``
# plugin directly (see #1363, sibling of #1597 / #1621 / #1626).
_COCKPIT_UI_FILE = "src/pollypm/cockpit_ui.py"
_COCKPIT_INBOX_FILE = "src/pollypm/cockpit_inbox.py"
_ACTIVITY_FEED_PLUGIN_MODULE = "pollypm.plugins_builtin.activity_feed.plugin"
# The ``activity_summary`` packer is owned by core
# (:mod:`pollypm.events.summaries`). The ``activity_feed`` plugin keeps
# a backward-compat re-export at
# ``pollypm.plugins_builtin.activity_feed.summaries`` for external plugin
# consumers, but core (non-plugin) callers must import the canonical
# path so the optional-plugin contract holds (see #1363, sibling of
# #1597 / #1621 / #1626 / #1672).
_ACTIVITY_FEED_SUMMARIES_PLUGIN_MODULE = (
    "pollypm.plugins_builtin.activity_feed.summaries"
)
# ``Supervisor`` resolves the default launch planner through the plugin
# host, but the planner's context dataclass is fundamentally the host's
# contract — it must be available from a core module so the supervisor
# never has to reach into the optional plugin tree to build it. The
# canonical home is :mod:`pollypm.launch_planner_protocol`; the plugin
# re-exports for back-compat. (#1363, sibling of #1597 / #1621 / #1626 /
# #1672 / #1682.)
_SUPERVISOR_FILE = "src/pollypm/supervisor.py"
_DEFAULT_LAUNCH_PLANNER_PLUGIN_PREFIX = (
    "pollypm.plugins_builtin.default_launch_planner"
)
# The ``pm tier4`` / ``pm system`` CLI surfaces resolve tier-4 dispatch
# and audit-emit helpers through :mod:`pollypm.tier4_actions_registry`
# instead of reaching into the optional ``core_recurring`` plugin
# directly. (#1363, sibling of #1597 / #1621 / #1626 / #1672 / #1682 /
# #1702.)
_TIER4_CLI_FILE = "src/pollypm/cli_features/tier4.py"


def _imports_module(source_file: Path, module: str) -> bool:
    tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == module:
            return True
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == module or alias.name.startswith(f"{module}."):
                    return True
    return False


def _imports_module_or_subpackage(source_file: Path, prefix: str) -> bool:
    """True iff ``source_file`` imports ``prefix`` or any of its submodules."""
    tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == prefix or module.startswith(f"{prefix}."):
                return True
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == prefix or alias.name.startswith(f"{prefix}."):
                    return True
    return False


def test_non_plugin_sources_do_not_import_project_planning_plan_presence() -> None:
    """The shared plan gate lives in core, not in the optional plugin tree."""
    root = _project_root()
    offenders: list[str] = []
    for source_file in _iter_source_files(root):
        rel = _relative_posix(source_file, root)
        if "/plugins_builtin/" in rel:
            continue
        if _imports_module(source_file, _PLAN_PRESENCE_PLUGIN_MODULE):
            offenders.append(rel)
    assert offenders == []


def test_non_plugin_sources_do_not_import_task_assignment_notify_api() -> None:
    """Task-assignment notification routes through the core event bus."""
    root = _project_root()
    offenders: list[str] = []
    for source_file in _iter_source_files(root):
        rel = _relative_posix(source_file, root)
        if "/plugins_builtin/" in rel:
            continue
        if _imports_module(source_file, _TASK_ASSIGNMENT_NOTIFY_API_MODULE):
            offenders.append(rel)
    assert offenders == []


def test_dashboard_does_not_import_morning_briefing_plugin() -> None:
    """The dashboard surfaces briefings via the core registry seam.

    :func:`pollypm.briefings_registry.list_briefings` is the sanctioned
    read path. The ``morning_briefing`` plugin installs its real
    ``list_briefings`` during ``initialize``; if the dashboard reaches
    into the plugin tree directly we lose the "plugin is optional"
    contract — fail loudly so the seam stays clean.
    """
    root = _project_root()
    source_file = root / _DASHBOARD_SECTION_FILE
    assert source_file.exists(), (
        f"Expected {_DASHBOARD_SECTION_FILE} to exist — boundary test "
        "needs updating if the file moved."
    )
    assert not _imports_module_or_subpackage(
        source_file, _MORNING_BRIEFING_PLUGIN_PREFIX
    ), (
        f"{_DASHBOARD_SECTION_FILE} must not import from "
        f"{_MORNING_BRIEFING_PLUGIN_PREFIX}; use "
        "pollypm.briefings_registry.list_briefings instead so the "
        "plugin stays optional."
    )


def test_doctor_does_not_import_core_recurring_plugin() -> None:
    """``pm doctor`` invokes maintenance handlers through the core registry.

    :mod:`pollypm.maintenance_handlers_registry` is the sanctioned read
    path for ``agent_worktree.prune`` and ``log.rotate``. The
    ``core_recurring`` plugin installs the real handlers during
    ``initialize``; if ``doctor.py`` reaches into the plugin tree
    directly we re-couple core to an "optional" plugin and the
    ``--fix`` flow stops degrading cleanly when the plugin is disabled.
    Fail loudly so the seam stays clean.
    """
    root = _project_root()
    source_file = root / _DOCTOR_FILE
    assert source_file.exists(), (
        f"Expected {_DOCTOR_FILE} to exist — boundary test "
        "needs updating if the file moved."
    )
    assert not _imports_module_or_subpackage(
        source_file, _CORE_RECURRING_PLUGIN_PREFIX
    ), (
        f"{_DOCTOR_FILE} must not import from "
        f"{_CORE_RECURRING_PLUGIN_PREFIX}; use "
        "pollypm.maintenance_handlers_registry.invoke_maintenance_handler "
        "instead so the plugin stays optional."
    )


def test_cockpit_does_not_import_activity_feed_plugin_for_projector() -> None:
    """Cockpit surfaces resolve the activity projector via the core seam.

    :func:`pollypm.activity_projector_registry.build_activity_projector`
    is the sanctioned read path. The ``activity_feed`` plugin installs
    its ``build_projector`` factory during ``initialize``; if the
    dashboard panel (``cockpit_ui._dashboard_activity``) or the full-
    screen activity inbox (``cockpit_inbox._fetch_activity_entries``)
    reach into the plugin tree directly we lose the "plugin is optional"
    contract — fail loudly so the seam stays clean.

    Other ``activity_feed`` submodules (e.g. ``cockpit.feed_panel``
    Textual widgets) are intentionally NOT covered here — those are
    UI-rendering helpers tracked under a separate slice of #1363.
    """
    root = _project_root()
    offenders: list[str] = []
    for rel in (_COCKPIT_UI_FILE, _COCKPIT_INBOX_FILE):
        source_file = root / rel
        assert source_file.exists(), (
            f"Expected {rel} to exist — boundary test needs updating if "
            "the file moved."
        )
        if _imports_module(source_file, _ACTIVITY_FEED_PLUGIN_MODULE):
            offenders.append(rel)
    assert not offenders, (
        "Cockpit surfaces must not import `build_projector` from "
        f"{_ACTIVITY_FEED_PLUGIN_MODULE}; use "
        "pollypm.activity_projector_registry.build_activity_projector "
        "instead so the plugin stays optional. Offenders:\n  - "
        + "\n  - ".join(offenders)
    )


def test_non_plugin_sources_do_not_import_activity_feed_summaries_shim() -> None:
    """``activity_summary`` is owned by core, not the activity_feed plugin.

    The canonical packer lives at
    :mod:`pollypm.events.summaries`. The plugin keeps a backward-compat
    re-export so external plugin consumers (and the plugin's own
    handlers) can import either path, but core (non-plugin) callers must
    use the canonical path. Reaching into the plugin shim re-couples
    core to an "optional" plugin — fail loudly so the seam stays clean.
    """
    root = _project_root()
    offenders: list[str] = []
    for source_file in _iter_source_files(root):
        rel = _relative_posix(source_file, root)
        if "/plugins_builtin/" in rel:
            continue
        if _imports_module(source_file, _ACTIVITY_FEED_SUMMARIES_PLUGIN_MODULE):
            offenders.append(rel)
    assert not offenders, (
        "Non-plugin sources must not import from "
        f"{_ACTIVITY_FEED_SUMMARIES_PLUGIN_MODULE}; import "
        "`activity_summary` from pollypm.events.summaries (the canonical "
        "path) instead so the plugin stays optional. Offenders:\n  - "
        + "\n  - ".join(offenders)
    )


def test_supervisor_does_not_import_default_launch_planner_plugin() -> None:
    """``Supervisor`` builds the planner context from the core protocol module.

    :class:`pollypm.launch_planner_protocol.DefaultLaunchPlannerContext`
    is the sanctioned source. The default planner ships as a built-in
    plugin and is resolved through the plugin host, but the *context*
    dataclass is the host's contract — reaching into the plugin tree to
    import it would re-couple ``Supervisor`` to an "optional" plugin and
    break the seam. Fail loudly so the boundary stays clean.
    """
    root = _project_root()
    source_file = root / _SUPERVISOR_FILE
    assert source_file.exists(), (
        f"Expected {_SUPERVISOR_FILE} to exist — boundary test "
        "needs updating if the file moved."
    )
    assert not _imports_module_or_subpackage(
        source_file, _DEFAULT_LAUNCH_PLANNER_PLUGIN_PREFIX
    ), (
        f"{_SUPERVISOR_FILE} must not import from "
        f"{_DEFAULT_LAUNCH_PLANNER_PLUGIN_PREFIX}; import "
        "DefaultLaunchPlannerContext from pollypm.launch_planner_protocol "
        "instead so the plugin stays optional."
    )


def test_tier4_cli_does_not_import_core_recurring_plugin() -> None:
    """``pm tier4`` / ``pm system`` resolve tier-4 actions via the core seam.

    :mod:`pollypm.tier4_actions_registry` is the sanctioned read path for
    ``dispatch_to_operator_tier4``, ``emit_tier4_demoted``, and
    ``emit_tier4_global_action``. The ``core_recurring`` plugin installs
    the real callables during ``initialize``; if ``cli_features/tier4.py``
    reaches into the plugin tree directly we re-couple core to an
    "optional" plugin and the CLI stops degrading cleanly when the
    plugin is disabled. Fail loudly so the seam stays clean.
    """
    root = _project_root()
    source_file = root / _TIER4_CLI_FILE
    assert source_file.exists(), (
        f"Expected {_TIER4_CLI_FILE} to exist — boundary test "
        "needs updating if the file moved."
    )
    assert not _imports_module_or_subpackage(
        source_file, _CORE_RECURRING_PLUGIN_PREFIX
    ), (
        f"{_TIER4_CLI_FILE} must not import from "
        f"{_CORE_RECURRING_PLUGIN_PREFIX}; use "
        "pollypm.tier4_actions_registry (dispatch_to_operator_tier4, "
        "emit_tier4_demoted, emit_tier4_global_action) instead so the "
        "plugin stays optional."
    )


def test_approval_notifications_does_not_import_human_notify_plugin() -> None:
    """Core approval flow must not reach into the human_notify plugin.

    The OS adapter is installed via
    :func:`pollypm.approval_notifications.register_default_os_adapter`
    during plugin initialize. Any direct import from
    ``pollypm.plugins_builtin.human_notify`` re-couples core to the
    optional plugin tree — fail loudly so the seam stays clean.
    """
    root = _project_root()
    source_file = root / _APPROVAL_NOTIFICATIONS_FILE
    assert source_file.exists(), (
        f"Expected {_APPROVAL_NOTIFICATIONS_FILE} to exist — boundary test "
        "needs updating if the file moved."
    )
    assert not _imports_module_or_subpackage(
        source_file, _HUMAN_NOTIFY_PLUGIN_PREFIX
    ), (
        f"{_APPROVAL_NOTIFICATIONS_FILE} must not import from "
        f"{_HUMAN_NOTIFY_PLUGIN_PREFIX}; use register_default_os_adapter "
        "instead so the plugin stays optional."
    )


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


def test_no_private_supervisor_imports_outside_supervisor() -> None:
    """Private helpers in ``supervisor.py`` are not a public dependency."""
    root = _project_root()
    offenders: list[str] = []
    for source_file in _iter_source_files(root):
        rel = _relative_posix(source_file, root)
        if rel == "src/pollypm/supervisor.py":
            continue
        text = source_file.read_text(encoding="utf-8")
        if _SUPERVISOR_PRIVATE_IMPORT_PATTERN.search(text):
            offenders.append(rel)

    assert not offenders, (
        "Private imports from `pollypm.supervisor` are forbidden. Promote "
        "the helper into a public module first, then import that instead. "
        "Offenders:\n  - " + "\n  - ".join(offenders)
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


def test_supervisor_reach_pattern_skips_capitalized_class_refs() -> None:
    """Regression for #1677: docstring class references must not trip the gate.

    The reach-through scanner is intended to flag *runtime* attribute access
    like ``supervisor._foo``, not documentation that references the
    :class:`Supervisor` class itself (e.g. ``Supervisor._assert_...`` in a
    docstring ``:meth:`` cross-ref). The pattern is case-sensitive on the
    leading ``s`` for exactly this reason; if that ever changes, every
    Sphinx-style class reference would suddenly be an offender.
    """
    # Capitalized class references in docstrings — must NOT match.
    assert _SUPERVISOR_REACH_PATTERN.search("Supervisor._assert_session_launch_matches") is None
    assert _SUPERVISOR_REACH_PATTERN.search(":meth:`Supervisor._foo`") is None
    # Lowercase variable reach-through — MUST match.
    assert _SUPERVISOR_REACH_PATTERN.search("supervisor._assert_session_launch_matches(x)") is not None
    assert _SUPERVISOR_REACH_PATTERN.search("self.supervisor._window_map()") is not None


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
        if _is_private_conn_owner(rel):
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


def test_worker_launch_contract_routes_through_provider_runtime_adapters() -> None:
    """Worker launch planning must use the public provider/runtime seams."""
    root = _project_root()
    source = (
        root / "src" / "pollypm" / "work" / "session_manager.py"
    ).read_text(encoding="utf-8")

    required_snippets = (
        "from pollypm.providers import get_provider",
        "from pollypm.runtimes import get_runtime",
        "provider.build_launch_command(session, account)",
        "runtime.wrap_command(launch, account, config.project)",
    )
    missing = [snippet for snippet in required_snippets if snippet not in source]
    assert not missing, (
        "Worker launch must route through provider/runtime adapters. "
        "Missing required session-manager snippets:\n  - "
        + "\n  - ".join(missing)
    )

    forbidden_snippets = (
        'provider="claude"',
        'runtime="local"',
    )
    present = [snippet for snippet in forbidden_snippets if snippet in source]
    assert not present, (
        "Worker launch must not hardcode provider/runtime ownership. "
        "Found forbidden snippets:\n  - " + "\n  - ".join(present)
    )
