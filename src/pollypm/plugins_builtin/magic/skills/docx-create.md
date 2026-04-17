---
name: docx-create
description: Generate Word .docx documents with tracked changes, comments, styles, and table formatting preserved.
when_to_trigger:
  - word doc
  - .docx
  - tracked changes
  - generate word document
kind: magic_skill
attribution: https://github.com/travisvn/awesome-claude-skills
---

# DOCX Create

## When to use

Use when the user needs a `.docx` file — typically because they are collaborating with people who live in Word (legal, finance, stakeholders who do not use markdown). This skill produces the binary file with real styles, not a markdown-with-a-renamed-extension. Reach for `markdown-document` when the output is for engineers or a wiki.

## Process

1. Choose the library: `python-docx` for generation, `docx2txt` for extraction. Default to `python-docx` unless the user wants to modify an existing file.
2. Start from a style set, not a blank document. Define `Heading 1/2/3`, `Body`, `Code`, `Quote` upfront with font family (Calibri default, Georgia for legal tone), size, color, and spacing. Styles make edits cheap; inline formatting makes edits painful.
3. Build the document top-down: title page (if needed) -> table of contents placeholder -> body sections -> appendices. Use `document.add_heading(..., level=N)` so Word's built-in TOC generator works.
4. For tables, use `document.add_table(rows, cols)` with `table.style = 'Light Grid Accent 1'` — never hand-style with cell shading. For long tables, set `table.autofit = True`.
5. Tracked changes: use `python-docx-ng` or `docx-revisions` for insertions/deletions with author attribution. Plain `python-docx` does not emit revision marks.
6. Comments: add via `document.add_comment(runs=[run], text=..., author=...)` anchored to a specific run. Comments without anchors orphan.
7. Save to the task artifact directory with extension `.docx`. Never serve .doc (legacy binary format) unless the user insists.

## Example invocation

```python
from docx import Document

doc = Document()
# Styles first
styles = doc.styles
h1 = styles['Heading 1']
h1.font.name = 'Calibri'
h1.font.size = Pt(18)

doc.add_heading('Q1 Review', level=1)
doc.add_paragraph('Summary of the quarter.', style='Body')

table = doc.add_table(rows=1, cols=3)
table.style = 'Light Grid Accent 1'
hdr = table.rows[0].cells
hdr[0].text = 'Metric'; hdr[1].text = 'Target'; hdr[2].text = 'Actual'

doc.save('.pollypm/artifacts/task-47/q1-review.docx')
```

## Outputs

- A `.docx` file with real Word styles applied.
- TOC placeholder that Word can regenerate via Insert -> Table of Contents.
- Comments and tracked changes attributed to a named author.
- Saved under the task artifact directory.

## Common failure modes

- Hand-styling every paragraph instead of using named styles — edits cascade.
- Using base `python-docx` for tracked changes and getting plain text instead.
- Adding comments without run anchors — they orphan in Word.
- Saving as `.doc` (legacy binary) instead of `.docx` (Office Open XML).
