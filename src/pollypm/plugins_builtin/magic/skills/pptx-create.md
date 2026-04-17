---
name: pptx-create
description: Generate PowerPoint .pptx decks with layouts, templates, charts, and consistent typography.
when_to_trigger:
  - presentation
  - slides
  - .pptx
  - powerpoint deck
kind: magic_skill
attribution: https://github.com/travisvn/awesome-claude-skills
---

# PPTX Create

## When to use

Use when the user needs a `.pptx` file they can open and edit in PowerPoint or Keynote. Reach for this when the audience is executive, stakeholder, or conference; when they want an HTML animated deck, use `frontend-slides` instead.

## Process

1. Start from a template (.pptx or .potx), not a blank presentation. Templates carry slide masters, color themes, and layouts that make every slide visually consistent. Default: the project's `templates/deck.pptx` if it exists; otherwise `python-pptx` default with the palette overridden to the brand tokens.
2. Define slide layouts up front: title, section-header, title+content, two-content, comparison, blank. Reference slides by layout index (`prs.slide_layouts[N]`) so the deck stays predictable.
3. One idea per slide. If a slide has >3 bullets, split it. If a bullet has >12 words, cut it. Long bullets are where decks die.
4. For charts, use `python-pptx`'s native `add_chart`. Never paste images of charts — the user cannot edit pasted images in PowerPoint. Exception: when the chart is an artistic infographic, then use `canvas-design` to render PNG and embed.
5. Typography: max 3 font sizes per deck (title, subhead, body). Titles 36-44pt, subheads 24pt, body 18pt minimum (lower than 18 = unreadable at projection distance).
6. Title slide + agenda slide + content slides + summary/next-steps slide. Do not ship a deck without a next-steps slide; that is the whole point.
7. Save as `.pptx` under the task artifact directory. Convert to PDF via LibreOffice headless if the user asked for print preview.

## Example invocation

```python
from pptx import Presentation
from pptx.util import Inches, Pt

prs = Presentation('templates/deck.pptx')

# Title slide
title_layout = prs.slide_layouts[0]
slide = prs.slides.add_slide(title_layout)
slide.shapes.title.text = 'Q1 Review'
slide.placeholders[1].text = 'Polly team | 2026-04-15'

# Content slide
content_layout = prs.slide_layouts[1]
slide = prs.slides.add_slide(content_layout)
slide.shapes.title.text = 'Three wins'
body = slide.placeholders[1].text_frame
body.text = '1. Shipped work service'
body.add_paragraph().text = '2. Landed plugin discovery'
body.add_paragraph().text = '3. Cut Tmux coupling by 80%'

prs.save('.pollypm/artifacts/task-47/q1-review.pptx')
```

## Outputs

- A `.pptx` file using the template's slide masters.
- Title, agenda, content, and next-steps slides at minimum.
- Native charts (editable), not pasted images.
- Optional PDF export via LibreOffice headless.

## Common failure modes

- Starting from blank instead of a template — every slide looks different.
- Pasting chart PNGs so the user cannot edit the data.
- Font size 12 body text that disappears at projection.
- Skipping the next-steps slide; decks without a CTA go nowhere.
