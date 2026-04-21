from pollypm.cli_shortcuts import render_shortcuts_text, shortcut_rows


def test_shortcut_rows_are_stable_for_shared_surfaces() -> None:
    assert shortcut_rows() == (
        ("Create", "pm task create | pm issue create"),
        ("Monitor", "pm activity --follow | pm cockpit"),
        ("Review", "pm inbox | pm task approve"),
        ("Advanced", "pm advisor | pm briefing"),
    )


def test_render_shortcuts_text_uses_all_rows() -> None:
    rendered = render_shortcuts_text()

    assert rendered.startswith("PollyPM shortcuts")
    for label, commands in shortcut_rows():
        assert label in rendered
        assert commands in rendered
