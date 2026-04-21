"""Audit live ``pm ...`` references in maintained docs against the real CLI.

This keeps the front-door docs from drifting onto renamed or removed
commands. Historical/generated docs are excluded on purpose: they contain
snapshots, archival transcripts, and background material rather than live
operator guidance.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

import click
from typer.main import get_command

from pollypm.cli import app


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOCS_ROOT = PROJECT_ROOT / "docs"
ROOT_COMMAND = get_command(app)
COMMAND_REF_RE = re.compile(r"\bpm(?:\s+[a-z][a-z0-9-]*)+")
INLINE_CODE_RE = re.compile(r"`([^`\n]*\bpm\b[^`\n]*)`")
FENCED_BLOCK_RE = re.compile(r"```(?:bash|shell|sh)?\n(.*?)```", re.S)

# These files are archival, generated, or snapshot-heavy rather than active
# operator docs, so they are intentionally excluded from the live CLI drift
# gate.
EXCLUDED_TOP_LEVEL_DOCS = {
    "activity-log.md",
    "decisions.md",
    "deprecated-facts.md",
    "history.md",
    "ideas.md",
    "project-overview.md",
    "status-report.md",
    "system-state-2026-04-11.md",
}


@dataclass(frozen=True)
class CommandReference:
    path: Path
    line_number: int
    reference: str


def _audited_paths() -> list[Path]:
    docs = [
        path
        for path in sorted(DOCS_ROOT.glob("*.md"))
        if path.name not in EXCLUDED_TOP_LEVEL_DOCS
    ]
    return [
        PROJECT_ROOT / "README.md",
        PROJECT_ROOT / "issues" / "instructions.md",
        *docs,
    ]


def _iter_command_references(path: Path) -> list[CommandReference]:
    text = path.read_text(encoding="utf-8")
    refs: list[CommandReference] = []

    for match in INLINE_CODE_RE.finditer(text):
        refs.extend(
            _refs_from_snippet(
                path=path,
                line_number=text.count("\n", 0, match.start()) + 1,
                snippet=match.group(1),
            )
        )

    for match in FENCED_BLOCK_RE.finditer(text):
        block_start = text.count("\n", 0, match.start()) + 1
        for offset, line in enumerate(match.group(1).splitlines()):
            refs.extend(
                _refs_from_snippet(
                    path=path,
                    line_number=block_start + offset + 1,
                    snippet=line,
                )
            )

    return refs


def _refs_from_snippet(*, path: Path, line_number: int, snippet: str) -> list[CommandReference]:
    return [
        CommandReference(path=path, line_number=line_number, reference=match.group(0))
        for match in COMMAND_REF_RE.finditer(snippet)
    ]


def _resolve_reference(reference: str) -> bool:
    tokens = reference.split()[1:]
    if not tokens:
        return False

    node: click.Command = ROOT_COMMAND
    consumed_any = False

    for token in tokens:
        child = _lookup_child(node, token)
        if child is not None:
            node = child
            consumed_any = True
            continue

        # If we are already at a concrete command, the remaining words are
        # positional args like account names, session targets, or roles.
        return consumed_any and not isinstance(node, click.Group)

    return consumed_any


def _lookup_child(node: click.Command, token: str) -> click.Command | None:
    if not isinstance(node, click.Group):
        return None
    return node.commands.get(token)


def test_live_doc_command_references_match_cli():
    failures: list[str] = []

    for path in _audited_paths():
        for ref in _iter_command_references(path):
            if _resolve_reference(ref.reference):
                continue
            relpath = ref.path.relative_to(PROJECT_ROOT)
            failures.append(f"{relpath}:{ref.line_number} -> {ref.reference}")

    assert not failures, "Invalid docs command references:\n" + "\n".join(failures)
