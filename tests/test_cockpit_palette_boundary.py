"""Regression coverage for the cockpit palette import boundary (#1367)."""

from __future__ import annotations

import ast
from pathlib import Path

from textual.binding import Binding

from pollypm.cockpit_palette import (
    _resolve_palette_dispatch,
    _right_pane_help_section_for_cockpit,
)


def test_cockpit_palette_does_not_import_cockpit_ui() -> None:
    """The palette must not import the UI module that imports the palette."""
    root = Path(__file__).resolve().parents[1]
    source_path = root / "src" / "pollypm" / "cockpit_palette.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))

    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "pollypm.cockpit_ui":
                offenders.append(f"from {module} import ...")
            if module == "pollypm":
                offenders.extend(
                    f"from pollypm import {alias.name}"
                    for alias in node.names
                    if alias.name == "cockpit_ui"
                )
        elif isinstance(node, ast.Import):
            offenders.extend(
                alias.name
                for alias in node.names
                if alias.name == "pollypm.cockpit_ui"
            )

    assert offenders == []


def test_right_pane_help_uses_host_resolver() -> None:
    class _Pane:
        BINDINGS = [
            Binding("x", "demo", "Demo action"),
            Binding("question_mark", "help", "Help"),
        ]

    class _Host:
        def right_pane_help_target(self):
            return _Pane, "Demo pane"

    assert _right_pane_help_section_for_cockpit(_Host()) == (
        "Right pane: Demo pane",
        [("x", "Demo action")],
    )


def test_palette_dispatch_uses_host_resolver() -> None:
    seen: list[str | None] = []

    class _Host:
        def dispatch_palette_tag(self, tag: str | None) -> None:
            seen.append(tag)

    _resolve_palette_dispatch(_Host())("project.open")

    assert seen == ["project.open"]
