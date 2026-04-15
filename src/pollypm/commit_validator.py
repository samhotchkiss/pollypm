"""Conventional Commit message validation."""

from __future__ import annotations

from dataclasses import dataclass, field
import re


ALLOWED_TYPES = frozenset(
    {
        "build",
        "chore",
        "ci",
        "docs",
        "feat",
        "fix",
        "perf",
        "refactor",
        "revert",
        "style",
        "test",
    }
)

HEADER_RE = re.compile(
    r"^(?P<type>[a-z]+)"
    r"(?:\((?P<scope>[^()\r\n]+)\))?"
    r"(?P<breaking>!)?"
    r": "
    r"(?P<description>\S.*)$"
)
BREAKING_FOOTER_RE = re.compile(r"^BREAKING CHANGE: .+\S$")
FOOTER_RE = re.compile(r"^[A-Za-z-]+(?: #[^\s].*|: .+\S)$")


@dataclass(slots=True)
class ValidationResult:
    """Structured Conventional Commit validation output."""

    is_valid: bool
    errors: list[str] = field(default_factory=list)
    commit_type: str | None = None
    scope: str | None = None
    description: str | None = None
    body: str | None = None
    footers: list[str] = field(default_factory=list)
    has_breaking_change: bool = False


def validate_commit_message(message: str) -> ValidationResult:
    """Validate a commit message against core Conventional Commits rules."""
    errors: list[str] = []
    normalized = message.replace("\r\n", "\n").strip("\n")
    if not normalized.strip():
        return ValidationResult(
            is_valid=False,
            errors=["Commit message cannot be empty."],
        )

    lines = normalized.split("\n")
    header = lines[0]
    match = HEADER_RE.match(header)
    if match is None:
        _append_header_errors(header, errors)
        return ValidationResult(is_valid=False, errors=errors)

    commit_type = match.group("type")
    scope = match.group("scope")
    description = match.group("description").strip()
    has_breaking_change = bool(match.group("breaking"))

    if commit_type not in ALLOWED_TYPES:
        errors.append(
            "Unknown commit type "
            f"`{commit_type}`. Allowed types: {', '.join(sorted(ALLOWED_TYPES))}."
        )

    if scope is not None and not scope.strip():
        errors.append("Commit scope cannot be empty.")

    if not description:
        errors.append("Commit description cannot be empty.")

    body, footers = _parse_body_and_footers(lines[1:])
    if any(BREAKING_FOOTER_RE.match(footer) for footer in footers):
        has_breaking_change = True

    return ValidationResult(
        is_valid=not errors,
        errors=errors,
        commit_type=commit_type,
        scope=scope,
        description=description,
        body=body,
        footers=footers,
        has_breaking_change=has_breaking_change,
    )


def _append_header_errors(header: str, errors: list[str]) -> None:
    if ": " not in header:
        errors.append("Commit header must contain a `: ` separator after type/scope.")

    prefix = header.split(":", 1)[0].strip()
    type_match = re.match(r"^(?P<type>[a-z]+)", prefix)
    if type_match is None:
        errors.append("Commit header must start with a lowercase type prefix.")
        return

    commit_type = type_match.group("type")
    if commit_type not in ALLOWED_TYPES:
        errors.append(
            "Unknown commit type "
            f"`{commit_type}`. Allowed types: {', '.join(sorted(ALLOWED_TYPES))}."
        )

    if "(" in prefix and ")" not in prefix:
        errors.append("Commit scope must use balanced parentheses.")
    elif prefix.endswith("()"):
        errors.append("Commit scope cannot be empty.")

    if ": " in header:
        description = header.split(": ", 1)[1].strip()
        if not description:
            errors.append("Commit description cannot be empty.")
    else:
        errors.append("Commit description cannot be empty.")


def _parse_body_and_footers(lines: list[str]) -> tuple[str | None, list[str]]:
    if not lines:
        return None, []

    body_lines: list[str] = []
    footer_lines: list[str] = []
    footer_started = False

    for line in lines:
        if footer_started:
            footer_lines.append(line)
            continue
        if BREAKING_FOOTER_RE.match(line) or FOOTER_RE.match(line):
            footer_started = True
            footer_lines.append(line)
        else:
            body_lines.append(line)

    body = "\n".join(body_lines).strip() or None
    footers = [line for line in footer_lines if line.strip()]
    return body, footers
