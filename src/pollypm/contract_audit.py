"""Prompt / docs / CLI contract verification helpers (#888).

The pre-launch audit (``docs/launch-issue-audit-2026-04-27.md``
§7) cites the recurring shape: prompts, docs, worker guides,
and UI copy reference commands, files, paths, roles, or schemas
that do not exist in the current app.

* `#471` — referenced ``pm mail``, which did not exist.
* `#476` — referenced ``pm inbox show <id>`` without verifying
  the subcommand.
* `#387` — told users ``--actor user`` while the node required
  ``--actor reviewer``.
* `#851` — a normal missing-role validation surfaced as a Rich/
  Python traceback.

Existing tests (``test_prompt_command_references.py``,
``test_docs_command_references.py``) cover the ``pm <command>``
references. This module adds the *other* contract dimensions
the audit calls out:

* actor names referenced in docs / prompts must exist in flows;
* flow node names / enum values referenced in docs must exist;
* role-guide paths referenced in runtime instructions must
  resolve from the session cwd;
* expected work-service validation errors render clean (no
  Rich/Python traceback);
* generated prompt / doc snippets are marked.

Architecture: helpers in this module are pure, importable, and
reusable. The release gate (#889) calls them; the contract
tests (``tests/test_contract_audit.py``) lock the invariants.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


# ---------------------------------------------------------------------------
# Public regex patterns
# ---------------------------------------------------------------------------


_ACTOR_FLAG_RE = re.compile(r"--actor\s+([a-z][a-z0-9_-]*)")
"""Matches ``--actor <name>`` references in prose / code blocks."""


_NODE_ID_BACKTICK_RE = re.compile(r"`([a-z][a-z0-9_]+)`")
"""Matches single-quoted-with-backticks tokens for node-id audit."""


_GENERATED_MARKER = "<!-- generated-from-cli -->"
"""Marker every CLI-generated snippet must carry. The audit
surfaces snippets without this marker as drift candidates."""


# ---------------------------------------------------------------------------
# Reference extraction
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReferencedActor:
    path: Path
    line: int
    actor: str


def extract_actor_references(text: str, *, path: Path) -> tuple[ReferencedActor, ...]:
    """Return every ``--actor <name>`` reference in ``text``."""
    out: list[ReferencedActor] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        for match in _ACTOR_FLAG_RE.finditer(line):
            out.append(
                ReferencedActor(
                    path=path,
                    line=line_no,
                    actor=match.group(1),
                )
            )
    return tuple(out)


def known_actor_names() -> frozenset[str]:
    """Return the canonical set of actor names referenced in CLI flags.

    The audit (#387) cites the failure shape: docs reference
    ``--actor user`` while the flow requires ``--actor reviewer``.
    The canonical set is small and stable: it derives from the
    role contract (#885) plus a few CLI-only sentinel actors
    (``user``, ``polly``, ``system``)."""
    try:
        from pollypm.role_contract import ROLE_REGISTRY
    except ImportError:
        # Defensive — keep the audit usable even before the role
        # contract is available in older snapshots.
        ROLE_REGISTRY = {}  # type: ignore[assignment]

    role_keys = set(ROLE_REGISTRY.keys())
    # CLI sentinels — used by ``pm task mine`` and similar where
    # ``--actor user`` is the legitimate value.
    sentinel_actors = {"user", "polly", "system"}
    # Also accept the persona forms (lowercase) since some docs
    # use the human-readable names.
    persona_aliases: set[str] = set()
    for contract in ROLE_REGISTRY.values():
        persona_aliases.add(contract.persona_name.lower())
    # Hyphen-form versions for legacy compatibility.
    hyphen_forms = {key.replace("_", "-") for key in role_keys}
    return frozenset(role_keys | sentinel_actors | persona_aliases | hyphen_forms)


# ---------------------------------------------------------------------------
# Flow node / role-guide path verification
# ---------------------------------------------------------------------------


def known_role_guide_paths() -> frozenset[Path]:
    """Return the canonical set of role-guide paths.

    The audit (#258 / #390) cites the recurring failure shape:
    runtime instructions point at ``src/...`` paths that aren't
    valid from the session cwd. The contract here is: any guide
    path the role contract names must exist on disk."""
    try:
        from pollypm.role_contract import ROLE_REGISTRY
    except ImportError:
        return frozenset()
    paths: set[Path] = set()
    repo_root = _repo_root()
    for contract in ROLE_REGISTRY.values():
        if contract.guide_path is None:
            continue
        paths.add((repo_root / contract.guide_path).resolve())
    return frozenset(paths)


def role_guide_paths_exist() -> tuple[str, ...]:
    """Return one human-readable line per missing guide.

    Empty tuple means every guide path resolves on disk. The
    release gate (#889) consults this."""
    out: list[str] = []
    for path in known_role_guide_paths():
        if not path.exists():
            out.append(f"role guide path not on disk: {path}")
    return tuple(out)


def legacy_role_guide_paths_exist() -> tuple[str, ...]:
    """#897 — verify any legacy ``role -> guide_path`` table the
    heartbeat still publishes resolves on disk.

    The audit's #897 criterion: *"Add a contract audit that
    rejects nonexistent guide paths in legacy/runtime tables, not
    only paths declared by ROLE_REGISTRY."* Today the heartbeat
    derives its table from the registry, so this check
    *should* be redundant — but the audit is defensive: a future
    refactor that re-introduces a hand-maintained legacy table
    will trip this check immediately if any path is wrong.
    """
    out: list[str] = []
    repo_root = _repo_root()
    try:
        from pollypm.heartbeats.local import _ROLE_GUIDE_PATHS
    except ImportError:
        return ()
    for role_key, guide_path in _ROLE_GUIDE_PATHS.items():
        absolute = (repo_root / guide_path).resolve()
        if not absolute.exists():
            out.append(
                f"legacy heartbeat guide path for {role_key!r} "
                f"missing on disk: {absolute}"
            )
    return tuple(out)


# ---------------------------------------------------------------------------
# Traceback-shape detection (#851)
# ---------------------------------------------------------------------------


_TRACEBACK_FRAGMENTS: tuple[str, ...] = (
    "Traceback (most recent call last):",
    'File "',
    "  File \"",
    "raise ",
)


def looks_like_rich_traceback(text: str) -> bool:
    """Heuristically detect whether ``text`` contains a Python
    traceback that has reached a user-facing error path.

    The audit cites #851: a normal missing-role validation
    surfaced as a Rich/Python traceback. Anywhere a CLI handler
    produces error text, this check should be False."""
    if not text:
        return False
    hits = sum(1 for frag in _TRACEBACK_FRAGMENTS if frag in text)
    return hits >= 2


# ---------------------------------------------------------------------------
# Generated-snippet marking
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UnmarkedSnippet:
    path: Path
    detail: str


def find_unmarked_generated_snippets(
    paths: Iterable[Path],
    *,
    cli_dependent_filenames: tuple[str, ...] = ("cli-reference.md",),
) -> tuple[UnmarkedSnippet, ...]:
    """Return doc paths whose name suggests CLI-generated content
    but which lack the canonical generated marker.

    Migration policy: docs that are intentionally generated from
    Typer command metadata should carry
    :data:`_GENERATED_MARKER`. Future edits that drift from the
    Typer schema then produce a CI signal."""
    out: list[UnmarkedSnippet] = []
    for path in paths:
        if not path.exists():
            continue
        if not any(name in path.name for name in cli_dependent_filenames):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if _GENERATED_MARKER in text:
            continue
        out.append(
            UnmarkedSnippet(
                path=path,
                detail=(
                    "filename suggests CLI-generated content but the "
                    f"{_GENERATED_MARKER!r} marker is missing"
                ),
            )
        )
    return tuple(out)


# ---------------------------------------------------------------------------
# Worker-guide duplication detection
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class WorkerGuideDuplicate:
    path: Path
    canonical_section_count: int
    detail: str = ""


def detect_worker_guide_duplication(
    *,
    canonical_marker: str = "## Your job in one paragraph",
    paths: Iterable[Path] | None = None,
) -> tuple[WorkerGuideDuplicate, ...]:
    """Return docs/snippets that contain the canonical worker-
    guide section header more than once.

    The audit (#487 / #488 / #489 / #490) cites the recurring
    shape: worker-guide content gets injected twice into the
    same prompt because two emitters each prepend it without
    coordinating. A duplicate canonical-section heading is a
    cheap detector."""
    paths = list(paths or _default_worker_guide_search_paths())
    out: list[WorkerGuideDuplicate] = []
    for path in paths:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        count = text.count(canonical_marker)
        if count >= 2:
            out.append(
                WorkerGuideDuplicate(
                    path=path,
                    canonical_section_count=count,
                    detail=(
                        f"{count} copies of canonical worker-guide "
                        f"section in one document"
                    ),
                )
            )
    return tuple(out)


def _default_worker_guide_search_paths() -> tuple[Path, ...]:
    """Default worker-guide search set used by the doctor / gate."""
    repo = _repo_root()
    return (
        repo / "docs" / "worker-guide.md",
        repo / "docs" / "worker-onboarding.md",
        repo / "src" / "pollypm" / "memory_prompts.py",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    """Return the PollyPM repo root (``../../`` from this file)."""
    here = Path(__file__).resolve()
    return here.parent.parent.parent
