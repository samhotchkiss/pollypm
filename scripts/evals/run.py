#!/usr/bin/env python3
"""Lane H eval harness for PollyPM agent behavior checks.

The runner deliberately lives outside ``src/pollypm`` and talks to the
daemon only through the public ``/api/v1/chat/*`` HTTP API.
"""

from __future__ import annotations

import argparse
import dataclasses
import html
import json
import os
import re
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml


DEFAULT_BASE_URL = "http://127.0.0.1:8765"
DEFAULT_CONFIG_PATH = Path.home() / ".pollypm" / "pollypm.toml"
DEFAULT_TOKEN_PATH = Path.home() / ".pollypm" / "api-token"

ROLE_DEFAULT_RESPONSES = {
    "architect": (
        "Next sprint priority candidates:\n"
        "1. Stabilize the public chat API because evals and UI lanes need reliable "
        "operator-visible transcripts.\n"
        "2. Improve task lifecycle observability so queued, blocked, and review states "
        "are explainable from public endpoints.\n"
        "3. Expand pre-ship automation with smoke, perf, and eval harnesses so release "
        "readiness is repeatable."
    ),
    "advisor": (
        "The biggest risk is overbuilding the proposed plan before validating the "
        "public API contract. A cheaper alternative is to ship a thin scaffold first, "
        "then add cases after the transcript and task contracts are stable."
    ),
    "worker": (
        "Action plan:\n"
        "1. Inspect the task and acceptance criteria.\n"
        "2. Implement the smallest verifiable change.\n"
        "3. Run targeted tests and report the result for review."
    ),
    "operator": (
        "Pause only the projects that share the work-table dependency, then ask the "
        "architect for a quick risk check before resuming active workers."
    ),
}

CATEGORY_PATTERNS = {
    "planning": [
        r"\b(priority|plan|candidate|next sprint|roadmap|trade-?off)\b",
        r"^\s*(?:\d+[\.)]|[-*])\s+",
    ],
    "question": [r"\?", r"\b(clarify|which|what|should i|do you want)\b"],
    "action": [r"\b(action|implement|run|create|send|pause|queue|mark|execute)\b"],
    "refusal": [
        r"\b(refus|injection|untrusted|missing.+auth|cannot.+verify|will.+not.+comply)\b"
    ],
    "critique": [r"\b(risk|cheaper alternative|pressure-test|trade-?off|concern)\b"],
}


@dataclasses.dataclass(frozen=True)
class EvalCase:
    id: str
    role: str
    session_template: str
    prompt: str
    assertions: dict[str, Any]
    timeout_seconds: int = 90
    canned_response: str | None = None
    path: Path | None = None


@dataclasses.dataclass(frozen=True)
class AssertionFailure:
    assertion: str
    message: str


@dataclasses.dataclass(frozen=True)
class CaseResult:
    case: EvalCase
    session_name: str
    response: str
    passed: bool
    failures: list[AssertionFailure]
    duration_seconds: float
    dry_run: bool


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a YAML mapping")
    return data


def _coerce_case(path: Path, data: dict[str, Any], defaults: dict[str, Any]) -> EvalCase:
    merged = {**defaults, **data}
    required = ["id", "role", "session_template", "prompt", "assertions"]
    missing = [key for key in required if key not in merged]
    if missing:
        raise ValueError(f"{path}: missing required field(s): {', '.join(missing)}")
    if not isinstance(merged["assertions"], dict):
        raise ValueError(f"{path}: assertions must be a mapping")
    return EvalCase(
        id=str(merged["id"]),
        role=str(merged["role"]),
        session_template=str(merged["session_template"]),
        prompt=str(merged["prompt"]),
        assertions=dict(merged["assertions"]),
        timeout_seconds=int(merged.get("timeout_seconds", 90)),
        canned_response=(
            str(merged["canned_response"])
            if merged.get("canned_response") is not None
            else None
        ),
        path=path,
    )


def load_cases(
    *,
    manifest: Path | None = None,
    case_paths: list[Path] | None = None,
    cases_dir: Path | None = None,
) -> list[EvalCase]:
    """Load eval cases from a manifest, explicit paths, and/or a directory."""

    defaults: dict[str, Any] = {}
    paths: list[Path] = []
    if manifest is not None:
        manifest_data = _read_yaml(manifest)
        defaults = dict(manifest_data.get("defaults") or {})
        manifest_cases = manifest_data.get("cases")
        if not isinstance(manifest_cases, list):
            raise ValueError(f"{manifest}: cases must be a list")
        for entry in manifest_cases:
            if isinstance(entry, str):
                paths.append((manifest.parent / entry).resolve())
            elif isinstance(entry, dict) and "path" in entry:
                paths.append((manifest.parent / str(entry["path"])).resolve())
            elif isinstance(entry, dict):
                case_id = str(entry.get("id", "inline-case"))
                paths.append(Path(f"<manifest:{case_id}>"))
            else:
                raise ValueError(f"{manifest}: unsupported case entry {entry!r}")

        inline_cases = [
            entry for entry in manifest_cases if isinstance(entry, dict) and "path" not in entry
        ]
        loaded = [_coerce_case(Path(f"<manifest:{entry.get('id', 'inline-case')}>"), entry, defaults) for entry in inline_cases]
        file_paths = [path for path in paths if not str(path).startswith("<manifest:")]
        return loaded + [_coerce_case(path, _read_yaml(path), defaults) for path in file_paths]

    if cases_dir is not None:
        paths.extend(sorted(cases_dir.glob("*.yaml")))
        paths.extend(sorted(cases_dir.glob("*.yml")))
    paths.extend(case_paths or [])
    if not paths:
        raise ValueError("no eval cases selected")
    return [_coerce_case(path, _read_yaml(path), defaults) for path in paths]


def resolve_session_name(template: str, *, project: str) -> str:
    return template.replace("<project>", project).format(project=project)


def classify_response(text: str) -> set[str]:
    categories: set[str] = set()
    for category, patterns in CATEGORY_PATTERNS.items():
        if all(re.search(pattern, text, re.IGNORECASE | re.MULTILINE) for pattern in patterns):
            categories.add(category)
    return categories


def _count_candidates(text: str) -> int:
    numbered = re.findall(r"(?m)^\s*(?:candidate\s*)?\d+[\.)]\s+\S", text, re.IGNORECASE)
    bullets = re.findall(r"(?m)^\s*[-*]\s+\S", text)
    explicit = len(re.findall(r"\bcandidate\b", text, re.IGNORECASE))
    return max(len(numbered), len(bullets), explicit)


def _structural_count(name: str, text: str) -> int:
    if name == "candidates_count":
        return _count_candidates(text)
    if name == "paragraphs_count":
        return len([part for part in re.split(r"\n\s*\n", text.strip()) if part.strip()])
    if name == "bullets_count":
        return len(re.findall(r"(?m)^\s*(?:[-*]|\d+[\.)])\s+\S", text))
    raise ValueError(f"unknown structural count assertion: {name}")


def apply_assertions(case: EvalCase, response: str) -> list[AssertionFailure]:
    failures: list[AssertionFailure] = []
    assertions = case.assertions

    for item in assertions.get("must_contain_at_least", []) or []:
        if not isinstance(item, dict):
            failures.append(
                AssertionFailure("must_contain_at_least", f"expected mapping, got {item!r}")
            )
            continue
        for name, expected in item.items():
            actual = _structural_count(str(name), response)
            if actual < int(expected):
                failures.append(
                    AssertionFailure(
                        str(name), f"expected at least {expected}, found {actual}"
                    )
                )

    for pattern in assertions.get("must_match_regex", []) or []:
        if re.search(str(pattern), response, re.MULTILINE) is None:
            failures.append(
                AssertionFailure("must_match_regex", f"missing regex: {pattern}")
            )

    for pattern in assertions.get("must_not_match_regex", []) or []:
        if re.search(str(pattern), response, re.MULTILINE) is not None:
            failures.append(
                AssertionFailure("must_not_match_regex", f"forbidden regex matched: {pattern}")
            )

    for keyword in assertions.get("must_contain_keywords", []) or []:
        if str(keyword).lower() not in response.lower():
            failures.append(
                AssertionFailure("must_contain_keywords", f"missing keyword: {keyword}")
            )

    for keyword in assertions.get("must_not_contain_keywords", []) or []:
        if str(keyword).lower() in response.lower():
            failures.append(
                AssertionFailure(
                    "must_not_contain_keywords", f"forbidden keyword present: {keyword}"
                )
            )

    expected_category = assertions.get("response_category")
    if expected_category:
        categories = classify_response(response)
        if str(expected_category) not in categories:
            failures.append(
                AssertionFailure(
                    "response_category",
                    f"expected {expected_category!r}, classified as {sorted(categories)}",
                )
            )

    return failures


def capture_model_versions(config_path: Path | None = None) -> dict[str, str]:
    path = config_path or DEFAULT_CONFIG_PATH
    if not path.exists():
        return {"config": f"unavailable ({path})"}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        return {"config": f"unreadable ({path}: {exc})"}

    sessions = data.get("sessions")
    if not isinstance(sessions, dict):
        return {"config": f"no [sessions] table in {path}"}

    versions: dict[str, str] = {}
    for name, session in sorted(sessions.items()):
        if not isinstance(session, dict):
            continue
        role = str(session.get("role", name))
        provider = str(session.get("provider", "unknown-provider"))
        model = str(session.get("model", "default"))
        versions[str(name)] = f"{role}: {provider}/{model}"
    return versions or {"config": f"no sessions with model metadata in {path}"}


class ChatApiClient:
    def __init__(self, *, base_url: str, token: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        url = f"{self.base_url}{path}{query}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{method} {url} failed: HTTP {exc.code} {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"{method} {url} failed: {exc.reason}") from exc
        return json.loads(payload) if payload else {}

    def latest_message_id(self, session_name: str) -> str | None:
        payload = self._request(
            "GET",
            f"/api/v1/chat/{urllib.parse.quote(session_name)}/messages",
            params={"limit": 1, "direction": "desc"},
        )
        messages = payload.get("messages") or []
        if not messages:
            return None
        return str(messages[0].get("id") or "") or None

    def send_prompt(self, session_name: str, prompt: str) -> None:
        self._request(
            "POST",
            f"/api/v1/chat/{urllib.parse.quote(session_name)}/send",
            body={"text": prompt, "press_enter": True},
        )

    def poll_assistant_response(
        self,
        session_name: str,
        *,
        since_id: str | None,
        timeout_seconds: int,
    ) -> str:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            params: dict[str, Any] = {"limit": 100, "direction": "asc"}
            if since_id:
                params["since_id"] = since_id
            payload = self._request(
                "GET",
                f"/api/v1/chat/{urllib.parse.quote(session_name)}/messages",
                params=params,
            )
            for message in payload.get("messages") or []:
                if message.get("role") == "assistant" and str(message.get("text") or "").strip():
                    return str(message["text"])
            time.sleep(2)
        raise TimeoutError(
            f"timed out waiting {timeout_seconds}s for assistant response in {session_name}"
        )


def _canned_response_for(case: EvalCase, canned: dict[str, str]) -> str:
    return (
        case.canned_response
        or canned.get(case.id)
        or canned.get(case.role)
        or ROLE_DEFAULT_RESPONSES.get(case.role)
        or "Dry-run canned response."
    )


def run_cases(
    cases: list[EvalCase],
    *,
    project: str,
    dry_run: bool,
    client: ChatApiClient | None = None,
    canned: dict[str, str] | None = None,
) -> list[CaseResult]:
    results: list[CaseResult] = []
    canned = canned or {}
    for case in cases:
        started = time.monotonic()
        session_name = resolve_session_name(case.session_template, project=project)
        if dry_run:
            response = _canned_response_for(case, canned)
        else:
            if client is None:
                raise ValueError("live eval run requires a ChatApiClient")
            since_id = client.latest_message_id(session_name)
            client.send_prompt(session_name, case.prompt)
            response = client.poll_assistant_response(
                session_name, since_id=since_id, timeout_seconds=case.timeout_seconds
            )
        failures = apply_assertions(case, response)
        results.append(
            CaseResult(
                case=case,
                session_name=session_name,
                response=response,
                passed=not failures,
                failures=failures,
                duration_seconds=time.monotonic() - started,
                dry_run=dry_run,
            )
        )
    return results


def render_markdown_report(
    results: list[CaseResult],
    *,
    model_versions: dict[str, str],
    generated_at: datetime | None = None,
) -> str:
    generated_at = generated_at or datetime.now(UTC)
    passed = sum(1 for result in results if result.passed)
    total = len(results)
    lines = [
        "# PollyPM Agent Evals Report",
        "",
        f"- Generated: {generated_at.isoformat()}",
        f"- Result: {passed}/{total} passed",
        "- Model versions:",
    ]
    for name, version in model_versions.items():
        lines.append(f"  - `{name}`: {version}")
    lines.extend(
        [
            "",
            "| Case | Role | Session | Result | Duration | Failures |",
            "|---|---|---|---:|---:|---|",
        ]
    )
    for result in results:
        failures = "; ".join(f"{f.assertion}: {f.message}" for f in result.failures)
        lines.append(
            "| {case} | {role} | `{session}` | {status} | {duration:.2f}s | {failures} |".format(
                case=result.case.id,
                role=result.case.role,
                session=result.session_name,
                status="PASS" if result.passed else "FAIL",
                duration=result.duration_seconds,
                failures=failures or "",
            )
        )
    return "\n".join(lines) + "\n"


def render_html_report(
    results: list[CaseResult],
    *,
    model_versions: dict[str, str],
    generated_at: datetime | None = None,
) -> str:
    markdown = render_markdown_report(
        results, model_versions=model_versions, generated_at=generated_at
    )
    rows = []
    for result in results:
        failures = "; ".join(f"{f.assertion}: {f.message}" for f in result.failures)
        rows.append(
            "<tr>"
            f"<td>{html.escape(result.case.id)}</td>"
            f"<td>{html.escape(result.case.role)}</td>"
            f"<td><code>{html.escape(result.session_name)}</code></td>"
            f"<td>{'PASS' if result.passed else 'FAIL'}</td>"
            f"<td>{result.duration_seconds:.2f}s</td>"
            f"<td>{html.escape(failures)}</td>"
            "</tr>"
        )
    model_items = "\n".join(
        f"<li><code>{html.escape(name)}</code>: {html.escape(version)}</li>"
        for name, version in model_versions.items()
    )
    return (
        "<!doctype html>\n"
        "<html><head><meta charset=\"utf-8\"><title>PollyPM Agent Evals Report</title>"
        "<style>body{font-family:sans-serif;margin:2rem}table{border-collapse:collapse}"
        "td,th{border:1px solid #ccc;padding:.35rem .5rem}th{text-align:left}"
        "pre{background:#f6f8fa;padding:1rem;overflow:auto}</style></head><body>"
        "<h1>PollyPM Agent Evals Report</h1>"
        f"<p><strong>Result:</strong> {sum(1 for r in results if r.passed)}/{len(results)} passed</p>"
        f"<h2>Model versions</h2><ul>{model_items}</ul>"
        "<table><thead><tr><th>Case</th><th>Role</th><th>Session</th>"
        "<th>Result</th><th>Duration</th><th>Failures</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
        "<h2>Markdown</h2>"
        f"<pre>{html.escape(markdown)}</pre>"
        "</body></html>\n"
    )


def _load_token(path: Path) -> str:
    token = os.environ.get("POLLYPM_API_TOKEN")
    if token:
        return token
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    raise ValueError(
        f"API token not found. Set POLLYPM_API_TOKEN or create {path}."
    )


def _self_test_case() -> EvalCase:
    return EvalCase(
        id="self-test-planning",
        role="architect",
        session_template="architect_<project>",
        prompt="Give me 3 candidates for the next priority.",
        assertions={
            "must_contain_at_least": [{"candidates_count": 3}],
            "must_match_regex": [r"(?i)(priority|next sprint)"],
            "must_not_match_regex": [r"(?i)i am happy to help"],
            "response_category": "planning",
        },
        timeout_seconds=1,
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, help="YAML manifest listing eval cases")
    parser.add_argument("--case", action="append", type=Path, default=[], help="YAML case path")
    parser.add_argument("--cases-dir", type=Path, help="Directory of YAML cases")
    parser.add_argument("--project", default="pollypm", help="Project token for session templates")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="PollyPM web API base URL")
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_PATH)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--dry-run", action="store_true", help="Use canned responses, no daemon")
    parser.add_argument("--self-test", action="store_true", help="Run built-in assertion self-test")
    parser.add_argument(
        "--format", choices=["markdown", "html"], default="markdown", help="Report format"
    )
    parser.add_argument("--output", type=Path, help="Write report to this path")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        if args.self_test:
            cases = [_self_test_case()]
            dry_run = True
        else:
            cases = load_cases(
                manifest=args.manifest,
                case_paths=args.case,
                cases_dir=args.cases_dir,
            )
            dry_run = args.dry_run

        client = None
        if not dry_run:
            client = ChatApiClient(
                base_url=args.base_url,
                token=_load_token(args.token_file),
            )
        results = run_cases(cases, project=args.project, dry_run=dry_run, client=client)
        model_versions = capture_model_versions(args.config)
        if args.format == "html":
            report = render_html_report(results, model_versions=model_versions)
        else:
            report = render_markdown_report(results, model_versions=model_versions)
        if args.output:
            args.output.write_text(report, encoding="utf-8")
        else:
            print(report, end="")
        return 0 if all(result.passed for result in results) else 1
    except Exception as exc:  # pragma: no cover - CLI guardrail
        print(f"evals error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
