from pathlib import Path

from pollypm.models import KnownProject, ProjectKind


def test_known_project_display_label_prefers_name_without_persona() -> None:
    # Persona is only shown in the PM Chat label, not the project label
    project = KnownProject(
        key="demo",
        path=Path("/tmp/demo"),
        name="Demo",
        persona_name="Dora",
        kind=ProjectKind.GIT,
    )

    assert project.display_label() == "Demo"


def test_known_project_display_label_falls_back_to_key() -> None:
    project = KnownProject(
        key="demo",
        path=Path("/tmp/demo"),
        kind=ProjectKind.FOLDER,
    )

    assert project.display_label() == "demo"
