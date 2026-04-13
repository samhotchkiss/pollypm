"""Generate and repair PollyPM instruction docs for projects.

Bundles system reference docs into each project's .pollypm/ directory
so that agents can read them regardless of provider (Claude, Codex, etc.).
"""
from __future__ import annotations

import shutil
from pathlib import Path

# Package-bundled defaults
_DEFAULTS_DIR = Path(__file__).resolve().parent / "defaults" / "docs"
_REFERENCE_SRC = _DEFAULTS_DIR / "reference"
_INSTRUCT_TEMPLATE = _DEFAULTS_DIR / "INSTRUCT.md.template"


def scaffold_docs(project_path: Path, *, force: bool = False) -> list[str]:
    """Copy reference docs and generate INSTRUCT.md for a project.

    Returns a list of actions taken (for reporting in pm repair).
    If force=True, overwrite existing files. Otherwise skip files that exist.
    """
    actions: list[str] = []
    instruction_dir = project_path / ".pollypm"
    instruction_dir.mkdir(parents=True, exist_ok=True)

    # -- SYSTEM.md (PollyPM system reference, separate from project INSTRUCT.md) --
    system_dest = instruction_dir / "docs" / "SYSTEM.md"
    system_dest.parent.mkdir(parents=True, exist_ok=True)
    if _INSTRUCT_TEMPLATE.exists() and (force or not system_dest.exists()):
        shutil.copyfile(_INSTRUCT_TEMPLATE, system_dest)
        actions.append(f"{'overwrote' if force else 'created'} .pollypm/docs/SYSTEM.md")

    # -- Rules/magic manifest (so agents know what's available) --
    try:
        from pollypm.rules import discover_rules, discover_magic
        manifest_lines = ["# Available Rules & Magic", ""]
        rules = discover_rules(project_path)
        if rules:
            manifest_lines.append("## Rules")
            manifest_lines.append("Read the relevant rule file BEFORE starting that type of work.")
            manifest_lines.append("")
            manifest_lines.append("| Rule | When to use | Path |")
            manifest_lines.append("|------|------------|------|")
            for name, cat in sorted(rules.items()):
                manifest_lines.append(f"| {name} | {cat.trigger} | {cat.display_path} |")
            manifest_lines.append("")
        magic = discover_magic(project_path)
        if magic:
            manifest_lines.append("## Magic")
            manifest_lines.append("Use these when the situation calls for them.")
            manifest_lines.append("")
            manifest_lines.append("| Magic | When to use | Path |")
            manifest_lines.append("|-------|------------|------|")
            for name, cat in sorted(magic.items()):
                manifest_lines.append(f"| {name} | {cat.trigger} | {cat.display_path} |")
            manifest_lines.append("")
        if rules or magic:
            manifest_dest = instruction_dir / "docs" / "rules-manifest.md"
            manifest_dest.parent.mkdir(parents=True, exist_ok=True)
            manifest_content = "\n".join(manifest_lines)
            if force or not manifest_dest.exists() or manifest_dest.read_text() != manifest_content:
                manifest_dest.write_text(manifest_content)
                actions.append(f"{'overwrote' if force else 'created'} .pollypm/docs/rules-manifest.md")
    except Exception:  # noqa: BLE001
        pass  # rules discovery is best-effort

    # -- Reference docs --
    if _REFERENCE_SRC.is_dir():
        ref_dest = instruction_dir / "docs" / "reference"
        ref_dest.mkdir(parents=True, exist_ok=True)
        for src_file in sorted(_REFERENCE_SRC.glob("*.md")):
            dest_file = ref_dest / src_file.name
            if force or not dest_file.exists():
                shutil.copyfile(src_file, dest_file)
                rel = f".pollypm/docs/reference/{src_file.name}"
                actions.append(f"{'overwrote' if force else 'created'} {rel}")

    return actions


def repair_docs(project_path: Path) -> list[str]:
    """Force-regenerate all docs. Used by pm repair."""
    return scaffold_docs(project_path, force=True)


def verify_docs(project_path: Path) -> list[str]:
    """Check which docs are missing or outdated. Returns list of problems."""
    problems: list[str] = []
    instruction_dir = project_path / ".pollypm"

    system_dest = instruction_dir / "docs" / "SYSTEM.md"
    if not system_dest.exists():
        problems.append("missing .pollypm/docs/SYSTEM.md")

    if _REFERENCE_SRC.is_dir():
        ref_dest = instruction_dir / "docs" / "reference"
        for src_file in sorted(_REFERENCE_SRC.glob("*.md")):
            dest_file = ref_dest / src_file.name
            if not dest_file.exists():
                problems.append(f"missing .pollypm/docs/reference/{src_file.name}")
            elif dest_file.read_bytes() != src_file.read_bytes():
                problems.append(f"outdated .pollypm/docs/reference/{src_file.name}")

    return problems
