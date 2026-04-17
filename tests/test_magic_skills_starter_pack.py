"""Smoke test for the Magic v1 skills starter pack.

Guarantees every shipped skill file parses cleanly, has the required
frontmatter fields, carries a real body (not a stub), and that the set
has no name collisions. Also pins the expected file count — this grows
as categories ship and locks at 71 once Meta lands.
"""

from __future__ import annotations

from pathlib import Path

import pytest

try:  # Prefer PyYAML if available — mirrors the real loader path.
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - fallback for minimal envs
    yaml = None


SKILLS_DIR = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "pollypm"
    / "plugins_builtin"
    / "magic"
    / "skills"
)


# Category file counts as they ship. Update when each category lands.
# The final number locks at 71 when Meta is committed.
# Progress:
#  - Architecture & Visualization (10): 10
#  - Documents (8): 18
#  - Code Quality & Review (10): 28
#  - Testing & QA (6): 34
#  - Frontend / UI (8): 42
EXPECTED_COUNT = 42


REQUIRED_FIELDS = {"name", "description", "when_to_trigger", "kind"}


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Return (frontmatter_yaml, body) for a markdown file with YAML frontmatter."""
    if not text.startswith("---"):
        raise ValueError("missing frontmatter opening '---'")
    # Split on the second '---' fence.
    rest = text[3:]
    end = rest.find("\n---")
    if end == -1:
        raise ValueError("missing frontmatter closing '---'")
    fm = rest[:end].lstrip("\n")
    body = rest[end + len("\n---") :].lstrip("\n")
    return fm, body


def _parse_yaml(fm: str) -> dict:
    if yaml is not None:
        loaded = yaml.safe_load(fm)
        if not isinstance(loaded, dict):
            raise ValueError("frontmatter did not parse to a dict")
        return loaded
    # Minimal YAML parser — keys and simple list-of-strings only.
    data: dict = {}
    current_key: str | None = None
    for raw in fm.splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if line.startswith("  - ") or line.startswith("- "):
            if current_key is None:
                raise ValueError(f"list entry with no key: {line!r}")
            value = line.split("- ", 1)[1].strip()
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            data.setdefault(current_key, []).append(value)
            continue
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip()
            current_key = key
            if val == "":
                data[key] = []
            else:
                if val.startswith('"') and val.endswith('"'):
                    val = val[1:-1]
                data[key] = val
    return data


def _all_skill_files() -> list[Path]:
    assert SKILLS_DIR.exists(), f"skills directory missing: {SKILLS_DIR}"
    return sorted(p for p in SKILLS_DIR.glob("*.md") if p.is_file())


def test_skill_count_matches_expected() -> None:
    files = _all_skill_files()
    assert len(files) == EXPECTED_COUNT, (
        f"expected {EXPECTED_COUNT} skills, found {len(files)}"
    )


@pytest.mark.parametrize("path", _all_skill_files(), ids=lambda p: p.name)
def test_skill_parses_and_has_required_fields(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    fm, body = _split_frontmatter(text)
    data = _parse_yaml(fm)

    missing = REQUIRED_FIELDS - data.keys()
    assert not missing, f"{path.name}: missing frontmatter fields {missing}"

    assert data["kind"] == "magic_skill", (
        f"{path.name}: kind must be 'magic_skill', got {data['kind']!r}"
    )

    assert isinstance(data["when_to_trigger"], list) and data["when_to_trigger"], (
        f"{path.name}: when_to_trigger must be a non-empty list"
    )

    # Body sanity — must be real content, not a stub.
    body_lines = body.splitlines()
    assert len(body_lines) >= 30, (
        f"{path.name}: body only {len(body_lines)} lines; need >= 30"
    )

    # Slug consistency — the filename stem should match the `name`.
    assert data["name"] == path.stem, (
        f"{path.name}: frontmatter name {data['name']!r} != filename stem {path.stem!r}"
    )


def test_skill_names_are_unique() -> None:
    names: list[str] = []
    for path in _all_skill_files():
        text = path.read_text(encoding="utf-8")
        fm, _ = _split_frontmatter(text)
        data = _parse_yaml(fm)
        names.append(data["name"])
    duplicates = {n for n in names if names.count(n) > 1}
    assert not duplicates, f"duplicate skill names: {sorted(duplicates)}"
