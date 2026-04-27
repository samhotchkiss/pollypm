"""Release verification gate for issue closure and regressions (#889).

A composable set of gates that the launch-hardening release
process consults before tagging v1. Each gate is a pure function
that returns a :class:`GateResult`; the gate run aggregates them
into a single :class:`ReleaseReport` with a ``blocked`` flag.

The pre-launch audit (``docs/launch-issue-audit-2026-04-27.md``
§8) cites the recurring shape of process failures:

* `#395` / `#501` / `#505` / `#511` / `#513` / `#515` — issues
  closed as fixed against local branch state, then reopened
  after checking ``origin/main``.
* `#840` / `#831` / `#829` / `#826` / `#820` — fixes that
  passed narrow unit tests but reproduced after a cockpit
  restart or in the rendered UI.
* `#821` regressed `#514`, `#820` regressed `#799`, `#819`
  regressed `#792` — one-day cockpit regressions in launch-
  critical surfaces.
* `#709` — main red with 12 failures and 10 errors blocking
  the desired CI gate.

The gate is the structural fix. Each gate is a small, named
predicate so the user-visible report explains exactly why the
release is blocked. Gates are not opinionated about *fixing*
problems — they only report.

Usage::

    from pollypm.release_gate import run_release_gate

    report = run_release_gate()
    if report.blocked:
        print(report.render())
        sys.exit(1)

The gate is invoked by ``scripts/release_burnin.py`` and (when
wired) by the GitHub release workflow.
"""

from __future__ import annotations

import enum
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


class GateSeverity(enum.Enum):
    """How a failed gate affects the release decision.

    ``BLOCKING`` failures set the report's ``blocked`` flag.
    ``WARNING`` failures surface in the report but do not block.
    """

    BLOCKING = "blocking"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class GateResult:
    """The outcome of one gate."""

    name: str
    """Stable identifier (snake_case)."""

    passed: bool
    """``True`` when the gate's invariant holds."""

    severity: GateSeverity = GateSeverity.BLOCKING
    """Effect on the release decision when ``passed`` is False."""

    summary: str = ""
    """One-line human-readable summary of the result."""

    detail: str = ""
    """Optional multi-line elaboration. Surfaced in the report."""


@dataclass(slots=True)
class ReleaseReport:
    """Aggregated result of every gate."""

    results: list[GateResult] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        """``True`` iff any blocking gate failed."""
        return any(
            (not r.passed) and r.severity is GateSeverity.BLOCKING
            for r in self.results
        )

    @property
    def warnings(self) -> tuple[GateResult, ...]:
        """Failed warning-severity gates (non-blocking)."""
        return tuple(
            r
            for r in self.results
            if (not r.passed) and r.severity is GateSeverity.WARNING
        )

    @property
    def failures(self) -> tuple[GateResult, ...]:
        """Failed blocking gates."""
        return tuple(
            r
            for r in self.results
            if (not r.passed) and r.severity is GateSeverity.BLOCKING
        )

    def render(self) -> str:
        """Human-readable report. Designed for CI log readability."""
        lines: list[str] = []
        verdict = "BLOCKED" if self.blocked else "OK"
        lines.append(f"Release gate: {verdict}")
        lines.append("=" * 32)
        for r in self.results:
            mark = "PASS" if r.passed else (
                "FAIL" if r.severity is GateSeverity.BLOCKING else "WARN"
            )
            lines.append(f"[{mark}] {r.name}: {r.summary}")
            if r.detail and not r.passed:
                for dl in r.detail.splitlines():
                    lines.append(f"      {dl}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Gate type
# ---------------------------------------------------------------------------


Gate = Callable[[], GateResult]


# ---------------------------------------------------------------------------
# Issue-closure metadata schema (#889 acceptance criterion 4)
# ---------------------------------------------------------------------------


_REQUIRED_CLOSURE_KEYS: tuple[str, ...] = (
    "commit",
    "branch",
    "command",
    "fresh_restart",
)
"""Keys every issue-closure comment must mention.

Acceptance criterion 4: closing comments include commit hash,
branch/ref verified, command(s) run, and whether a fresh
cockpit/session restart was included. The keys are matched
loosely (case-insensitive substring) so a free-form closure
comment qualifies as long as it names each one."""


_COMMIT_HASH_RE = re.compile(r"\b[0-9a-f]{7,40}\b")


def parse_closure_comment(text: str) -> dict[str, str]:
    """Extract structured closure metadata from a free-form comment.

    Recognized shapes::

        commit: abc123
        branch: origin/main
        command(s) run: pytest tests/test_foo.py
        fresh restart: yes / no
        cockpit restart: yes / no

    Missing keys are absent from the returned dict; callers check
    membership.
    """
    out: dict[str, str] = {}
    text = text or ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        # Tolerate prefix bullets and "* " markers.
        line = line.lstrip("-* ").strip()
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key_norm = key.strip().lower()
        value = value.strip()
        if not value:
            continue
        if "commit" in key_norm or "hash" in key_norm:
            # Confirm the value contains a plausible git hash.
            if _COMMIT_HASH_RE.search(value):
                out["commit"] = value
        elif "branch" in key_norm or "ref" in key_norm:
            out["branch"] = value
        elif "command" in key_norm:
            out["command"] = value
        elif "fresh" in key_norm or "cockpit restart" in key_norm:
            out["fresh_restart"] = value
    return out


def closure_comment_complete(text: str) -> tuple[bool, tuple[str, ...]]:
    """Return ``(complete, missing_keys)`` for a closure comment."""
    parsed = parse_closure_comment(text)
    missing = tuple(k for k in _REQUIRED_CLOSURE_KEYS if k not in parsed)
    return (len(missing) == 0, missing)


# ---------------------------------------------------------------------------
# Built-in gates
# ---------------------------------------------------------------------------


def gate_signal_routing_emitters_migrated() -> GateResult:
    """Verify the high-traffic emitters have adopted SignalEnvelope (#883).

    Inspects :data:`pollypm.signal_routing.ROUTED_EMITTERS` for
    every entry in :func:`required_high_traffic_emitters`.

    #894 — once the heartbeat, supervisor_alerts, and
    work_service modules registered themselves at import time
    AND each routed at least one representative signal site
    through ``route_signal``, the gate flipped to BLOCKING. A
    regression that drops a registration (or removes the
    representative call site) blocks v1.

    The gate cannot enforce that *every* emit site in those
    modules has migrated — that is a follow-up. What it
    enforces is that the registration is real (the emitter is
    importable and active) so the migration cannot quietly
    backslide to scaffolding.
    """
    try:
        # Importing the emitter modules is what triggers their
        # ``register_routed_emitter(...)`` call at module load.
        # Without this side-effect import, the registry is empty
        # (no other consumer in the gate path imports them).
        import pollypm.heartbeats.local  # noqa: F401
        import pollypm.supervisor_alerts  # noqa: F401
        import pollypm.work.sqlite_service  # noqa: F401
        from pollypm.signal_routing import (
            missing_routed_emitters,
            required_high_traffic_emitters,
        )
    except Exception as exc:  # noqa: BLE001
        return GateResult(
            name="signal_routing_emitters",
            passed=False,
            severity=GateSeverity.BLOCKING,
            summary="signal_routing module not importable",
            detail=str(exc),
        )

    missing = missing_routed_emitters()
    if not missing:
        return GateResult(
            name="signal_routing_emitters",
            passed=True,
            summary=(
                f"all {len(required_high_traffic_emitters())} required "
                f"emitters use SignalEnvelope"
            ),
        )
    return GateResult(
        name="signal_routing_emitters",
        passed=False,
        severity=GateSeverity.BLOCKING,
        summary=f"{len(missing)} required emitters not registered",
        detail=(
            "missing: " + ", ".join(sorted(missing)) +
            "\n(blocking — see docs/signal-routing-spec.md for "
            "the migration contract)"
        ),
    )


def gate_security_checklist() -> GateResult:
    """Run the launch security checklist (#892) and report any
    failing line as a blocking gate failure.

    #893 — earlier the security checklist existed as a free-
    standing module that the release gate did not consult, so
    the gate could report OK while the checklist had failures.
    Wiring it in here closes that loop."""
    try:
        from pollypm.security_checklist import audit_security_checklist
    except Exception as exc:  # noqa: BLE001
        return GateResult(
            name="security_checklist",
            passed=False,
            summary="security_checklist not importable",
            detail=str(exc),
        )
    failures = audit_security_checklist()
    if failures:
        return GateResult(
            name="security_checklist",
            passed=False,
            severity=GateSeverity.BLOCKING,
            summary=f"{len(failures)} security check(s) failing",
            detail="\n".join(failures),
        )
    return GateResult(
        name="security_checklist",
        passed=True,
        summary="security checklist clean",
    )


def gate_storage_legacy_writers() -> GateResult:
    """Refuse to tag while a legacy storage writer is neither
    isolated nor tracked under a migration issue (#887, #893).

    Tracked migrations (e.g., ``notification_staging`` under
    #704) surface via :func:`tracked_legacy_writers` and the
    release-gate report renders them as warnings. Untracked
    blocking writers fail the gate."""
    try:
        from pollypm.storage_contracts import (
            audit_legacy_writers,
            tracked_legacy_writers,
        )
    except Exception as exc:  # noqa: BLE001
        return GateResult(
            name="storage_legacy_writers",
            passed=False,
            summary="storage_contracts not importable",
            detail=str(exc),
        )
    blocking = audit_legacy_writers()
    if blocking:
        return GateResult(
            name="storage_legacy_writers",
            passed=False,
            severity=GateSeverity.BLOCKING,
            summary=f"{len(blocking)} untracked legacy writer(s)",
            detail="\n".join(blocking),
        )
    tracked = tracked_legacy_writers()
    if tracked:
        return GateResult(
            name="storage_legacy_writers",
            passed=False,
            severity=GateSeverity.WARNING,
            summary=f"{len(tracked)} tracked migration(s) in flight",
            detail="\n".join(tracked),
        )
    return GateResult(
        name="storage_legacy_writers",
        passed=True,
        summary="all legacy writers isolated",
    )


def gate_task_invariant_metadata_complete() -> GateResult:
    """Refuse to tag if a ``WorkStatus`` value lacks a
    :class:`StateMetadata` entry (#886).

    Used to catch the case where a new state is added to the
    ``WorkStatus`` enum but the canonical transition table is not
    updated. The audit ``all_statuses_have_metadata`` returns the
    list of missing names."""
    try:
        from pollypm.task_invariants import all_statuses_have_metadata
    except Exception as exc:  # noqa: BLE001
        return GateResult(
            name="task_invariant_metadata",
            passed=False,
            summary="task_invariants not importable",
            detail=str(exc),
        )
    missing = all_statuses_have_metadata()
    if missing:
        return GateResult(
            name="task_invariant_metadata",
            passed=False,
            severity=GateSeverity.BLOCKING,
            summary=f"{len(missing)} status(es) missing metadata",
            detail=", ".join(missing),
        )
    return GateResult(
        name="task_invariant_metadata",
        passed=True,
        summary="every WorkStatus has canonical metadata",
    )


def gate_cockpit_interaction_audit_clean() -> GateResult:
    """Verify the cockpit interaction registry has no contract
    violations (#881)."""
    try:
        from pollypm.cockpit_interaction import REGISTRY
        # Importing the canonical registered screens triggers their
        # contract registration. Add new screens here when they
        # register so the gate reflects them.
        import pollypm.cockpit_tasks  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return GateResult(
            name="cockpit_interaction_audit",
            passed=False,
            summary="cockpit_interaction registry not importable",
            detail=str(exc),
        )

    violations = REGISTRY.audit()
    if not violations:
        return GateResult(
            name="cockpit_interaction_audit",
            passed=True,
            summary=(
                f"{len(REGISTRY.screen_names())} registered screen(s); "
                f"audit clean"
            ),
        )
    return GateResult(
        name="cockpit_interaction_audit",
        passed=False,
        summary=f"{len(violations)} contract violation(s)",
        detail="\n".join(violations),
    )


def gate_main_branch_green() -> GateResult:
    """Verify ``origin/main`` is current with the local working tree.

    Acceptance criterion 1: closure-by-local-branch-state is the
    recurring failure mode. The release gate cannot launch with
    ``origin/main`` ahead of HEAD because that means a fix the
    user thinks merged hasn't actually merged yet.

    The check is conservative — it queries ``git rev-parse`` and
    skips with a warning if the repo is shallow or the remote is
    unreachable. The gate is informational in those cases.
    """
    try:
        head = _run_git("rev-parse", "HEAD")
        origin_main = _run_git("rev-parse", "origin/main")
    except _GitError as exc:
        return GateResult(
            name="main_branch_green",
            passed=False,
            severity=GateSeverity.WARNING,
            summary="git state not available",
            detail=str(exc),
        )

    if head == origin_main:
        return GateResult(
            name="main_branch_green",
            passed=True,
            summary=f"HEAD == origin/main ({head[:8]})",
        )
    # HEAD is downstream of origin/main is OK (user has unpushed
    # commits ahead of main). HEAD *behind* origin/main is the
    # bad case — the user would tag from stale state.
    try:
        ahead = int(_run_git("rev-list", "--count", "origin/main..HEAD"))
        behind = int(_run_git("rev-list", "--count", "HEAD..origin/main"))
    except _GitError as exc:
        return GateResult(
            name="main_branch_green",
            passed=False,
            severity=GateSeverity.WARNING,
            summary="git rev-list comparison failed",
            detail=str(exc),
        )

    if behind > 0:
        return GateResult(
            name="main_branch_green",
            passed=False,
            summary=(
                f"HEAD is {behind} commit(s) behind origin/main — "
                f"refresh before tagging"
            ),
        )
    return GateResult(
        name="main_branch_green",
        passed=True,
        summary=f"HEAD ahead of origin/main by {ahead}; not behind",
    )


# ---------------------------------------------------------------------------
# Gate runner
# ---------------------------------------------------------------------------


DEFAULT_GATES: tuple[Gate, ...] = (
    gate_main_branch_green,
    gate_cockpit_interaction_audit_clean,
    gate_signal_routing_emitters_migrated,
    gate_security_checklist,
    gate_storage_legacy_writers,
    gate_task_invariant_metadata_complete,
)
"""The standard launch-hardening gate set. Used by
``scripts/release_burnin.py`` and the GitHub release workflow.

#893 expanded this set so a passing report actually reflects the
launch-hardening invariants. Each new gate follows the same
contract: pure callable, catches its own exceptions, returns a
typed GateResult, and is paired with a unit test."""


def run_release_gate(
    gates: Iterable[Gate] | None = None,
) -> ReleaseReport:
    """Run every gate and aggregate results into a report.

    Each gate runs in isolation: an uncaught exception in one
    becomes a synthetic failing GateResult so a single broken
    gate cannot prevent the rest of the report.
    """
    chosen = tuple(gates) if gates is not None else DEFAULT_GATES
    report = ReleaseReport()
    for gate in chosen:
        try:
            result = gate()
        except Exception as exc:  # noqa: BLE001 — gate runner must not crash
            result = GateResult(
                name=getattr(gate, "__name__", "unnamed_gate"),
                passed=False,
                summary="gate raised",
                detail=f"{type(exc).__name__}: {exc}",
            )
        report.results.append(result)
    return report


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _GitError(Exception):
    """Raised when a git subprocess fails; the gate translates it
    into a warning rather than a blocking failure so a developer
    workspace without network access still produces a report."""


def _run_git(*args: str) -> str:
    """Run ``git ...`` and return stripped stdout, or raise."""
    try:
        result = subprocess.run(
            ("git", *args),
            cwd=_repo_root(),
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise _GitError(str(exc)) from exc
    except subprocess.TimeoutExpired as exc:
        raise _GitError("git timed out") from exc
    return result.stdout.strip()


def _repo_root() -> Path:
    """Return the PollyPM repo root (the parent of ``src/``)."""
    here = Path(__file__).resolve()
    return here.parent.parent.parent
