from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path

_SESSION_MANIFEST_PATH = Path(".pollypm/MANIFEST.md")
_SESSION_SECTION_LIMIT = 6


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
    name_override: str | None = None,
) -> CatalogFile:
    name = name_override if name_override is not None else path.stem
    description = ""
    trigger = ""
    # Strip a YAML frontmatter block if present so that directory-style skills
    # (SKILL.md) can use ``description:`` inside the front matter.
    body_lines = content.splitlines()
    scan_lines: list[str]
    if body_lines and body_lines[0].strip() == "---":
        fm_end = None
        for idx in range(1, min(len(body_lines), 40)):
            if body_lines[idx].strip() == "---":
                fm_end = idx
                break
        if fm_end is not None:
            scan_lines = body_lines[1:fm_end] + body_lines[fm_end + 1 : fm_end + 21]
        else:
            scan_lines = body_lines[:20]
    else:
        scan_lines = body_lines[:20]
    for line in scan_lines:
        stripped = line.strip()
        lowered = stripped.casefold()
        if not description and lowered.startswith("description:"):
            description = stripped.split(":", 1)[1].strip()
        elif not trigger and lowered.startswith("trigger:"):
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
    # Single-file skills/rules: any top-level ``*.md`` (excluding private ones).
    for path in sorted(directory.glob("*.md")):
        if path.name.startswith("_"):
            continue
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
    # Directory-style skills: any subdirectory that contains a ``SKILL.md``.
    # The directory name becomes the skill name (not ``SKILL``), so that a
    # layout like ``visual-explainer/SKILL.md`` registers as ``visual-explainer``.
    for child in sorted(directory.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith(("_", ".")):
            continue
        skill_md = child / "SKILL.md"
        if not skill_md.is_file():
            continue
        content = skill_md.read_text()
        rule = _parse_catalog_metadata(
            skill_md,
            content,
            default_description=default_description,
            default_trigger=default_trigger,
            name_override=child.name,
        )
        if display_base is not None:
            rule.display_path = f"{display_base}/{child.name}/SKILL.md"
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


def _write_project_manifest(project_root: Path) -> None:
    full_manifest = "\n\n".join(
        part
        for part in (
            render_rules_manifest(project_root),
            render_magic_manifest(project_root),
        )
        if part
    )
    if not full_manifest:
        return
    try:
        dest = project_root / _SESSION_MANIFEST_PATH
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists() or dest.read_text() != full_manifest:
            dest.write_text(full_manifest)
    except Exception:  # noqa: BLE001
        return


def _render_compact_catalog(
    title: str,
    entries: dict[str, CatalogFile],
    *,
    intro: str,
) -> str:
    lines = [title, intro]
    names = sorted(entries)
    for name in names[:_SESSION_SECTION_LIMIT]:
        entry = entries[name]
        # Include display_path so both project-local overrides
        # (``.pollypm/rules/build.md``) and their builtin fallbacks
        # (``pollypm/defaults/rules/build.md``) are unambiguous to the
        # session. Workers need the exact file to read for any rule they
        # invoke.
        lines.append(
            f"- {entry.name}: {entry.description} ({entry.display_path})"
        )
    remaining = len(names) - _SESSION_SECTION_LIMIT
    if remaining > 0:
        lines.append(f"- … {remaining} more in `{_SESSION_MANIFEST_PATH.as_posix()}`")
    return "\n".join(lines)


def render_session_manifest(project_root: Path) -> str:
    _write_project_manifest(project_root)
    rules = discover_rules(project_root)
    magic = discover_magic(project_root)
    parts: list[str] = []
    if rules:
        parts.append(
            _render_compact_catalog(
                "## Available Rules",
                rules,
                intro=(
                    "Rules and available magic skills are summarized in "
                    f"`{_SESSION_MANIFEST_PATH.as_posix()}` (auto-regenerated). "
                    "Read it when you need one."
                ),
            )
        )
    if magic:
        parts.append(
            _render_compact_catalog(
                "## Available Magic",
                magic,
                intro=(
                    f"See `{_SESSION_MANIFEST_PATH.as_posix()}` for the full "
                    "catalog with paths and trigger details."
                ),
            )
        )
    return "\n\n".join(parts)
