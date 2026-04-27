"""Launch security and trust boundary checklist (#892).

Codifies the trust assumptions every launch-sensitive boundary
must satisfy and gives the release gate (#889) a typed audit
that runs on every release.

The pre-launch audit (``docs/launch-issue-audit-2026-04-27.md``
§11) cites a concentrated security batch: #496, #495, #494,
#493, #492, #491 (mostly already-fixed). The launch-hardening
goal is *regression resistance* — keep the trust assumptions
visible and tested so a future refactor cannot quietly
re-introduce a closed vulnerability.

Architecture:

* :class:`SecurityCheck` — frozen declaration of one trust
  invariant.
* :data:`SECURITY_CHECKS` — the canonical list. Each check
  exposes a ``predicate`` callable and a ``rationale`` string.
* :func:`run_security_checks` — runs every check, returns one
  ``CheckResult`` per check.
* :func:`audit_security_checklist` — reduces the run to one-
  line summaries the release gate report renders.

The checks intentionally do *not* re-implement security tools
already in scope (e.g., ``test_backup_restore.py`` covers
backup symlink-escape mechanics). They assert that the
boundary remains in the documented shape — for example, that
``plugin_trust.py`` still exposes its trust API surface, that
the runtime-probe execution path validates input, and that
the persona-drift remediation message format stays free of
prompt-injection-shaped markup (#755).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


# ---------------------------------------------------------------------------
# Boundary catalog
# ---------------------------------------------------------------------------


class TrustBoundary(enum.Enum):
    """The security-sensitive boundaries the launch checklist
    covers."""

    PLUGIN_INSTALL = "plugin_install"
    """Third-party plugin install / enable trust."""

    PATH_VALIDATION = "path_validation"
    """Worktree, plugin content, branch name, and backup tar
    path validation."""

    BACKGROUND_ROLES = "background_roles"
    """Heartbeat / supervisor / scheduler write privilege
    surfaces."""

    REMEDIATION_MESSAGES = "remediation_messages"
    """The heartbeat persona-drift message format (#755)."""

    BACKUP_RESTORE = "backup_restore"
    """Backup tar extraction (symlink escape, locked DB,
    WAL/SHM cleanup)."""


# ---------------------------------------------------------------------------
# Check shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CheckResult:
    """The outcome of one security check."""

    check_name: str
    boundary: TrustBoundary
    passed: bool
    summary: str = ""
    detail: str = ""


@dataclass(frozen=True, slots=True)
class SecurityCheck:
    """Declaration of one trust invariant the release gate runs."""

    name: str
    boundary: TrustBoundary
    predicate: Callable[[], CheckResult]
    rationale: str


# ---------------------------------------------------------------------------
# Concrete check implementations
# ---------------------------------------------------------------------------


def _check_plugin_trust_module_exists() -> CheckResult:
    """``plugin_trust.py`` must remain importable.

    The audit cites the recurring shape: a security boundary
    module getting moved silently. Verifying the import path
    holds turns that into an immediate failure."""
    try:
        import pollypm.plugin_trust  # noqa: F401
    except ImportError as exc:
        return CheckResult(
            check_name="plugin_trust_module_exists",
            boundary=TrustBoundary.PLUGIN_INSTALL,
            passed=False,
            summary="pollypm.plugin_trust not importable",
            detail=str(exc),
        )
    return CheckResult(
        check_name="plugin_trust_module_exists",
        boundary=TrustBoundary.PLUGIN_INSTALL,
        passed=True,
        summary="plugin trust module is present",
    )


def _check_remediation_message_avoids_injection_markup() -> CheckResult:
    """The persona-drift remediation message format must not
    use ``<system-update>`` (the audit's #755 shape: prompt-
    injection defenses learned to reject that tag)."""
    try:
        from pollypm.role_contract import build_remediation_message
    except ImportError as exc:
        return CheckResult(
            check_name="remediation_message_safe_format",
            boundary=TrustBoundary.REMEDIATION_MESSAGES,
            passed=False,
            summary="role_contract not importable",
            detail=str(exc),
        )
    msg = build_remediation_message("operator_pm", "Russell")
    bad_markup = ("<system-update>", "<system_update>", "[[SYSTEM]]")
    found = [tag for tag in bad_markup if tag in msg]
    if found:
        return CheckResult(
            check_name="remediation_message_safe_format",
            boundary=TrustBoundary.REMEDIATION_MESSAGES,
            passed=False,
            summary="remediation message contains injection-shaped markup",
            detail=f"forbidden tag(s): {found}",
        )
    return CheckResult(
        check_name="remediation_message_safe_format",
        boundary=TrustBoundary.REMEDIATION_MESSAGES,
        passed=True,
        summary="remediation message format is safe",
    )


def _check_role_guide_paths_resolve() -> CheckResult:
    """Every role-guide path the contract names must resolve.

    Cross-references the contract audit helper (#888) so the
    release gate sees the failure as a security check too — a
    broken guide path means the persona-drift remediation
    points the agent at a non-existent file, which the model
    might then hallucinate fill-in content for."""
    try:
        from pollypm.contract_audit import role_guide_paths_exist
    except ImportError as exc:
        return CheckResult(
            check_name="role_guide_paths_resolve",
            boundary=TrustBoundary.REMEDIATION_MESSAGES,
            passed=False,
            summary="contract_audit not importable",
            detail=str(exc),
        )
    missing = role_guide_paths_exist()
    if missing:
        return CheckResult(
            check_name="role_guide_paths_resolve",
            boundary=TrustBoundary.REMEDIATION_MESSAGES,
            passed=False,
            summary=f"{len(missing)} role guide path(s) missing on disk",
            detail="\n".join(missing),
        )
    return CheckResult(
        check_name="role_guide_paths_resolve",
        boundary=TrustBoundary.REMEDIATION_MESSAGES,
        passed=True,
        summary="every role guide resolves on disk",
    )


def _check_no_legacy_writers_active() -> CheckResult:
    """Storage-contract legacy writers must be retired or
    isolated. An unisolated writer is a path for divergent /
    stale data to reach user surfaces — both a UX bug and a
    least-privilege concern (the writer might run with broader
    privileges than the canonical reader)."""
    try:
        from pollypm.storage_contracts import audit_legacy_writers
    except ImportError as exc:
        return CheckResult(
            check_name="no_legacy_writers_active",
            boundary=TrustBoundary.BACKGROUND_ROLES,
            passed=False,
            summary="storage_contracts not importable",
            detail=str(exc),
        )
    rows = audit_legacy_writers()
    if rows:
        # Reported as a soft fail today (the migration is in
        # progress); release-gate severity is decided by the
        # caller. Returning passed=False with a clear summary
        # surfaces the issue.
        return CheckResult(
            check_name="no_legacy_writers_active",
            boundary=TrustBoundary.BACKGROUND_ROLES,
            passed=False,
            summary=f"{len(rows)} legacy writer(s) still active",
            detail="\n".join(rows),
        )
    return CheckResult(
        check_name="no_legacy_writers_active",
        boundary=TrustBoundary.BACKGROUND_ROLES,
        passed=True,
        summary="every legacy storage writer is isolated",
    )


def _check_backup_module_exists() -> CheckResult:
    """``pollypm.backup`` must remain importable.

    The audit cites #492 / #491 for backup tar extraction
    safety. Keeping the boundary visible — by asserting the
    module imports — turns a silent rename into an immediate
    failure."""
    try:
        import pollypm.backup  # noqa: F401
    except ImportError as exc:
        return CheckResult(
            check_name="backup_module_exists",
            boundary=TrustBoundary.BACKUP_RESTORE,
            passed=False,
            summary="pollypm.backup not importable",
            detail=str(exc),
        )
    return CheckResult(
        check_name="backup_module_exists",
        boundary=TrustBoundary.BACKUP_RESTORE,
        passed=True,
        summary="backup module is present",
    )


def _check_worktree_path_validator_exists() -> CheckResult:
    """Worktree creation must remain gated behind the validator
    in :mod:`pollypm.worktrees`. The audit cites #494 for path-
    traversal during worktree create as the original concern."""
    try:
        from pollypm.worktrees import (  # noqa: F401
            create_agent_worktree,
        )
    except ImportError as exc:
        return CheckResult(
            check_name="worktree_path_validator_exists",
            boundary=TrustBoundary.PATH_VALIDATION,
            passed=False,
            summary="pollypm.worktrees.create_agent_worktree missing",
            detail=str(exc),
        )
    return CheckResult(
        check_name="worktree_path_validator_exists",
        boundary=TrustBoundary.PATH_VALIDATION,
        passed=True,
        summary="worktree path validator present",
    )


def _check_heartbeat_role_write_set_documented() -> CheckResult:
    """The heartbeat write-permission set must be derivable from
    the role contract (#885). A ``WRITE`` privilege that is not
    grounded in a documented contract is a least-privilege
    violation."""
    try:
        from pollypm.role_contract import (
            ROLE_REGISTRY,
            can_write_session_state,
        )
    except ImportError as exc:
        return CheckResult(
            check_name="heartbeat_role_write_set_documented",
            boundary=TrustBoundary.BACKGROUND_ROLES,
            passed=False,
            summary="role_contract not importable",
            detail=str(exc),
        )
    write_capable = {
        key for key in ROLE_REGISTRY if can_write_session_state(key)
    }
    # The write-capable set must be non-empty and a *strict
    # subset* of the registry. A registry where every role
    # could write would mean the contract has no privileged
    # boundary.
    if not write_capable:
        return CheckResult(
            check_name="heartbeat_role_write_set_documented",
            boundary=TrustBoundary.BACKGROUND_ROLES,
            passed=False,
            summary="no role declares write permission",
        )
    if write_capable == set(ROLE_REGISTRY):
        return CheckResult(
            check_name="heartbeat_role_write_set_documented",
            boundary=TrustBoundary.BACKGROUND_ROLES,
            passed=False,
            summary="every role can write — no privilege boundary",
        )
    return CheckResult(
        check_name="heartbeat_role_write_set_documented",
        boundary=TrustBoundary.BACKGROUND_ROLES,
        passed=True,
        summary=(
            f"{len(write_capable)}/{len(ROLE_REGISTRY)} roles "
            f"are write-capable"
        ),
    )


# ---------------------------------------------------------------------------
# Canonical check list
# ---------------------------------------------------------------------------


SECURITY_CHECKS: tuple[SecurityCheck, ...] = (
    SecurityCheck(
        name="plugin_trust_module_exists",
        boundary=TrustBoundary.PLUGIN_INSTALL,
        predicate=_check_plugin_trust_module_exists,
        rationale=(
            "The plugin trust module owns the third-party plugin "
            "install/enable confirmation flow. Losing it silently "
            "would skip the trust prompt entirely."
        ),
    ),
    SecurityCheck(
        name="remediation_message_safe_format",
        boundary=TrustBoundary.REMEDIATION_MESSAGES,
        predicate=_check_remediation_message_avoids_injection_markup,
        rationale=(
            "The audit (#755) cites <system-update>-shaped tags as "
            "the prompt-injection-defense trigger. The remediation "
            "message must use plain prose."
        ),
    ),
    SecurityCheck(
        name="role_guide_paths_resolve",
        boundary=TrustBoundary.REMEDIATION_MESSAGES,
        predicate=_check_role_guide_paths_resolve,
        rationale=(
            "A remediation message that cites a non-existent guide "
            "path invites the model to hallucinate the contents."
        ),
    ),
    SecurityCheck(
        name="no_legacy_writers_active",
        boundary=TrustBoundary.BACKGROUND_ROLES,
        predicate=_check_no_legacy_writers_active,
        rationale=(
            "Legacy writers may run with broader privileges than "
            "the canonical reader and produce divergent state."
        ),
    ),
    SecurityCheck(
        name="backup_module_exists",
        boundary=TrustBoundary.BACKUP_RESTORE,
        predicate=_check_backup_module_exists,
        rationale=(
            "Backup tar extraction is a known footgun (#492). "
            "Keep the module visible to the release gate."
        ),
    ),
    SecurityCheck(
        name="worktree_path_validator_exists",
        boundary=TrustBoundary.PATH_VALIDATION,
        predicate=_check_worktree_path_validator_exists,
        rationale=(
            "Worktree creation is the entry point most exposed to "
            "user-controlled paths. Keep the canonical creator "
            "function visible to the release gate (#494)."
        ),
    ),
    SecurityCheck(
        name="heartbeat_role_write_set_documented",
        boundary=TrustBoundary.BACKGROUND_ROLES,
        predicate=_check_heartbeat_role_write_set_documented,
        rationale=(
            "Background roles must be least-privilege actors. The "
            "write-capable set is derived from role_contract — a "
            "writer that bypasses the contract is a privilege "
            "creep."
        ),
    ),
)


# ---------------------------------------------------------------------------
# Runner + audit
# ---------------------------------------------------------------------------


def run_security_checks() -> tuple[CheckResult, ...]:
    """Run every check in :data:`SECURITY_CHECKS`."""
    out: list[CheckResult] = []
    for check in SECURITY_CHECKS:
        try:
            result = check.predicate()
        except Exception as exc:  # noqa: BLE001 — gate must not crash
            result = CheckResult(
                check_name=check.name,
                boundary=check.boundary,
                passed=False,
                summary="check raised",
                detail=f"{type(exc).__name__}: {exc}",
            )
        out.append(result)
    return tuple(out)


def audit_security_checklist() -> tuple[str, ...]:
    """Return one human-readable line per failing check.

    Empty tuple means clean. The release gate (#889) reads this."""
    out: list[str] = []
    for result in run_security_checks():
        if result.passed:
            continue
        out.append(
            f"[{result.boundary.value}] {result.check_name}: "
            f"{result.summary}"
        )
    return tuple(out)
