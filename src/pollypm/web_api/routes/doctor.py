"""Doctor endpoints (Phase 2 — §7 of the phase-2 endpoints spec).

Implements:

- ``GET  /api/v1/doctor/checks`` — list available check names + metadata.
- ``GET  /api/v1/doctor/report`` — return the last-run report (or 404
  ``not_found`` when no run has happened yet in this process).
- ``POST /api/v1/doctor/run`` — execute all checks or a single named
  check, optionally invoking :func:`pollypm.doctor.apply_fixes` when
  ``fix=true`` is set.

Per ``~/Desktop/pollypm-phase2-endpoints-spec.md`` §7. The router is a
thin adapter over :mod:`pollypm.doctor` — it never re-implements the
check registry or runner; that lives in the canonical doctor module the
CLI and cockpit both consume.

The "last report" cache is process-local (held in module state via the
``_LAST_REPORT`` slot). The CLI persists doctor output to disk under
``~/.pollypm`` already, but the API surface is intentionally
in-memory: spec §7.1 only asks for the *last-run* report, and tying
to disk would couple the API to the CLI's file layout. Tests can reset
the cache via :func:`_reset_last_report` so cases don't leak state.
"""

from __future__ import annotations

import concurrent.futures
import logging
import threading
import time
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from pollypm.config import DEFAULT_CONFIG_PATH
from pollypm.doctor import (
    Check,
    CheckResult,
    DoctorReport,
    apply_fixes,
    run_checks,
)
from pollypm.doctor import (
    _auto_fix_supported,  # type: ignore[attr-defined]
    _registered_checks,  # type: ignore[attr-defined]
)
from pollypm.web_api.errors import APIError, conflict, invalid_request, not_found
from pollypm.web_api.routes._deps import ConfigDep

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Doctor"])


# ---------------------------------------------------------------------------
# Limits + defaults (kept module-level so tests can patch)
# ---------------------------------------------------------------------------


# Per spec §2.7: long-running sync actions get a 30s budget; doctor runs
# typically complete in < 5s but ``--fix`` may exceed that. Stay below
# the spec's 30s ceiling so the caller sees a typed 504 rather than the
# generic uvicorn timeout.
DEFAULT_RUN_TIMEOUT_SECONDS = 25.0


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class DoctorCheckInfo(BaseModel):
    """Catalog row for ``GET /doctor/checks`` (spec §7.1).

    Matches the spec's recommended shape: ``{name, description,
    severity, has_auto_fix}``. ``description`` is best-effort — the
    underlying :class:`pollypm.doctor.Check` dataclass doesn't carry
    a docstring, so we use the category as the human label until we
    plumb richer metadata through.
    """

    name: str
    category: str
    severity: str
    description: str = ""
    has_auto_fix: bool = False


class DoctorChecksResponse(BaseModel):
    checks: list[DoctorCheckInfo]


class DoctorCheckResult(BaseModel):
    """One row of a doctor report (spec §7).

    Mirrors :class:`pollypm.doctor.CheckResult` with the non-trivial
    fields the cockpit / frontend renders. ``fix_fn`` is intentionally
    omitted — it's a callable, not a value.
    """

    name: str
    category: str
    passed: bool
    skipped: bool
    severity: str
    status: str = ""
    why: str = ""
    fix: str = ""
    fixable: bool = False
    has_auto_fix: bool = False
    data: dict[str, Any] = Field(default_factory=dict)


class DoctorReportResponse(BaseModel):
    """Full report envelope (spec §7.1)."""

    generated_at: float
    duration_seconds: float
    ok: bool
    passed: int
    errors: int
    warnings: int
    skipped: int
    checks: list[DoctorCheckResult]


class DoctorRunRequest(BaseModel):
    """Body for ``POST /doctor/run`` (spec §7.1).

    Spec says ``{check?: name, fix: bool}``. We make ``fix`` optional
    with default ``false`` so callers who want to dry-run a single
    check don't have to spell it out.
    """

    check: str | None = None
    fix: bool = False


class DoctorRunResponse(DoctorReportResponse):
    """``POST /doctor/run`` response — report + (when ``fix=true``)
    the list of fixes that were applied.
    """

    fixes_applied: list[dict[str, Any]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Last-report cache (process-local; spec §7.1)
# ---------------------------------------------------------------------------


_LAST_REPORT_LOCK = threading.Lock()
_LAST_REPORT: tuple[DoctorReport, float] | None = None


# Operation-level single-flight lock for ``POST /doctor/run`` when
# ``fix=true``. ``_LAST_REPORT_LOCK`` above only guards the cache slot;
# without this second lock, two concurrent callers can each invoke
# ``apply_fixes`` on the same filesystem/session/storage state and
# interleave the post-fix verify writes (Codex round-1 P0 on PR #2058).
# Acquired non-blocking — a second concurrent ``fix=true`` returns a
# typed 409 ``busy`` rather than queueing behind the first.
_FIX_OPERATION_LOCK = threading.Lock()


def _record_last_report(report: DoctorReport) -> float:
    """Stash ``report`` as the last-run report. Returns the timestamp."""
    ts = time.time()
    global _LAST_REPORT
    with _LAST_REPORT_LOCK:
        _LAST_REPORT = (report, ts)
    return ts


def _read_last_report() -> tuple[DoctorReport, float] | None:
    with _LAST_REPORT_LOCK:
        return _LAST_REPORT


def _reset_last_report() -> None:
    """Test hook — clears the module-level cache between cases."""
    global _LAST_REPORT
    with _LAST_REPORT_LOCK:
        _LAST_REPORT = None


# ---------------------------------------------------------------------------
# Conversion helpers
# ---------------------------------------------------------------------------


def _check_info(check: Check) -> DoctorCheckInfo:
    """Build the catalog row for one registered check.

    ``has_auto_fix`` is best-effort: we don't actually have a result
    yet, so we report ``False`` here. The per-result row in a report
    surfaces the real value once a run has happened.
    """
    return DoctorCheckInfo(
        name=check.name,
        category=check.category,
        severity=check.severity,
        description="",
        has_auto_fix=False,
    )


def _result_row(check: Check, result: CheckResult) -> DoctorCheckResult:
    """Coerce one ``(Check, CheckResult)`` tuple to the wire row.

    Drops the ``fix_fn`` callable (non-serializable) and folds the
    ``auto_fix`` plan into a boolean — clients that need the full
    plan can call ``POST /doctor/run`` with ``fix=true`` and inspect
    ``fixes_applied``.
    """
    return DoctorCheckResult(
        name=check.name,
        category=check.category,
        passed=result.passed,
        skipped=result.skipped,
        severity=result.severity,
        status=result.status,
        why=result.why,
        fix=result.fix,
        fixable=bool(result.fixable and result.fix_fn is not None),
        has_auto_fix=_auto_fix_supported(result.auto_fix),
        data=_jsonable(result.data),
    )


def _jsonable(value: Any) -> Any:
    """Best-effort coerce arbitrary check ``data`` payloads to JSON.

    Doctor checks frequently stash ``Path`` instances and other
    non-JSON-native types in ``CheckResult.data``. FastAPI's JSON
    encoder handles most of these, but the type signature on the
    wire model is ``dict[str, Any]`` so we lean on stringification
    for anything pydantic can't round-trip natively.
    """
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _build_report_response(report: DoctorReport, *, generated_at: float) -> DoctorReportResponse:
    rows = [_result_row(check, result) for check, result in report.results]
    return DoctorReportResponse(
        generated_at=generated_at,
        duration_seconds=report.duration_seconds,
        ok=report.ok,
        passed=report.passed_count,
        errors=len(report.errors),
        warnings=len(report.warnings),
        skipped=report.skipped_count,
        checks=rows,
    )


# ---------------------------------------------------------------------------
# Typed errors
# ---------------------------------------------------------------------------


def _unknown_check(name: str, available: list[str]) -> APIError:
    return not_found(
        f"Unknown doctor check: {name!r}",
        hint=(
            f"Use GET /api/v1/doctor/checks to list available names "
            f"({len(available)} registered)."
        ),
    )


def _check_timeout(name: str | None, elapsed: float) -> APIError:
    target = name or "all checks"
    return APIError(
        status_code=504,
        code="timeout",
        message=(
            f"Doctor run for {target} exceeded the "
            f"{DEFAULT_RUN_TIMEOUT_SECONDS:.0f}s budget "
            f"(ran for {elapsed:.1f}s)"
        ),
        hint=(
            "Retry the specific failing check, or re-run asynchronously "
            "once the async job runner ships (spec §2.7 / Q13)."
        ),
    )


def _fix_busy_error() -> APIError:
    """Typed 409 returned when a concurrent ``fix=true`` is in flight.

    Doctor fixes touch shared state (filesystem, tmux sessions, the
    Postgres heartbeat tables); we serialize the run+fix+verify
    sequence with :data:`_FIX_OPERATION_LOCK`. Callers that race a
    second ``fix=true`` request get an immediate 409 ``busy`` rather
    than blocking on the lock or running fixes a second time.
    """
    return conflict(
        "Doctor fix already in progress",
        hint=(
            "Wait for the in-flight POST /doctor/run (fix=true) to complete, "
            "then retry. Doctor fixes are single-flight."
        ),
    )


def _reject_non_default_config(config: Any) -> None:
    """Refuse to run doctor against a non-default config path.

    Many checks/fixes in :mod:`pollypm.doctor` load
    :data:`pollypm.config.DEFAULT_CONFIG_PATH` internally rather than
    accepting an injected :class:`PollyPMConfig`. When ``pm serve
    --config <path>`` is used, the endpoint would otherwise *report*
    on (and with ``fix=true`` *mutate*) the wrong workspace — a real
    cross-config leak (Codex round-1 P0 on PR #2058).

    Until the doctor module grows a config-accepting facade, refuse
    explicit non-default configs with a typed 400. The default config
    path (``~/.pollypm/pollypm.toml``) is the only one the checks
    themselves load, so passing through that case is safe.

    ``config.config_path`` is ``None`` for in-memory fixtures (tests),
    which we pass through — there's no on-disk config to disagree
    with the checks' internal loads.
    """
    config_path = getattr(config, "config_path", None)
    if config_path is None:
        return
    try:
        resolved = config_path.resolve()
        default_resolved = DEFAULT_CONFIG_PATH.resolve()
    except OSError:
        # Couldn't resolve (file moved between requests, transient I/O
        # error). Compare unresolved as a fallback so we still refuse
        # the obvious cross-config case rather than silently passing.
        resolved = config_path
        default_resolved = DEFAULT_CONFIG_PATH
    if resolved == default_resolved:
        return
    raise invalid_request(
        "Doctor endpoints only support the default config path",
        hint=(
            f"`pm serve --config {config_path}` is loaded, but doctor "
            f"checks load `{DEFAULT_CONFIG_PATH}` internally and would "
            "mutate the wrong workspace. Run `pm doctor` from the CLI "
            "instead, or restart `pm serve` without --config."
        ),
    )


# ---------------------------------------------------------------------------
# Core runners (broken out for ease of testing)
# ---------------------------------------------------------------------------


def _select_checks(name: str | None) -> list[Check]:
    """Resolve ``name`` to a list of :class:`Check` objects.

    ``name is None`` → every registered check (the default).
    ``name`` matches one registered check → that check only.
    Otherwise raise a typed 404.
    """
    registered = _registered_checks()
    if name is None:
        return registered
    for check in registered:
        if check.name == name:
            return [check]
    raise _unknown_check(name, [c.name for c in registered])


def _run_with_budget(
    selected: list[Check],
    *,
    name: str | None,
    timeout: float,
) -> DoctorReport:
    """Run the selected checks under a real wall-clock budget.

    ``run_checks`` itself doesn't honor a timeout — individual checks
    each have their own short subprocess timeouts (see ``_run_cmd``),
    but a pure-Python hang (network call, stuck storage probe, blocking
    fix path) would otherwise hold the request worker forever.

    To bound the HTTP request, we hand ``run_checks`` off to a
    single-shot :class:`concurrent.futures.ThreadPoolExecutor` and
    wait at most ``timeout`` seconds for the result. On overrun we
    raise a typed 504 ``timeout`` immediately — the worker thread is
    *not* cancelled (Python stdlib has no safe way to interrupt
    arbitrary blocking code) and is allowed to finish in the
    background. The HTTP caller sees the 504 within the budget; the
    leaked thread is the accepted v1 RC tradeoff (Codex round-1 P0
    on PR #2058).

    ``DoctorThreadLeak`` is intentionally not raised on leak: the
    leak is silent and best-effort cleanup happens when the thread
    finally returns.
    """
    t0 = time.monotonic()
    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix="doctor-run",
    )
    try:
        future = executor.submit(run_checks, selected)
        try:
            report = future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            elapsed = time.monotonic() - t0
            logger.warning(
                "doctor: run exceeded %.1fs budget for %s; abandoning worker thread",
                timeout,
                name or "all checks",
            )
            raise _check_timeout(name, elapsed) from None
    finally:
        # ``wait=False`` so we don't block the request on the leaked
        # worker thread when the budget already fired. If the future
        # finished cleanly the shutdown is effectively a no-op.
        executor.shutdown(wait=False)

    elapsed = time.monotonic() - t0
    if elapsed > timeout:
        # Defensive: the future returned just past the deadline. Still
        # surface 504 so the caller sees a consistent contract.
        raise _check_timeout(name, elapsed)
    return report


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/doctor/checks",
    response_model=DoctorChecksResponse,
    summary="List available doctor checks",
    operation_id="listDoctorChecks",
)
def list_doctor_checks_endpoint(config: ConfigDep) -> DoctorChecksResponse:
    """GET /api/v1/doctor/checks — return every registered check.

    The catalog is static per process — restart ``pm serve`` to pick
    up new checks from plugins. Order matches the canonical
    :func:`_registered_checks` ordering (category-grouped) so the
    cockpit can render a stable list.
    """
    # ``config`` is accepted so the dependency injection keeps the
    # auth guard active (FastAPI only resolves the dep chain when the
    # route declares one). Doctor checks themselves currently load
    # ``DEFAULT_CONFIG_PATH`` inside each probe — :func:`_reject_non_default_config`
    # refuses ``--config <other>`` so the catalog matches the workspace
    # the checks would actually probe (Codex round-1 P0 on PR #2058).
    _reject_non_default_config(config)
    checks = _registered_checks()
    return DoctorChecksResponse(checks=[_check_info(c) for c in checks])


@router.get(
    "/doctor/report",
    response_model=DoctorReportResponse,
    summary="Return the last-run doctor report",
    operation_id="getDoctorReport",
)
def get_doctor_report_endpoint(config: ConfigDep) -> DoctorReportResponse:
    """GET /api/v1/doctor/report — the most-recent in-process report.

    Returns 404 ``not_found`` when no doctor run has happened yet in
    this process — callers should ``POST /doctor/run`` first. This is
    intentionally in-memory; the on-disk cache the CLI keeps is not
    plumbed through (spec §7.1 deliberately scopes "last report" to
    the API's own surface).
    """
    _reject_non_default_config(config)
    cached = _read_last_report()
    if cached is None:
        raise not_found(
            "No doctor report cached yet",
            hint="POST /api/v1/doctor/run to generate one.",
        )
    report, generated_at = cached
    return _build_report_response(report, generated_at=generated_at)


@router.post(
    "/doctor/run",
    response_model=DoctorRunResponse,
    summary="Run doctor checks (optionally with --fix)",
    operation_id="runDoctor",
)
def run_doctor_endpoint(
    config: ConfigDep,
    body: DoctorRunRequest | None = None,
    timeout_seconds: Annotated[
        float,
        Query(
            ge=1.0,
            le=120.0,
            description=(
                "Wall-clock budget for the run. Defaults to "
                f"{DEFAULT_RUN_TIMEOUT_SECONDS:.0f}s; max 120s."
            ),
        ),
    ] = DEFAULT_RUN_TIMEOUT_SECONDS,
) -> DoctorRunResponse:
    """POST /api/v1/doctor/run — execute checks and (optionally) fixes.

    Body shape per spec §7.1:

        {"check": "<name>" | null, "fix": true | false}

    - ``check`` omitted / null → run every registered check.
    - ``check`` set → run only that check; unknown → 404.
    - ``fix=true`` → after the run, invoke ``apply_fixes`` on the
      report and re-run *just the fixed checks* so the response shows
      post-fix state. The list of attempted fixes lands in
      ``fixes_applied``.

    The whole sequence (run + optional fix + post-fix verify) is
    capped at ``timeout_seconds``; overrun → 504 ``timeout``.

    When ``fix=true`` the run+fix+verify sequence is serialized via
    :data:`_FIX_OPERATION_LOCK`; concurrent ``fix=true`` callers get
    a typed 409 ``busy`` (Codex round-1 P0).
    """
    _reject_non_default_config(config)
    payload = body or DoctorRunRequest()

    selected = _select_checks(payload.check)

    # Single-flight gate for ``fix=true``. Acquire non-blocking so a
    # second concurrent caller returns 409 immediately rather than
    # queueing behind the in-flight fix. ``fix=false`` is read-only
    # for our purposes (each check has its own internal locks) and
    # is allowed to overlap.
    fix_lock_held = False
    if payload.fix:
        fix_lock_held = _FIX_OPERATION_LOCK.acquire(blocking=False)
        if not fix_lock_held:
            raise _fix_busy_error()

    try:
        t0 = time.monotonic()
        report = _run_with_budget(
            selected, name=payload.check, timeout=timeout_seconds,
        )

        fixes_applied: list[dict[str, Any]] = []
        if payload.fix:
            # ``apply_fixes`` skips passing/skipped checks itself — no need
            # to filter on the caller side. Returns a list of
            # ``(name, success, message)`` tuples; we coerce to JSON for
            # the wire response.
            try:
                raw = apply_fixes(report)
            except Exception as exc:  # noqa: BLE001 — bubble as 500 via outer handler
                logger.exception("doctor: apply_fixes raised: %s", exc)
                raise
            fixes_applied = [
                {"name": name, "ok": ok, "message": message}
                for (name, ok, message) in raw
            ]

            # Re-run just the checks we tried to fix so the response
            # reflects the post-fix state (issue #1063 style verification:
            # don't trust the handler's self-report).
            fixed_names = {entry["name"] for entry in fixes_applied}
            if fixed_names:
                rerun_checks = [c for c in selected if c.name in fixed_names]
                elapsed_so_far = time.monotonic() - t0
                remaining = timeout_seconds - elapsed_so_far
                if remaining <= 0:
                    raise _check_timeout(payload.check, elapsed_so_far)
                verify_report = _run_with_budget(
                    rerun_checks,
                    name=payload.check,
                    timeout=max(remaining, 1.0),
                )
                # Splice verify-report rows back into the original report
                # so the response always reflects the latest state. The
                # original report is a dataclass — build a new merged list
                # of ``(check, result)`` tuples and mutate in place.
                verify_index = {
                    check.name: result
                    for check, result in verify_report.results
                }
                merged: list[tuple[Check, CheckResult]] = []
                for check, result in report.results:
                    replacement = verify_index.get(check.name)
                    merged.append((check, replacement if replacement else result))
                report.results = merged

        generated_at = _record_last_report(report)
    finally:
        if fix_lock_held:
            _FIX_OPERATION_LOCK.release()
    base = _build_report_response(report, generated_at=generated_at)
    return DoctorRunResponse(
        generated_at=base.generated_at,
        duration_seconds=base.duration_seconds,
        ok=base.ok,
        passed=base.passed,
        errors=base.errors,
        warnings=base.warnings,
        skipped=base.skipped,
        checks=base.checks,
        fixes_applied=fixes_applied,
    )


# Re-export for tests + the app factory.
__all__ = [
    "DEFAULT_RUN_TIMEOUT_SECONDS",
    "DoctorCheckInfo",
    "DoctorCheckResult",
    "DoctorChecksResponse",
    "DoctorReportResponse",
    "DoctorRunRequest",
    "DoctorRunResponse",
    "_FIX_OPERATION_LOCK",
    "_reset_last_report",
    "get_doctor_report_endpoint",
    "list_doctor_checks_endpoint",
    "router",
    "run_doctor_endpoint",
]


# Silence unused-import lints — these names are part of the module's
# public re-export surface so tests can patch them.
_ = invalid_request
