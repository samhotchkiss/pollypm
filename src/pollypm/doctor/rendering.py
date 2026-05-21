"""Doctor formatting helpers extracted from :mod:`pollypm.doctor`."""

from __future__ import annotations

import json
from typing import Any

import pollypm.doctor as doctor


_TICK = "✓"
_CROSS = "✗"
_WARN = "!"
_SKIP = "-"

_CATEGORY_LABELS: dict[str, str] = {
    "system": "Environment",
    "install": "Install",
    "plugins": "Plugins",
    "migrations": "Migrations",
    "filesystem": "Filesystem",
    "tmux": "Tmux",
    "network": "Network",
    "roles": "Roles",
    "pipeline": "Pipeline",
    "guides": "Guide Drift",
    "schedulers": "Schedulers",
    "resources": "Resources",
    "inbox": "Inbox",
    "sessions": "Sessions",
}


# --------------------------------------------------------------------- #
# Clustering
# --------------------------------------------------------------------- #
#
# A long pm doctor run can pile up dozens of structurally-identical
# warnings (e.g. project-guide-drift across 12 projects, each with
# multiple roles). The bottom Failures: block then dwarfs the
# checklist itself — a screen of repeated Why/Fix blob per row that
# the operator can't act on individually.
#
# We cluster INSIDE a single failing check whose data payload lists
# ≥ CLUSTER_THRESHOLD sub-alerts (e.g. result.data["stale"] = [...]),
# replacing that one row's verbose Why/Fix block with a compact
# "N alerts across M projects" headline + N=3 sample subjects +
# pointer to --fix / --verbose.
#
# Why per-check rather than across-checks:
# - each Check is uniquely named, so cross-check clustering would
#   collapse unrelated alert types together;
# - the granular alerts live inside one CheckResult's data payload
#   (the "list" field, e.g. stale/items/orphans/dead/missing/behind),
#   which is the natural clustering unit;
# - the --fix machinery already operates per-check, so the clustered
#   "Run pm doctor --fix to bulk-resolve" pointer maps 1:1 to the
#   existing fix_fn surface.

CLUSTER_THRESHOLD = 3
CLUSTER_SAMPLE_SIZE = 3

# Known list-shaped fields inside CheckResult.data that carry per-item
# alerts. Ordered by specificity — the first one with len ≥ threshold
# wins. Keep this list in sync with the data shapes emitted by the
# checks in src/pollypm/doctor.py (grep "data={" there).
_ALERT_LIST_KEYS: tuple[str, ...] = (
    "stale",
    "drifted",
    "items",
    "entries",
    "orphans",
    "missing",
    "behind",
    "dead",
    "samples",
    "failures",
)

# Known subject-shaped fields inside a single alert dict, in order
# of preference. Pairs like (project, role) get joined with a colon
# to match the spec's "booktalk:architect" sample format.
_SUBJECT_KEY_PAIRS: tuple[tuple[str, str], ...] = (
    ("project", "role"),
    ("project_name", "role"),
)
_SUBJECT_KEYS: tuple[str, ...] = (
    "subject",
    "name",
    "project",
    "project_name",
    "path",
    "role",
    "host",
    "task",
    "session",
    "id",
)


def _category_label(category: str) -> str:
    return _CATEGORY_LABELS.get(category, category.replace("_", " ").title())


def _alert_list(result: doctor.CheckResult) -> list[Any]:
    """Return the most-specific list-shaped alert payload inside ``result.data``.

    An empty list is returned when no recognised list field is present
    OR the payload doesn't reach the clustering threshold — callers
    use len(...) >= CLUSTER_THRESHOLD to gate the clustered render.
    """
    data = result.data or {}
    if not isinstance(data, dict):
        return []
    for key in _ALERT_LIST_KEYS:
        value = data.get(key)
        if isinstance(value, list) and value:
            return list(value)
    return []


def _subject_for(item: Any) -> str:
    """Extract a short subject label for a single sub-alert.

    Dict items prefer the (project, role) pair (yielding "booktalk:architect"),
    then fall back to a single key from ``_SUBJECT_KEYS``. Scalars are
    stringified as-is. Anything unrecognised becomes "<item>".
    """
    if isinstance(item, dict):
        for left_key, right_key in _SUBJECT_KEY_PAIRS:
            left = item.get(left_key)
            right = item.get(right_key)
            if left and right:
                return f"{left}:{right}"
        for key in _SUBJECT_KEYS:
            value = item.get(key)
            if value:
                return str(value)
        # Last-ditch: the first non-empty value, just so we don't print
        # "<item>" when SOME label exists.
        for value in item.values():
            if value:
                return str(value)
        return "<item>"
    if item is None:
        return "<item>"
    return str(item)


def _cluster_summary_lines(
    check: doctor.Check,
    result: doctor.CheckResult,
    alerts: list[Any],
) -> list[str]:
    """Render the clustered detail block for a single failing check."""
    count = len(alerts)
    subjects = [_subject_for(item) for item in alerts[:CLUSTER_SAMPLE_SIZE]]
    remaining = count - len(subjects)
    sample = ", ".join(subjects)
    if remaining > 0:
        sample = f"{sample} ... (+{remaining} more)"
    lines: list[str] = []
    # Headline mirrors the existing row's status text but reframed as
    # a cluster count — operators scanning the Failures: block see
    # "N alerts" before any other detail.
    data = result.data or {}
    project_count = data.get("projects") if isinstance(data, dict) else None
    if isinstance(project_count, int) and project_count > 0:
        project_word = "project" if project_count == 1 else "projects"
        headline = f"{check.name}: {count} alerts across {project_count} {project_word}"
    else:
        alert_word = "alert" if count == 1 else "alerts"
        headline = f"{check.name}: {count} {alert_word}"
    glyph = _WARN if result.severity == "warning" else _CROSS
    lines.append(f"{glyph} {headline}")
    lines.append(f"  Sample subjects: {sample}")
    fix_hint = (
        "Run `pm doctor --fix` to bulk-resolve, or `pm doctor --verbose` for full list."
        if doctor._auto_fix_supported(result.auto_fix) or result.fixable
        else "Run `pm doctor --verbose` for full list."
    )
    lines.append(f"  {fix_hint}")
    return lines


def render_human(
    report: doctor.DoctorReport,
    *,
    verbose: bool = False,
    alert_type: str | None = None,
) -> str:
    """Render the human-readable doctor checklist.

    ``verbose`` — when True, never cluster; show the full Why/Fix block
        for every failing check (legacy behaviour, pre-#1988 cluster
        work).
    ``alert_type`` — when set, restrict the report to checks whose
        ``check.name`` matches; renders verbose (per-row) detail for
        the filtered subset so the operator can drill into one
        cluster.
    """
    results = report.results
    if alert_type is not None:
        results = [(c, r) for c, r in results if c.name == alert_type]
        # When the operator drilled into a specific alert_type, show
        # the verbose detail by default — they asked to inspect THIS
        # cluster, so the truncated cluster summary would defeat the
        # ask. --verbose toggles between cluster-and-detail and
        # detail-only at the top level; --alert-type forces detail
        # for the filtered slice.
        verbose = True

    lines: list[str] = []
    last_category: str | None = None
    for check, result in results:
        if check.category != last_category:
            if last_category is not None:
                lines.append("")
            lines.append(f"-- {_category_label(check.category)} --")
            last_category = check.category
        if result.skipped:
            glyph = _SKIP
        elif result.passed:
            glyph = _TICK
        elif result.severity == "warning":
            glyph = _WARN
        else:
            glyph = _CROSS
        status = result.status or ("ok" if result.passed else "fail")
        badge = " [f] Fix" if not result.passed and doctor._auto_fix_supported(result.auto_fix) else ""
        lines.append(f"{glyph} {check.name}: {status}{badge}")

    passed = sum(1 for _, r in results if r.passed)
    total = len(results)
    errors_list = [
        (c, r) for c, r in results
        if not r.passed and not r.skipped and r.severity == "error"
    ]
    warnings_list = [
        (c, r) for c, r in results
        if not r.passed and not r.skipped and r.severity == "warning"
    ]
    errors = len(errors_list)
    warnings = len(warnings_list)
    skipped = sum(1 for _, r in results if r.skipped)
    warning_word = "warning" if warnings == 1 else "warnings"
    error_word = "error" if errors == 1 else "errors"
    check_word = "check" if total == 1 else "checks"
    lines.append("")
    lines.append(
        f"Summary: {passed}/{total} passed, {warnings} {warning_word}, "
        f"{errors} {error_word}, {skipped} skipped "
        f"({report.duration_seconds:.2f}s)"
    )
    lines.append(
        f"{total} {check_word} · {passed} passed · "
        f"{warnings} {warning_word} · {errors} {error_word}"
    )

    failures = [
        (c, r) for c, r in results
        if not r.passed and not r.skipped
    ]
    if failures:
        lines.append("")
        lines.append("Failures:")
        for check, result in failures:
            alerts = _alert_list(result)
            if not verbose and len(alerts) >= CLUSTER_THRESHOLD:
                lines.append("")
                lines.extend(_cluster_summary_lines(check, result, alerts))
                continue
            glyph = _WARN if result.severity == "warning" else _CROSS
            lines.append("")
            lines.append(f"{glyph} {check.name}: {result.status}")
            if result.why:
                lines.append("")
                lines.append(f"  Why: {result.why}")
            if result.fix:
                lines.append("")
                for fix_line in result.fix.splitlines():
                    lines.append(f"  {fix_line}" if fix_line else "")
    return "\n".join(lines)


def render_json(report: doctor.DoctorReport) -> str:
    payload = {
        "ok": report.ok,
        "duration_seconds": round(report.duration_seconds, 4),
        "summary": {
            "total": len(report.results),
            "passed": report.passed_count,
            "warnings": len(report.warnings),
            "errors": len(report.errors),
            "skipped": report.skipped_count,
        },
        "checks": [
            {
                "name": check.name,
                "category": check.category,
                "passed": result.passed,
                "skipped": result.skipped,
                "severity": result.severity,
                "status": result.status,
                "why": result.why,
                "fix": result.fix,
                "fixable": result.fixable,
                "auto_fix": doctor._auto_fix_payload(result.auto_fix),
                "auto_fix_available": doctor._auto_fix_supported(result.auto_fix),
                "data": result.data,
            }
            for check, result in report.results
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True)
