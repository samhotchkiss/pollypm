"""Regression checks for the current plugin-document entry points."""

from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (PROJECT_ROOT / path).read_text(encoding="utf-8")


def test_current_plugin_docs_point_to_one_authoring_on_ramp():
    authoring = _read("docs/plugin-authoring.md")
    getting_started = _read("docs/getting-started.md")
    boundaries = _read("docs/plugin-boundaries.md")
    defaults_ref = _read("src/pollypm/defaults/docs/reference/plugins.md")
    ext_arch = _read("docs/extensibility-architecture.md")

    assert "If you are trying to build a plugin today, start here." in authoring
    assert "current starting point for authoring your own" in getting_started
    assert "Rail Plugin References" in boundaries
    assert "Rail Plugin Manifest (v1)" not in boundaries

    assert "~/.pollypm/plugins/*/" in defaults_ref
    assert "<project>/.pollypm/plugins/*/" in defaults_ref
    assert ".pollypm-state/plugins" not in defaults_ref
    assert "~/.config/pollypm/plugins/" not in defaults_ref
    assert "capabilities = [" not in defaults_ref

    assert "lightweight redirect for older links" in ext_arch
    assert "~/.config/pollypm/plugins/" in ext_arch
