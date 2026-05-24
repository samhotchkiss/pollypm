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

The "last report" cache is per-app (held on ``app.state`` via the
``doctor_last_report`` slot, owned by the FastAPI lifespan). The CLI
persists doctor output to disk under ``~/.pollypm`` already, but the
API surface is intentionally in-memory: spec §7.1 only asks for the
*last-run* report, and tying to disk would couple the API to the
CLI's file layout. Each ``create_app`` call gets its own slot, so
test instances don't cross-contaminate (Codex round-5 on PR #2058).
"""

from __future__ import annotations

import concurrent.futures
import logging
import threading
import time
from typing import Annotated, Any, Callable, TypeVar

from fastapi import APIRouter, Query, Request
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
from pollypm.web_api.errors import (
    APIError,
    invalid_request,
    not_found,
    service_unavailable,
)
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
DOCTOR_FIX_BUSY_RETRY_AFTER_SECONDS = 5


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
# App-scoped state accessors (spec §7.1)
#
# The doctor executor, single-flight fix lock, and last-report cache
# all live on ``app.state``, owned by the FastAPI lifespan in
# :mod:`pollypm.web_api.app`. Previously these were module-level
# globals; that left timed-out worker threads alive past ``pm serve``
# shutdown with no FastAPI hook to call ``shutdown(cancel_futures=True)``
# (Codex round-5 on PR #2058). Tying them to the app makes the
# executor lifecycle-managed, makes the locks per-app (so test
# instances don't cross-contaminate), and lets the lifespan emit
# observability for in-flight workers at teardown.
# ---------------------------------------------------------------------------


def _get_executor(request: Request) -> concurrent.futures.ThreadPoolExecutor:
    """Resolve the doctor executor from app-level state.

    Raises ``service_unavailable`` if the lifespan didn't fire — only
    possible when ``TestClient`` is used outside a ``with`` block.
    """
    executor = getattr(request.app.state, "doctor_executor", None)
    if executor is None:  # pragma: no cover — only hit if lifespan didn't run
        raise service_unavailable(
            "doctor executor is not initialized",
            hint=(
                "The FastAPI lifespan didn't run; ensure ``create_app`` "
                "is invoked through the standard ASGI server (uvicorn) "
                "or a TestClient context manager (``with TestClient(app)``)."
            ),
        )
    return executor


def _get_fix_lock(request: Request) -> threading.Lock:
    """Resolve the single-flight ``fix=true`` lock from app-level state.

    Acquired non-blocking in the run endpoint; released by the worker's
    done-callback when the future truly terminates (Codex round-4 on
    PR #2058 — not when the HTTP request returns 504).
    """
    lock = getattr(request.app.state, "doctor_fix_lock", None)
    if lock is None:  # pragma: no cover — only hit if lifespan didn't run
        raise service_unavailable(
            "doctor fix lock is not initialized",
            hint=(
                "The FastAPI lifespan didn't run; ensure ``create_app`` "
                "is invoked through the standard ASGI server."
            ),
        )
    return lock


def _release_fix_lock_factory(
    lock: threading.Lock,
) -> Callable[[concurrent.futures.Future[Any]], None]:
    """Build a done-callback that releases ``lock`` when the future terminates.

    The done-callback also logs the late completion (success or
    exception) so a timed-out fix that finishes minutes later is
    observable in the server log rather than silently disappearing
    (Codex round-5 on PR #2058: timed-out workers were unowned).

    The release MUST NOT raise — if it did, the future's result would
    be silently corrupted by ``concurrent.futures``; we defensively
    swallow + log instead.
    """

    def _callback(future: concurrent.futures.Future[Any]) -> None:
        # Observability first: log whether the worker eventually
        # succeeded, raised, or was cancelled. Cheap and gives the
        # operator a paper trail for the timed-out → late-completion
        # path that round-5 flagged.
        try:
            if future.cancelled():
                logger.info("doctor: fix worker cancelled before completion")
            elif future.exception() is not None:
                logger.warning(
                    "doctor: fix worker terminated with exception: %s",
                    future.exception(),
                )
            else:
                logger.info("doctor: fix worker completed (late or in-budget)")
        except Exception:  # noqa: BLE001 — never raise from a done-callback
            logger.exception("doctor: error inspecting fix-future result")

        try:
            lock.release()
        except RuntimeError:
            # Lock wasn't held (e.g. test harness already released it).
            # Log but don't propagate — the done-callback contract
            # forbids raising.
            logger.warning(
                "doctor: fix lock already released when fix worker terminated"
            )

    return _callback


def _log_check_worker_outcome(
    future: concurrent.futures.Future[Any],
) -> None:
    """Done-callback for ``fix=false`` futures — log late completion.

    Codex round-5 (PR #2058): a timed-out ``fix=false`` worker is
    abandoned by ``_await_with_budget`` and finishes silently. The
    operator has no way to know whether the underlying check ever
    returned. This callback logs the eventual outcome so late
    completions / exceptions surface in the server log. It does NOT
    interact with any lock — the read path doesn't take one.
    """
    try:
        if future.cancelled():
            logger.info("doctor: check worker cancelled before completion")
        elif future.exception() is not None:
            logger.warning(
                "doctor: check worker terminated with exception: %s",
                future.exception(),
            )
        else:
            logger.info("doctor: check worker completed (late or in-budget)")
    except Exception:  # noqa: BLE001 — never raise from a done-callback
        logger.exception("doctor: error inspecting check-future result")


def _record_last_report(request: Request, report: DoctorReport) -> float:
    """Stash ``report`` as the last-run report on app state. Returns the timestamp."""
    ts = time.time()
    lock = getattr(request.app.state, "doctor_last_report_lock", None)
    if lock is None:  # pragma: no cover — only hit if lifespan didn't run
        raise service_unavailable(
            "doctor last-report cache is not initialized",
        )
    with lock:
        request.app.state.doctor_last_report = (report, ts)
    return ts


def _read_last_report(
    request: Request,
) -> tuple[DoctorReport, float] | None:
    lock = getattr(request.app.state, "doctor_last_report_lock", None)
    if lock is None:  # pragma: no cover — only hit if lifespan didn't run
        return None
    with lock:
        return getattr(request.app.state, "doctor_last_report", None)


def _reset_last_report(request: Request | None = None) -> None:
    """Test hook — clears the per-app last-report cache between cases.

    Accepts an optional ``request`` for symmetry with the read helpers.
    When called without one (legacy test entrypoint) this is a no-op:
    each test fixture now builds a fresh app whose lifespan starts
    with ``doctor_last_report = None``.
    """
    if request is None:
        return
    lock = getattr(request.app.state, "doctor_last_report_lock", None)
    if lock is None:  # pragma: no cover
        return
    with lock:
        request.app.state.doctor_last_report = None


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
    sequence with ``app.state.doctor_fix_lock``. Callers that race a
    second ``fix=true`` request get an immediate 409 ``in_progress``
    rather than blocking on the lock or running fixes a second time.
    """
    return APIError(
        status_code=409,
        code="in_progress",
        message="Doctor fix already in progress",
        hint=(
            "Wait for the in-flight POST /doctor/run (fix=true) to complete, "
            "then retry. Doctor fixes are single-flight."
        ),
        retry_after_seconds=DOCTOR_FIX_BUSY_RETRY_AFTER_SECONDS,
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


# Shared bounded executor for all doctor work (checks + fixes + verify).
#
# Round-3 (Codex on PR #2058) flagged the prior per-request executor as a
# DoS vector: each timeout abandoned a worker thread, so a client retrying
# a stuck check could accumulate unbounded background workers. The
# round-3 fix capped that with a single module-level pool. Round-5
# (Codex on PR #2058) flagged that *the module-level pool itself* was
# unowned by app lifecycle, so timed-out workers had no shutdown story
# and pm serve exit left them alive. The executor now lives on
# ``app.state.doctor_executor``, created by the FastAPI lifespan in
# ``web_api/app.py`` and explicitly torn down on shutdown. Routes
# resolve it via :func:`_get_executor`.

_T = TypeVar("_T")


def _submit_doctor_work(
    executor: concurrent.futures.ThreadPoolExecutor,
    fn: Callable[..., _T],
    *args: Any,
    **kwargs: Any,
) -> concurrent.futures.Future[_T]:
    """Submit ``fn`` to ``executor``.

    Factored out so the ``fix=true`` path in
    :func:`run_doctor_endpoint` can own the future lifecycle (it
    needs to attach a lock-release callback that fires when the worker
    *really* terminates, not when the HTTP request times out).
    """
    return executor.submit(fn, *args, **kwargs)


def _await_with_budget(
    future: concurrent.futures.Future[_T],
    *,
    budget_s: float,
    name: str | None = None,
) -> _T:
    """Wait on ``future`` for at most ``budget_s`` seconds.

    Doctor work (``run_checks`` itself, ``apply_fixes``, the post-fix
    verify rerun) doesn't honor a Python-level timeout — individual
    checks have their own short subprocess timeouts (see ``_run_cmd``),
    but a pure-Python hang (network call, stuck storage probe, blocking
    fix on a flaky filesystem) would otherwise hold the request worker
    forever.

    On overrun we raise a typed 504 ``timeout`` immediately — the worker
    thread is *not* cancelled (Python stdlib has no safe way to interrupt
    arbitrary blocking code) and is allowed to finish in the background,
    eating its slot in the app's doctor executor until it returns. The
    HTTP caller sees the 504 within the budget; the leaked thread is the
    accepted v1 RC tradeoff (Codex round-1 P0 on PR #2058). The shared
    pool bounds the total number of leaks (Codex round-3 P0 on PR #2058).
    The lifespan (Codex round-5 P0 on PR #2058) emits a warning if any
    such worker is still alive at app shutdown so the operator has a
    paper trail. Callers attach an ``add_done_callback`` (see
    :func:`_log_check_worker_outcome` and
    :func:`_release_fix_lock_factory`) so late completion / exception
    surfaces in the server log even after the HTTP request returned 504.

    ``name`` is used purely for the 504 message; the caller supplies the
    user-facing check name (or ``None`` for "all checks").
    """
    t0 = time.monotonic()
    try:
        result = future.result(timeout=budget_s)
    except concurrent.futures.TimeoutError:
        elapsed = time.monotonic() - t0
        logger.warning(
            "doctor: %s exceeded %.1fs budget; abandoning worker thread",
            name or "run",
            budget_s,
        )
        raise _check_timeout(name, elapsed) from None

    elapsed = time.monotonic() - t0
    if elapsed > budget_s:
        # Defensive: the future returned just past the deadline. Still
        # surface 504 so the caller sees a consistent contract.
        raise _check_timeout(name, elapsed)
    return result


def _run_with_budget(
    executor: concurrent.futures.ThreadPoolExecutor,
    fn: Callable[..., _T],
    *args: Any,
    budget_s: float,
    name: str | None = None,
    **kwargs: Any,
) -> _T:
    """Submit ``fn`` to ``executor`` and await it under ``budget_s``.

    Convenience wrapper for callers that don't need to own the future
    (the ``fix=false`` path). The ``fix=true`` path submits + awaits
    explicitly so it can attach a lock-release callback to the future.
    """
    future = _submit_doctor_work(executor, fn, *args, **kwargs)
    future.add_done_callback(_log_check_worker_outcome)
    return _await_with_budget(future, budget_s=budget_s, name=name)


def _run_full_fix_under_budget(
    selected: list[Check],
) -> tuple[DoctorReport, list[dict[str, Any]]]:
    """Run ``run_checks`` → ``apply_fixes`` → verify rerun in one closure.

    Round-3 (Codex on PR #2058): the prior implementation called the
    initial run under :func:`_run_with_budget` and the verify rerun
    under :func:`_run_with_budget`, but :func:`apply_fixes` itself was
    called inline. A hanging fix (stuck subprocess, blocked filesystem
    on a network mount, jammed worktree git op) would therefore block
    the HTTP worker past the documented ``timeout_seconds`` ceiling
    with no 504.

    By packing all three phases into one closure dispatched through
    :func:`_run_with_budget`, the wall-clock budget covers the full
    sequence end-to-end. A hang in any phase trips the timeout and
    surfaces 504 within the budget.

    Returns ``(merged_report, fixes_applied)``. ``fixes_applied`` is
    the wire-shape list of attempted fixes (matches the JSON we ship
    back to clients).
    """
    report = run_checks(selected)

    # ``apply_fixes`` skips passing/skipped checks itself — no need to
    # filter on the caller side. Returns a list of
    # ``(name, success, message)`` tuples; we coerce to JSON for the
    # wire response.
    try:
        raw = apply_fixes(report)
    except Exception as exc:  # noqa: BLE001 — bubble as 500 via outer handler
        logger.exception("doctor: apply_fixes raised: %s", exc)
        raise
    fixes_applied = [
        {"name": name, "ok": ok, "message": message}
        for (name, ok, message) in raw
    ]

    # Re-run just the checks we tried to fix so the response reflects
    # the post-fix state (issue #1063 style verification: don't trust
    # the handler's self-report). All within the same budget — if the
    # caller's timeout fires mid-verify, the outer future cancels and
    # surfaces 504 just like a hang in the initial run.
    fixed_names = {entry["name"] for entry in fixes_applied}
    if fixed_names:
        rerun_checks = [c for c in selected if c.name in fixed_names]
        verify_report = run_checks(rerun_checks)
        # Splice verify-report rows back into the original report so
        # the response always reflects the latest state. The original
        # report is a dataclass — build a new merged list of
        # ``(check, result)`` tuples and mutate in place.
        verify_index = {
            check.name: result for check, result in verify_report.results
        }
        merged: list[tuple[Check, CheckResult]] = []
        for check, result in report.results:
            replacement = verify_index.get(check.name)
            merged.append((check, replacement if replacement else result))
        report.results = merged

    return report, fixes_applied


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
def get_doctor_report_endpoint(
    config: ConfigDep, request: Request,
) -> DoctorReportResponse:
    """GET /api/v1/doctor/report — the most-recent in-app report.

    Returns 404 ``not_found`` when no doctor run has happened yet
    against *this app* — callers should ``POST /doctor/run`` first.
    The cache is per-app (lives on ``request.app.state``), so two
    sibling apps in the same process keep independent reports
    (Codex round-5 on PR #2058).
    """
    _reject_non_default_config(config)
    cached = _read_last_report(request)
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
    responses={
        "401": {"description": "Missing or invalid bearer token."},
        "404": {"description": "Unknown doctor check."},
        "409": {
            "description": (
                "A fix=true doctor run is already in progress; the error "
                "body includes retry_after_seconds."
            )
        },
        "504": {"description": "Run exceeded the timeout budget."},
    },
)
def run_doctor_endpoint(
    config: ConfigDep,
    request: Request,
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
    the per-app ``doctor_fix_lock``; concurrent ``fix=true`` callers
    get a typed 409 ``busy`` (Codex round-1 P0). Worker lifecycle is
    owned by the FastAPI lifespan (Codex round-5 on PR #2058).
    """
    _reject_non_default_config(config)
    payload = body or DoctorRunRequest()

    selected = _select_checks(payload.check)
    executor = _get_executor(request)

    fixes_applied: list[dict[str, Any]] = []
    if payload.fix:
        fix_lock = _get_fix_lock(request)
        # Single-flight gate for ``fix=true``. Acquire non-blocking so a
        # second concurrent caller returns 409 immediately rather than
        # queueing behind the in-flight fix. ``fix=false`` is read-only
        # for our purposes (each check has its own internal locks) and
        # is allowed to overlap.
        if not fix_lock.acquire(blocking=False):
            raise _fix_busy_error()

        # Round-4 (Codex on PR #2058): the lock MUST be held until the
        # worker thread truly terminates, not just until the HTTP request
        # returns. The prior implementation released in a ``finally``
        # block, which ran on the 504 timeout path even though the worker
        # thread was still alive inside ``apply_fixes`` mutating shared
        # state. A retry within seconds of the 504 would acquire the lock
        # and start a *second* concurrent fix.
        #
        # The fix: submit the work ourselves, attach an
        # ``add_done_callback`` that releases the lock when the worker
        # really finishes, and DO NOT release the lock on any code path
        # in this function — the callback owns the release.
        #
        # ``add_done_callback`` runs synchronously if the future is
        # already done at registration time; otherwise it runs in the
        # worker thread once the result is set. Either way, the lock is
        # released exactly once when the work is truly complete.
        #
        # Round-5 (Codex on PR #2058): the callback also logs the
        # eventual outcome (success / exception / cancel) so a
        # timed-out fix that finishes late is observable in the
        # server log rather than disappearing silently.
        future = _submit_doctor_work(
            executor, _run_full_fix_under_budget, selected,
        )
        future.add_done_callback(_release_fix_lock_factory(fix_lock))
        # Round-3 (Codex on PR #2058): the entire run+apply+verify
        # sequence is wrapped in one budget so a hanging
        # ``apply_fixes`` can't escape the timeout contract. The
        # post-fix verify rerun shares the same wall-clock budget;
        # if apply_fixes itself consumes the budget, the future
        # times out before verify even starts and we surface 504.
        report, fixes_applied = _await_with_budget(
            future,
            budget_s=timeout_seconds,
            name=payload.check,
        )
    else:
        report = _run_with_budget(
            executor,
            run_checks,
            selected,
            budget_s=timeout_seconds,
            name=payload.check,
        )

    generated_at = _record_last_report(request, report)
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
    "_get_executor",
    "_get_fix_lock",
    "_reset_last_report",
    "get_doctor_report_endpoint",
    "list_doctor_checks_endpoint",
    "router",
    "run_doctor_endpoint",
]


# Silence unused-import lints — these names are part of the module's
# public re-export surface so tests can patch them.
_ = invalid_request
