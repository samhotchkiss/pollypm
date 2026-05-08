"""Audit `pm ...` command references across prompt/documentation surfaces."""

from __future__ import annotations

import re
import shlex
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
        repo_root / "src/pollypm/agent_profiles/defaults.py",
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


def _normalize_option_value(option: str, value: str) -> str:
    if value.startswith("<") and value.endswith(">"):
        if option == "--output":
            return "{}"
        if option in {"--reason", "-d"}:
            return "example"
        if option in {"--project", "-p"}:
            return "demo"
        if option == "--actor":
            return "cli"
        return "x"
    return value


def _profiles_help_args(command_text: str) -> list[str]:
    text = command_text.replace('\\"', '"').rstrip("\\").strip()
    tokens = shlex.split(text)
    assert tokens and tokens[0] == "pm", command_text

    current = get_command(app)
    args: list[str] = []
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if "|" in token:
            raise AssertionError(
                f"Command reference uses shell shorthand instead of a real pm command: {command_text}"
            )
        commands = getattr(current, "commands", None) or {}
        next_command = commands.get(token)
        if next_command is None:
            break
        args.append(token)
        current = next_command
        index += 1

    while index < len(tokens):
        token = tokens[index]
        if token.startswith("-"):
            args.append(token)
            if index + 1 < len(tokens):
                next_token = tokens[index + 1]
                if not next_token.startswith("-"):
                    args.append(_normalize_option_value(token, next_token))
                    index += 1
            index += 1
            continue
        index += 1

    return [*args, "--help"]


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


def test_core_agent_profile_prompt_commands_resolve_against_real_cli() -> None:
    runner = CliRunner()
    repo_root = _repo_root()
    path = repo_root / "src/pollypm/agent_profiles/defaults.py"
    broken: list[str] = []

    for command_text in sorted(_extract_command_references(path.read_text(encoding="utf-8"))):
        try:
            help_args = _profiles_help_args(command_text)
        except AssertionError as exc:
            broken.append(f"{path.relative_to(repo_root)}: {exc}")
            continue
        result = runner.invoke(app, help_args)
        if result.exit_code != 0:
            broken.append(
                f"{path.relative_to(repo_root)}: `{command_text}` "
                f"resolved to `{ ' '.join(help_args) }` with exit {result.exit_code}"
            )

    assert not broken, "\n".join(broken)
