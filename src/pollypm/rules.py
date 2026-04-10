from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path


@dataclass(slots=True)
class CatalogFile:
    name: str
    description: str
    trigger: str
    source_path: Path
    display_path: str
    content: str


def _parse_catalog_metadata(
    path: Path,
    content: str,
    *,
    default_description: str,
    default_trigger: str,
) -> CatalogFile:
    name = path.stem
    description = ""
    trigger = ""
    for line in content.splitlines()[:20]:
        stripped = line.strip()
        lowered = stripped.casefold()
        if lowered.startswith("description:"):
            description = stripped.split(":", 1)[1].strip()
        elif lowered.startswith("trigger:"):
            trigger = stripped.split(":", 1)[1].strip()
    if not description:
        description = default_description.format(name=name)
    if not trigger:
        trigger = default_trigger.format(name=name)
    display_path = str(path)
    try:
        home = Path.home().resolve()
        resolved = path.resolve()
        if resolved.is_relative_to(home):
            display_path = f"~/{resolved.relative_to(home)}"
    except ValueError:
        pass
    return CatalogFile(
        name=name,
        description=description,
        trigger=trigger,
        source_path=path,
        display_path=display_path,
        content=content,
    )


def _builtin_rules_dir() -> Path:
    return Path(str(resources.files("pollypm.defaults.rules")))


def _builtin_magic_dir() -> Path:
    return Path(str(resources.files("pollypm.defaults.magic")))


def _scan_catalog_dir(
    directory: Path,
    *,
    display_base: str | None = None,
    default_description: str,
    default_trigger: str,
) -> dict[str, CatalogFile]:
    if not directory.exists():
        return {}
    merged: dict[str, CatalogFile] = {}
    for path in sorted(directory.glob("*.md")):
        content = path.read_text()
        rule = _parse_catalog_metadata(
            path,
            content,
            default_description=default_description,
            default_trigger=default_trigger,
        )
        if display_base is not None:
            rule.display_path = f"{display_base}/{path.name}"
        merged[rule.name] = rule
    return merged


def discover_rules(project_root: Path) -> dict[str, CatalogFile]:
    merged = _scan_catalog_dir(
        _builtin_rules_dir(),
        display_base="pollypm/defaults/rules",
        default_description="Instructions for {name} work",
        default_trigger="When doing {name} work",
    )
    merged.update(
        _scan_catalog_dir(
            Path.home() / ".pollypm" / "rules",
            display_base="~/.pollypm/rules",
            default_description="Instructions for {name} work",
            default_trigger="When doing {name} work",
        )
    )
    merged.update(
        _scan_catalog_dir(
            project_root / ".pollypm" / "rules",
            display_base=".pollypm/rules",
            default_description="Instructions for {name} work",
            default_trigger="When doing {name} work",
        )
    )
    return merged


def discover_magic(project_root: Path) -> dict[str, CatalogFile]:
    merged = _scan_catalog_dir(
        _builtin_magic_dir(),
        display_base="pollypm/defaults/magic",
        default_description="Capability for {name}",
        default_trigger="When {name} would help",
    )
    merged.update(
        _scan_catalog_dir(
            Path.home() / ".pollypm" / "magic",
            display_base="~/.pollypm/magic",
            default_description="Capability for {name}",
            default_trigger="When {name} would help",
        )
    )
    merged.update(
        _scan_catalog_dir(
            project_root / ".pollypm" / "magic",
            display_base=".pollypm/magic",
            default_description="Capability for {name}",
            default_trigger="When {name} would help",
        )
    )
    return merged


def render_rules_manifest(project_root: Path) -> str:
    rules = discover_rules(project_root)
    if not rules:
        return ""
    lines = [
        "## Available Rules",
        "You have specialized instructions for these scenarios. Read the relevant file before starting that type of work.",
    ]
    for name in sorted(rules):
        rule = rules[name]
        lines.append(f"- {rule.name}: {rule.description} -> {rule.display_path} ({rule.trigger})")
    return "\n".join(lines)


def render_magic_manifest(project_root: Path) -> str:
    magic = discover_magic(project_root)
    if not magic:
        return ""
    lines = [
        "## Available Magic",
        "You have access to these capabilities. Use them when the situation calls for it.",
    ]
    for name in sorted(magic):
        entry = magic[name]
        lines.append(f"- {entry.name}: {entry.description} -> {entry.display_path} ({entry.trigger})")
    return "\n".join(lines)


def render_session_manifest(project_root: Path) -> str:
    parts = [render_rules_manifest(project_root), render_magic_manifest(project_root)]
    return "\n\n".join(part for part in parts if part)
