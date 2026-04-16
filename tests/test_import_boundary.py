"""Import boundary: prevent ad-hoc ``from pollypm.supervisor import Supervisor``.

The Supervisor decomposition (issue #179 and its step children) moves the
"public surface" to :mod:`pollypm.service_api` (currently v1). Outside of
:mod:`pollypm.core` — which owns Supervisor construction itself — and tests,
no module should import :class:`pollypm.supervisor.Supervisor` directly.

This test enforces the boundary via an explicit allow-list. The initial
allow-list is generous: every file that imports Supervisor on the day
Step 5 (#182) landed is listed with a TODO. As Steps 6 (#183) and 8
(#185) migrate the TUI/CLI and inbox/plugin callers to the service_api
facade, entries are removed. When the allow-list is empty the test
becomes a strict guard.

Mechanism
---------
We walk every ``*.py`` under ``src/pollypm`` and regex-scan for imports of
``Supervisor`` from ``pollypm.supervisor``. A file is a violation if it
has such an import *and* is neither inside ``pollypm/core/`` nor on the
explicit allow-list below.
"""

from __future__ import annotations

import re
from pathlib import Path

# Allow-list of files that still import Supervisor directly as of #183.
# Each entry is a POSIX-style path relative to the project root.
#
# Step 6 (#183) landed — TUI/CLI/cockpit surfaces now route through
# pollypm.service_api.v1 instead of importing Supervisor directly.
#
# TODO(#185): Step 8 migrates inbox + plugin + scheduler + worker
#             integration points — remove the remaining entries.
_SUPERVISOR_IMPORT_ALLOWLIST: frozenset[str] = frozenset(
    {
        # Facade: this IS the sanctioned wrapper; stays until v1.1 if ever.
        "src/pollypm/service_api/v1.py",
        # Step 8 targets (internal integrations — migrate to CoreRail /
        # service_api as the rail grows):
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


def _project_root() -> Path:
    # tests/ sits next to src/; walk up until we find pyproject.toml.
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


def test_supervisor_import_allowlist_matches_reality() -> None:
    """Every file with a direct Supervisor import must be on the allow-list.

    If this fails, either:

    - You added a new direct ``from pollypm.supervisor import Supervisor``.
      Prefer :mod:`pollypm.service_api` instead.
    - You intentionally need one (e.g. as part of the core decomposition).
      Add the file to ``_SUPERVISOR_IMPORT_ALLOWLIST`` with a TODO pointing
      to the issue that will remove it.
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

    Keeps the allow-list honest as Steps 6 / 8 land: once a caller is
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
