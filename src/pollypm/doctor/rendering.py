"""Doctor formatting helpers extracted from :mod:`pollypm.doctor`."""

from __future__ import annotations

import json

import pollypm.doctor as doctor


_TICK = "\u2713"
_CROSS = "\u2717"
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
    "pipeline": "Pipeline",
    "schedulers": "Schedulers",
    "resources": "Resources",
    "inbox": "Inbox",
    "sessions": "Sessions",
}


def _category_label(category: str) -> str:
    return _CATEGORY_LABELS.get(category, category.replace("_", " ").title())


def render_human(report: doctor.DoctorReport) -> str:
    lines: list[str] = []
    last_category: str | None = None
    for check, result in report.results:
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
        lines.append(f"{glyph} {check.name}: {status}")

    passed = report.passed_count
    total = len(report.results)
    errors = len(report.errors)
    warnings = len(report.warnings)
    skipped = report.skipped_count
    lines.append("")
    lines.append(
        f"Summary: {passed}/{total} passed, {warnings} warning(s), "
        f"{errors} error(s), {skipped} skipped "
        f"({report.duration_seconds:.2f}s)"
    )
    lines.append(
        f"{total} checks · {passed} passed · {warnings} warnings · {errors} errors"
    )

    failures = [
        (c, r) for c, r in report.results
        if not r.passed and not r.skipped
    ]
    if failures:
        lines.append("")
        lines.append("Failures:")
        for check, result in failures:
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
                "data": result.data,
            }
            for check, result in report.results
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True)
