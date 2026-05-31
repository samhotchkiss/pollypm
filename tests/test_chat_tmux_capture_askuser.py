"""AskUserQuestion interactive-menu synthesis (M5 / #2476).

Fixture is built from a REAL live capture of Claude Code's AskUserQuestion
TUI form (the exact shape prior parser attempts mis-modeled): a top tab-bar
with ``✔ Submit``, a blank line, a multi-line WRAPPED prompt, then NUMBERED
options with indented descriptions, then numbered ``Type something.`` /
``Chat about this`` affordances, then the footer.
"""

from __future__ import annotations

from pollypm.web_api.chat.tmux_capture import (
    capture_envelopes,
    extract_ask_user_menu,
)

# Verbatim live capture (architect_savethenovel @ pollypm-storage-closet:8).
REAL_PANE = """\
  - The "plain share-button row" you mean is ShareLinks.astro on the Pledge page — four generic text buttons (Share on X / BlueSky / Facebook / Email) sitting under
  the beautifully-set book-plate. It works, but it's the one un-editorial thing on an otherwise bespoke page.

  Two decisions here are genuinely yours and they change the whole plan, so I want to lock them before I decompose — not guess and replan later.
──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
←  ☐ Imagery medium  ☐ Hero treatment  ✔ Submit  →

What medium should the imagery be? The original brief said 'Unsplash placeholders,' but the build pivoted to a bespoke SVG (book-stack.svg) that matches the palette —
and the existing IMAGE_NOTES literally warn against stock 'open book on rustic table' clichés.

❯ 1. Bespoke SVG illustration
     Extend the existing book-stack.svg direction: palette-matched illustrations + textured motifs. No stock clichés, no image pipeline, light for DreamHost shared
     hosting, fully on-brand. My recommendation — it's the most 'multi-channel delight' while protecting the editorial voice.
  2. Curated photography
     The original locked decision — real Unsplash photos (warm window light, book stacks, indie bookstore interiors) as annotated placeholders for S.E. to swap.
     Warmer and more literal, but carries stock-cliché risk, asset weight, manual optimization, and credit/licensing overhead.
  3. Mix of both
     Bespoke SVG for decorative/hero moments where tone matters most; curated photography only where a real place earns it (e.g. a bookstore interior). More surface
     area to build and maintain two pipelines.
  4. Type something.
──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
  5. Chat about this

Enter to select · Tab/Arrow keys to navigate · Esc to cancel
"""


def test_extract_real_askuser_menu_options():
    menu = extract_ask_user_menu(REAL_PANE)
    assert menu is not None
    q = menu["questions"][0]
    labels = [o["label"] for o in q["options"]]
    # Exactly the three real options — the numbered "Type something." / "Chat
    # about this" affordances must be excluded.
    assert labels == [
        "Bespoke SVG illustration",
        "Curated photography",
        "Mix of both",
    ]


def test_extract_real_askuser_headers_and_prompt():
    menu = extract_ask_user_menu(REAL_PANE)
    assert menu is not None
    # Both question tabs parsed from the top tab-bar; Submit dropped.
    assert menu["headers"] == ["Imagery medium", "Hero treatment"]
    assert menu["pending_questions"] == ["Hero treatment"]
    # Wrapped prompt joined into one line.
    q = menu["questions"][0]
    assert q["header"] == "Imagery medium"
    assert q["question"].startswith("What medium should the imagery be?")
    assert "clichés." in q["question"]
    assert "\n" not in q["question"]


def test_extract_real_askuser_recommended_and_selected():
    menu = extract_ask_user_menu(REAL_PANE)
    assert menu is not None
    opts = menu["questions"][0]["options"]
    rec = [o for o in opts if o["recommended"]]
    assert [o["label"] for o in rec] == ["Bespoke SVG illustration"]
    assert menu["selected_label"] == "Bespoke SVG illustration"
    # Descriptions captured (multi-line joined).
    assert "book-stack.svg" in opts[0]["description"]


def test_capture_synthesizes_one_ask_user_envelope_and_drops_chrome():
    class FakeTmux:
        def capture_pane(self, target, lines=3000):
            return REAL_PANE

    envs = capture_envelopes(FakeTmux(), session_name="architect_savethenovel", target="x")
    ask = [e for e in envs if e.type.value == "ask_user"]
    assert len(ask) == 1
    md = ask[0].metadata
    assert [o["label"] for o in md["questions"][0]["options"]] == [
        "Bespoke SVG illustration",
        "Curated photography",
        "Mix of both",
    ]
    # The menu chrome must NOT also appear in the text stream …
    text_blob = " ".join(e.text for e in envs if e.type.value == "text")
    for chrome in ("Enter to select", "Type something", "Chat about this", "✔ Submit", "❯ 1."):
        assert chrome not in text_blob, f"chrome leaked: {chrome!r}"
    # … but the conversation prose above the menu is kept.
    assert "ShareLinks" in text_blob


def test_askuser_negatives_return_none():
    assert extract_ask_user_menu("") is None
    assert extract_ask_user_menu("Hello.\nI finished the task.\n> ") is None
    # Prose that merely QUOTES the menu chrome in backticks is not a live menu.
    assert extract_ask_user_menu(
        "The pane shows `Enter to select ... Esc to cancel` and a `✔ Submit` bar."
    ) is None


def test_askuser_glyph_form_still_parses():
    # An alternate render using a glyph option list (☐) below the tab-bar
    # should still extract options.
    pane = """\
←  ☐ Pick one  ✔ Submit  →

Which colour?

❯ 1. Red
  2. Blue
  3. Type something.

Enter to select · Tab/Arrow keys to navigate · Esc to cancel
"""
    menu = extract_ask_user_menu(pane)
    assert menu is not None
    assert [o["label"] for o in menu["questions"][0]["options"]] == ["Red", "Blue"]
    assert menu["headers"] == ["Pick one"]
