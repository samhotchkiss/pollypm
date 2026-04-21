"""Audit `pm ...` command references across prompt/documentation surfaces."""

from __future__ import annotations

import re
from pathlib import Path

from typer.main import get_command
from typer.testing import CliRunner

from pollypm.cli import app

_PM_INLINE_RE = re.compile(r"`(pm[^`\n]+)`")
_PM_LINE_RE = re.compile(r"^(?:[-*•]\s+)?(pm[^\n]+)$")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _prompt_command_surfaces() -> list[Path]:
    repo_root = _repo_root()
    builtin_profiles = sorted(
        (repo_root / "src/pollypm/plugins_builtin").glob("*/profiles/*.md")
    )
    return [
        repo_root / "README.md",
        repo_root / "docs/worker-guide.md",
        repo_root / "docs/getting-started.md",
        repo_root / "src/pollypm/memory_prompts.py",
        repo_root / "src/pollypm/recovery_prompt.py",
        repo_root / "src/pollypm/plugins_builtin/core_agent_profiles/profiles.py",
        *builtin_profiles,
    ]


def _resolve_command_path(command_text: str) -> tuple[str, ...] | None:
    tokens = re.findall(r"[a-z][a-z0-9-]*", command_text)[1:]
    if not tokens:
        return ()
    current = get_command(app)
    resolved: list[str] = []
    for token in tokens:
        commands = getattr(current, "commands", None) or {}
        next_command = commands.get(token)
        if next_command is None:
            break
        resolved.append(token)
        current = next_command
    if not resolved:
        return None
    return tuple(resolved)


def _extract_command_references(text: str) -> set[str]:
    commands = set(_PM_INLINE_RE.findall(text))
    for raw_line in text.splitlines():
        line = raw_line.strip()
        match = _PM_LINE_RE.match(line)
        if match:
            command_text = match.group(1).split("#", 1)[0].strip()
            if command_text:
                commands.add(command_text)
    return commands


def test_prompt_command_references_resolve_against_real_cli() -> None:
    runner = CliRunner()
    repo_root = _repo_root()
    broken: list[str] = []

    for path in _prompt_command_surfaces():
        text = path.read_text(encoding="utf-8")
        commands = sorted(_extract_command_references(text))
        for command_text in commands:
            resolved = _resolve_command_path(command_text)
            if resolved is None:
                broken.append(
                    f"{path.relative_to(repo_root)}: unknown `{command_text}`"
                )
                continue
            help_args = ["--help"] if not resolved else [*resolved, "--help"]
            result = runner.invoke(app, help_args)
            if result.exit_code != 0:
                broken.append(
                    f"{path.relative_to(repo_root)}: `{command_text}` "
                    f"help failed with exit {result.exit_code}"
                )

    assert not broken, "\n".join(broken)
