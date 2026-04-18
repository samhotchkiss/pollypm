"""Smoke tests for the vendored visual-explainer magic plugin.

Covers:
  1. The vendored ``SKILL.md`` exists and has a YAML frontmatter with
     ``name: visual-explainer``.
  2. The ``commands/`` directory ships the expected entrypoints (at minimum
     ``plan-review.md`` and ``generate-visual-plan.md``).
  3. ``discover_magic`` registers ``visual-explainer`` as a directory-style
     skill whose display path is the ``SKILL.md`` inside the directory.
  4. ``discover_magic`` still picks up the single-file skills
     (``itsalive`` and ``deploy-site``).
  5. The architect profile's synthesize/kickoff stage references the
     ``visual-explainer`` skill by name.

Run targeted:
    HOME=/tmp/pytest-agent-vexp uv run pytest \
        tests/test_magic_visual_explainer_install.py -q
"""

from __future__ import annotations

from pathlib import Path

from pollypm.rules import discover_magic


REPO_ROOT = Path(__file__).resolve().parent.parent
VEXP_DIR = (
    REPO_ROOT
    / "src"
    / "pollypm"
    / "defaults"
    / "magic"
    / "visual-explainer"
)
ARCHITECT_PROFILE = (
    REPO_ROOT
    / "src"
    / "pollypm"
    / "plugins_builtin"
    / "project_planning"
    / "profiles"
    / "architect.md"
)


def test_vendored_skill_md_exists_with_name_frontmatter() -> None:
    skill_md = VEXP_DIR / "SKILL.md"
    assert skill_md.is_file(), f"SKILL.md missing at {skill_md}"

    text = skill_md.read_text(encoding="utf-8")
    # Frontmatter MUST be the first thing in the file.
    assert text.startswith("---\n"), "SKILL.md does not start with YAML frontmatter"

    # Locate the closing fence within the first 40 lines.
    lines = text.splitlines()
    closing = None
    for idx in range(1, min(len(lines), 40)):
        if lines[idx].strip() == "---":
            closing = idx
            break
    assert closing is not None, "SKILL.md frontmatter is not closed within the first 40 lines"

    fm_block = "\n".join(lines[1:closing])
    assert "name: visual-explainer" in fm_block, (
        "SKILL.md frontmatter must declare name: visual-explainer"
    )


def test_vendored_commands_directory_has_expected_entries() -> None:
    commands_dir = VEXP_DIR / "commands"
    assert commands_dir.is_dir(), f"commands/ missing at {commands_dir}"

    required = {
        "plan-review.md",
        "generate-visual-plan.md",
    }
    have = {p.name for p in commands_dir.glob("*.md")}
    missing = required - have
    assert not missing, f"commands/ is missing required files: {missing} (have {sorted(have)})"


def test_discover_magic_picks_up_visual_explainer_as_directory_skill(tmp_path: Path) -> None:
    magic = discover_magic(tmp_path)

    assert "visual-explainer" in magic, (
        f"discover_magic did not register 'visual-explainer'; found {sorted(magic)}"
    )
    entry = magic["visual-explainer"]
    # Directory-style skill advertises its SKILL.md inside the directory, not a
    # bare ``visual-explainer.md`` at the parent level.
    assert entry.display_path == "pollypm/defaults/magic/visual-explainer/SKILL.md", (
        f"unexpected display_path: {entry.display_path!r}"
    )
    assert entry.source_path.name == "SKILL.md"
    assert entry.source_path.parent.name == "visual-explainer"
    # Sanity: description/trigger should be non-empty (fall back to defaults
    # if the frontmatter doesn't carry description: fields).
    assert entry.description
    assert entry.trigger


def test_discover_magic_still_picks_up_single_file_skills(tmp_path: Path) -> None:
    magic = discover_magic(tmp_path)

    for name in ("itsalive", "deploy-site"):
        assert name in magic, (
            f"discover_magic dropped single-file skill {name!r}; have {sorted(magic)}"
        )
        entry = magic[name]
        assert entry.display_path == f"pollypm/defaults/magic/{name}.md", (
            f"{name} display_path regressed: {entry.display_path!r}"
        )
        assert entry.source_path.name == f"{name}.md"


def test_architect_profile_references_visual_explainer_by_name() -> None:
    assert ARCHITECT_PROFILE.is_file(), f"architect profile missing at {ARCHITECT_PROFILE}"
    text = ARCHITECT_PROFILE.read_text(encoding="utf-8")

    # The architect must name the skill — not paraphrase it — so that Archie
    # invokes the vendored plugin rather than writing HTML from scratch.
    assert "visual-explainer" in text, (
        "architect profile does not reference the 'visual-explainer' skill by name"
    )
    # Must specifically reference one of the command entrypoints, so it's
    # clear which command Archie should invoke at synthesize.
    assert "plan-review" in text or "generate-visual-plan" in text, (
        "architect profile does not reference a visual-explainer command (plan-review or "
        "generate-visual-plan)"
    )
