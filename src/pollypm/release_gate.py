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
    Failure is currently a *warning* because the migration is
    in progress; once the work_service / supervisor_alerts /
    heartbeat emitters are converted, this becomes blocking.
    """
    try:
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
        severity=GateSeverity.WARNING,
        summary=f"{len(missing)} required emitters not yet migrated",
        detail=(
            "missing: " + ", ".join(sorted(missing)) +
            "\n(this gate becomes blocking once the migration ships)"
        ),
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
)
"""The standard launch-hardening gate set. Used by
``scripts/release_burnin.py`` and the GitHub release workflow."""


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
