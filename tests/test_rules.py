from pathlib import Path

from pollypm.rules import discover_magic, discover_rules, render_magic_manifest, render_rules_manifest


def test_discover_rules_includes_packaged_defaults(tmp_path: Path) -> None:
    rules = discover_rules(tmp_path)

    assert {"bugfix", "build", "audit"} <= set(rules)
    assert rules["bugfix"].description == "How to fix bugs thoroughly"


def test_discover_rules_respects_override_hierarchy(monkeypatch, tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    (fake_home / ".pollypm" / "rules").mkdir(parents=True)
    (fake_home / ".pollypm" / "rules" / "bugfix.md").write_text(
        "Description: User bugfix\nTrigger: when user wants custom bugfixing\n"
    )
    (tmp_path / ".pollypm" / "rules").mkdir(parents=True)
    (tmp_path / ".pollypm" / "rules" / "bugfix.md").write_text(
        "Description: Project bugfix\nTrigger: when project wants bugfixing\n"
    )
    (tmp_path / ".pollypm" / "rules" / "deploy.md").write_text(
        "Description: Deploy instructions\nTrigger: when deploying\n"
    )

    rules = discover_rules(tmp_path)

    assert rules["bugfix"].description == "Project bugfix"
    assert rules["bugfix"].display_path == ".pollypm/rules/bugfix.md"
    assert rules["deploy"].description == "Deploy instructions"


def test_rules_manifest_lists_merged_rules(monkeypatch, tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    (tmp_path / ".pollypm" / "rules").mkdir(parents=True)
    (tmp_path / ".pollypm" / "rules" / "audit.md").write_text(
        "Description: Project audit flow\nTrigger: when reviewing code in this project\n"
    )

    manifest = render_rules_manifest(tmp_path)

    assert "## Available Rules" in manifest
    assert "- audit: Project audit flow -> .pollypm/rules/audit.md (when reviewing code in this project)" in manifest
    assert "- build: How to build features that impress -> pollypm/defaults/rules/build.md" in manifest


def test_discover_magic_respects_override_hierarchy(monkeypatch, tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    (fake_home / ".pollypm" / "magic").mkdir(parents=True)
    (fake_home / ".pollypm" / "magic" / "deploy-site.md").write_text(
        "Description: User deploy path\nTrigger: when user deploy flow applies\n"
    )
    (tmp_path / ".pollypm" / "magic").mkdir(parents=True)
    (tmp_path / ".pollypm" / "magic" / "deploy-site.md").write_text(
        "Description: Project deploy path\nTrigger: when project deploy flow applies\n"
    )
    (tmp_path / ".pollypm" / "magic" / "screenshot-verify.md").write_text(
        "Description: Screenshot verification\nTrigger: when verifying UI visually\n"
    )

    magic = discover_magic(tmp_path)

    assert magic["deploy-site"].description == "Project deploy path"
    assert magic["deploy-site"].display_path == ".pollypm/magic/deploy-site.md"
    assert magic["screenshot-verify"].description == "Screenshot verification"


def test_magic_manifest_lists_merged_magic(monkeypatch, tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    (tmp_path / ".pollypm" / "magic").mkdir(parents=True)
    (tmp_path / ".pollypm" / "magic" / "screenshot-verify.md").write_text(
        "Description: Screenshot verification\nTrigger: when checking UI output visually\n"
    )

    manifest = render_magic_manifest(tmp_path)

    assert "## Available Magic" in manifest
    assert "- screenshot-verify: Screenshot verification -> .pollypm/magic/screenshot-verify.md (when checking UI output visually)" in manifest
    assert "- deploy-site: Put a site online quickly -> pollypm/defaults/magic/deploy-site.md" in manifest
