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

import ast
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

    Per-site enforcement (every ``raise_alert`` in a policed
    module routes through SignalEnvelope) lives in
    :func:`gate_signal_routing_call_sites_migrated` (#910). This
    gate stays focused on the registration check: the emitter is
    importable, active, and registered.
    """
    try:
        # Importing the emitter modules is what triggers their
        # ``register_routed_emitter(...)`` call at module load.
        # Without this side-effect import, the registry is empty
        # (no other consumer in the gate path imports them).
        # Post-sqlite-ripout (refs #1971) the SQLiteWorkService emitter
        # registration is gone; the pg service registers its own
        # routed emitters on import.
        import pollypm.heartbeats.local  # noqa: F401
        import pollypm.supervisor_alerts  # noqa: F401
        import pollypm.work.pg_service  # noqa: F401
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
                f"emitters registered (per-site coverage enforced by "
                f"signal_routing_call_sites)"
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


_LEGACY_EMIT_API_NAMES: frozenset[str] = frozenset(
    {
        "raise_alert",
        "record_event",
    }
)
"""Legacy emit-API method names that must no longer be called
directly from policed modules. The gate flags every ``foo.<name>(...)``
call expression where ``<name>`` is in this set.

Scope: every public emit boundary the audit flagged in #910 must
route through SignalEnvelope before persisting. That's
``raise_alert`` (alerts — the original 9 sites in
heartbeats/local.py) AND ``record_event`` (activity-feed events —
the follow-up #910 fix). ``upsert_alert`` is intentionally NOT in
the set: the routing funnels persist via the api shim
(``api.raise_alert``) which itself calls ``upsert_alert``;
flagging that name would create a false positive on every
funnel."""


_LEGACY_EMIT_ALLOWED_FILES: frozenset[str] = frozenset(
    {
        # The single funnel for routed alerts. Calls
        # ``api.raise_alert`` AFTER constructing a SignalEnvelope
        # and routing it — this is the migration target, not a
        # legacy holdout.
        "src/pollypm/heartbeats/local.py:_emit_routed_alert",
        # The single funnel for routed activity-feed events
        # (#910 follow-up). Calls ``api.record_event`` AFTER
        # constructing + routing the envelope.
        "src/pollypm/heartbeats/local.py:_emit_routed_event",
        # The plugin API method itself — the storage shim every
        # routed emitter calls into.
        "src/pollypm/heartbeats/api.py:raise_alert",
        "src/pollypm/heartbeats/api.py:record_event",
    }
)
"""Module-qualified function names allowed to call a legacy emit
API. These are the *funnels* the migration sends every signal
through — they own the SignalEnvelope construction + routing
before the legacy persistence call."""


_POLICED_DIRECTORIES: tuple[str, ...] = (
    "src/pollypm/heartbeats",
)
"""Directories whose Python modules are subject to the
'no legacy emit calls outside the routing funnel' rule.

#910 — heartbeats was the first subsystem to fully migrate. New
directories are added here as their migration completes."""


@dataclass(frozen=True, slots=True)
class _LegacyEmitFinding:
    """A single AST-detected violation of the routing-funnel rule."""

    file: str
    """Repo-relative file path."""

    line: int
    """1-indexed source line of the offending call."""

    method: str
    """Legacy emit method name (e.g. ``raise_alert``)."""

    enclosing: str
    """Name of the enclosing function/method, or ``"<module>"``."""

    def render(self) -> str:
        return f"{self.file}:{self.line} {self.enclosing}() calls .{self.method}(...)"


def _scan_module_for_legacy_emit_calls(
    path: Path, *, repo_root: Path,
) -> list[_LegacyEmitFinding]:
    """Walk ``path``'s AST and collect legacy emit call findings.

    A finding is any ``<expr>.<name>(...)`` call where ``<name>``
    is in :data:`_LEGACY_EMIT_API_NAMES` AND the enclosing
    function is not in :data:`_LEGACY_EMIT_ALLOWED_FILES`. Sites
    inside the routing funnel itself are exempt — that's where
    the canonical legacy persistence call lives.
    """
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    rel = str(path.relative_to(repo_root)).replace("\\", "/")

    # Build a parent map so we can answer "what function contains
    # this Call node?" without traversing twice.
    parent: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node

    def _enclosing_function_name(node: ast.AST) -> str:
        cur: ast.AST | None = parent.get(node)
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return cur.name
            cur = parent.get(cur)
        return "<module>"

    findings: list[_LegacyEmitFinding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr not in _LEGACY_EMIT_API_NAMES:
            continue
        enclosing = _enclosing_function_name(node)
        qualifier = f"{rel}:{enclosing}"
        if qualifier in _LEGACY_EMIT_ALLOWED_FILES:
            continue
        findings.append(
            _LegacyEmitFinding(
                file=rel,
                line=node.lineno,
                method=func.attr,
                enclosing=enclosing,
            )
        )
    return findings


def audit_legacy_emit_call_sites() -> tuple[_LegacyEmitFinding, ...]:
    """Return every ``raise_alert``-style call under the policed
    directories that is not inside the routing funnel.

    Public so tests can assert on the empty-tree contract and so
    a CI step can re-use the same audit logic without invoking
    the full release gate.
    """
    repo_root = _repo_root()
    findings: list[_LegacyEmitFinding] = []
    for relative in _POLICED_DIRECTORIES:
        directory = repo_root / relative
        if not directory.exists():
            continue
        for path in sorted(directory.rglob("*.py")):
            findings.extend(
                _scan_module_for_legacy_emit_calls(path, repo_root=repo_root)
            )
    return tuple(findings)


def gate_signal_routing_call_sites_migrated() -> GateResult:
    """#910 — refuse to tag while a policed module still calls a
    legacy emit API outside the routing funnel.

    Closes the false-positive that #894 left open: the previous
    gate (:func:`gate_signal_routing_emitters_migrated`) only
    asserts that the *module* registered itself and routed at
    least one representative signal. This gate AST-scans every
    file under :data:`_POLICED_DIRECTORIES` and fails when it
    finds a ``raise_alert``-style call outside the documented
    funnel functions. The funnel is the only place SignalEnvelope
    construction + ``route_signal`` happen before the legacy
    persistence write — anywhere else is a regression.
    """
    findings = audit_legacy_emit_call_sites()
    if not findings:
        return GateResult(
            name="signal_routing_call_sites",
            passed=True,
            summary=(
                "no legacy emit call sites under "
                f"{', '.join(_POLICED_DIRECTORIES)} (every site routes "
                "through SignalEnvelope)"
            ),
        )
    return GateResult(
        name="signal_routing_call_sites",
        passed=False,
        severity=GateSeverity.BLOCKING,
        summary=(
            f"{len(findings)} legacy emit call site(s) outside the "
            "routing funnel"
        ),
        detail="\n".join(f.render() for f in findings),
    )


def gate_cockpit_smoke_harness() -> GateResult:
    """Verify the rendered cockpit smoke matrix actually runs (#882, #898, #911).

    The audit (#898) requires the smoke matrix to be a blocking
    launch check. The gate has two responsibilities:

    1. Shape — the harness MODULE is importable, the size matrix
       matches the audit's published list (80x30, 100x40, 169x50,
       200x50, 210x65), and the universal-assertion API is intact.
    2. Render — at least one rendered scenario actually executes.
       #911 — the prior version of this gate checked only shape,
       so it could report PASS while every rendered smoke scenario
       was failing. The fix runs :func:`run_smoke_matrix` and
       fails BLOCKING when any scenario raises.

    The default :data:`pollypm.cockpit_smoke.SMOKE_SCENARIOS`
    registry is intentionally tiny so the gate stays fast. The
    deep per-screen matrix (seeded data, multiple sizes) lives in
    ``tests/test_cockpit_smoke_render.py`` and runs in CI; the gate
    proves the *path* executes, the test suite proves the breadth.
    """
    try:
        from pollypm.cockpit_smoke import (
            SMOKE_SCENARIOS,
            SMOKE_TERMINAL_SIZES,
            SmokeHarness,
            run_smoke_matrix,
        )
    except Exception as exc:  # noqa: BLE001
        return GateResult(
            name="cockpit_smoke_harness",
            passed=False,
            severity=GateSeverity.BLOCKING,
            summary="cockpit_smoke not importable",
            detail=str(exc),
        )

    expected_sizes = {(80, 30), (100, 40), (169, 50), (200, 50), (210, 65)}
    if set(SMOKE_TERMINAL_SIZES) != expected_sizes:
        return GateResult(
            name="cockpit_smoke_harness",
            passed=False,
            severity=GateSeverity.BLOCKING,
            summary="smoke size matrix drifted from the audit's published set",
            detail=(
                f"expected {sorted(expected_sizes)}, "
                f"got {sorted(SMOKE_TERMINAL_SIZES)}"
            ),
        )
    required_methods = (
        "snapshot",
        "assert_no_traceback",
        "assert_no_bootstrap_prompt",
        "assert_no_orphan_box_chars",
        "assert_no_letter_by_letter_wrap",
        "assert_text_visible",
        "assert_text_not_visible",
        "assert_minimum_widget_count",
        "assert_counts_agree",
    )
    missing = [m for m in required_methods if not hasattr(SmokeHarness, m)]
    if missing:
        return GateResult(
            name="cockpit_smoke_harness",
            passed=False,
            severity=GateSeverity.BLOCKING,
            summary=f"smoke harness missing API: {', '.join(missing)}",
        )

    # Shape OK — now actually render. #911: the gate must invoke
    # the smoke runner, not merely check that the runner exists.
    if not SMOKE_SCENARIOS:
        return GateResult(
            name="cockpit_smoke_harness",
            passed=False,
            severity=GateSeverity.BLOCKING,
            summary="no smoke scenarios registered — rendered coverage is zero",
        )
    try:
        failures = run_smoke_matrix()
    except Exception as exc:  # noqa: BLE001 — runner crashes block the gate
        return GateResult(
            name="cockpit_smoke_harness",
            passed=False,
            severity=GateSeverity.BLOCKING,
            summary="smoke runner raised before completing the matrix",
            detail=f"{type(exc).__name__}: {exc}",
        )
    if failures:
        return GateResult(
            name="cockpit_smoke_harness",
            passed=False,
            severity=GateSeverity.BLOCKING,
            summary=(
                f"{len(failures)} rendered smoke scenario(s) failing"
            ),
            detail="\n".join(f.render() for f in failures),
        )

    cells = sum(len(s.sizes) for s in SMOKE_SCENARIOS)
    return GateResult(
        name="cockpit_smoke_harness",
        passed=True,
        summary=(
            f"smoke matrix has {len(SMOKE_TERMINAL_SIZES)} sizes; "
            f"{len(required_methods)} canonical assertions; "
            f"{cells} rendered scenario cell(s) passed"
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
    gate_cockpit_smoke_harness,
    gate_signal_routing_emitters_migrated,
    gate_signal_routing_call_sites_migrated,
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
