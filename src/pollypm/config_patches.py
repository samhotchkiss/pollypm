from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil

from pollypm.config import project_config_path
from pollypm.doc_scaffold import scaffold_docs
from pollypm.plugin_host import PLUGIN_MANIFEST
from pollypm.projects import ensure_project_scaffold
from pollypm.rules import discover_magic, discover_rules


@dataclass(slots=True)
class PreferencePatch:
    kind: str
    target_name: str
    path: Path
    offer_text: str


def detect_preference_patch(project_root: Path, text: str) -> PreferencePatch | None:
    lowered = text.strip().lower()
    if not lowered:
        return None
    if not any(
        marker in lowered
        for marker in ("i don't like", "i do not like", "stop doing", "stop using", "don't want", "do not want", "prefer")
    ):
        return None
    ensure_project_scaffold(project_root)
    if any(token in lowered for token in ("test", "tests", "commit", "pre-commit")):
        return PreferencePatch(
            kind="rule",
            target_name="build",
            path=project_root / ".pollypm" / "rules" / "build.md",
            offer_text="I can create a project-local override for the `build` rule in `.pollypm/rules/build.md` so the next session uses your preference.",
        )
    if any(token in lowered for token in ("magic", "capability")):
        return PreferencePatch(
            kind="magic",
            target_name="deploy-site",
            path=project_root / ".pollypm" / "magic" / "deploy-site.md",
            offer_text="I can create a project-local magic override in `.pollypm/magic/` so the next session uses that behavior.",
        )
    if any(token in lowered for token in ("plugin", "provider", "scheduler", "heartbeat backend")):
        return PreferencePatch(
            kind="plugin_selection",
            target_name="plugins",
            path=project_root / ".pollypm" / "config" / "plugins.toml",
            offer_text="I can write a project-local plugin selection override in `.pollypm/config/plugins.toml`.",
        )
    return PreferencePatch(
        kind="project_setting",
        target_name="project",
        path=project_config_path(project_root),
        offer_text="I can write that as a project-local override in `.pollypm/config/project.toml`.",
    )


def apply_preference_patch(project_root: Path, text: str) -> PreferencePatch:
    patch = detect_preference_patch(project_root, text)
    if patch is None:
        raise ValueError("No overridable preference detected.")
    ensure_project_scaffold(project_root)
    if patch.kind == "rule":
        _write_rule_override(project_root, patch.target_name, text)
        return patch
    if patch.kind == "magic":
        _write_magic_override(project_root, patch.target_name, text)
        return patch
    if patch.kind == "plugin_selection":
        patch.path.parent.mkdir(parents=True, exist_ok=True)
        patch.path.write_text(
            "[plugins]\n"
            f'preference = "{_escape(text)}"\n'
            'source = "project-local override"\n'
        )
        return patch
    patch.path.parent.mkdir(parents=True, exist_ok=True)
    patch.path.write_text(
        "[project]\n"
        f'preference_note = "{_escape(text)}"\n'
    )
    return patch


def list_project_overrides(project_root: Path) -> list[Path]:
    ensure_project_scaffold(project_root)
    results: list[Path] = []
    for relative in (
        ".pollypm/rules",
        ".pollypm/magic",
        ".pollypm/config",
        ".pollypm-state/plugins",
    ):
        root = project_root / relative
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file():
                results.append(path)
    return results


def _write_rule_override(project_root: Path, rule_name: str, preference: str) -> None:
    rules = discover_rules(project_root)
    source = rules.get(rule_name)
    if source is None:
        raise ValueError(f"Unknown rule: {rule_name}")
    target = project_root / ".pollypm" / "rules" / f"{rule_name}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    body = source.content.rstrip() + "\n\n## Project Override\n" f"- User preference: {preference.strip()}\n"
    if "test" in preference.lower() and "commit" in preference.lower():
        body += "- Do not require unit tests before every commit unless the change specifically needs them.\n"
        body += "- Preserve integration and user-visible verification before handoff.\n"
    target.write_text(body)


def _write_magic_override(project_root: Path, magic_name: str, preference: str) -> None:
    magic = discover_magic(project_root)
    source = magic.get(magic_name)
    if source is None:
        raise ValueError(f"Unknown magic: {magic_name}")
    target = project_root / ".pollypm" / "magic" / f"{magic_name}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source.content.rstrip() + "\n\n## Project Override\n" f"- User preference: {preference.strip()}\n")


def write_plugin_override(project_root: Path, plugin_name: str, body: str) -> Path:
    ensure_project_scaffold(project_root)
    plugin_dir = project_root / ".pollypm-state" / "plugins" / plugin_name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / PLUGIN_MANIFEST).write_text(
        "\n".join(
            [
                f'name = "{plugin_name}"',
                'api_version = "1"',
                'version = "0.1.0"',
                'kind = "override"',
                'entrypoint = "plugin.py:plugin"',
                'capabilities = []',
                'description = "Project-local plugin override"',
                "",
            ]
        )
    )
    (plugin_dir / "plugin.py").write_text(body)
    # Regenerate docs so reference material reflects the new plugin
    scaffold_docs(project_root, force=True)
    return plugin_dir


def remove_project_override(project_root: Path, kind: str, target_name: str) -> None:
    candidates = {
        ("rule", target_name): project_root / ".pollypm" / "rules" / f"{target_name}.md",
        ("magic", target_name): project_root / ".pollypm" / "magic" / f"{target_name}.md",
        ("project_setting", target_name): project_config_path(project_root),
        ("plugin_selection", target_name): project_root / ".pollypm" / "config" / "plugins.toml",
        ("plugin", target_name): project_root / ".pollypm-state" / "plugins" / target_name,
    }
    path = candidates.get((kind, target_name))
    if path is None:
        return
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("\r", " ").strip()
