---
name: pdf-toolkit
description: Extract, merge, split, and form-fill PDFs with the right library for each operation — no one-size-fits-all.
when_to_trigger:
  - pdf
  - extract from pdf
  - merge pdfs
  - fill pdf form
  - split pdf
kind: magic_skill
attribution: https://github.com/travisvn/awesome-claude-skills
---

# PDF Toolkit

## When to use

Use whenever the task touches a PDF — text extraction, page-range splitting, merging, form filling, or OCR of a scanned document. Each of these operations has a different right-tool answer; the skill picks the right one up front instead of reaching for whichever library was cached in memory.

## Process

1. Classify the input: text-native PDF, scanned image PDF, mixed, or fillable form. Use `pdfminer.six` detect — if page 1 has zero extractable text, treat as scanned.
2. Pick the library by operation:
   - **Extract text**: `pdfplumber` (preserves layout) > `pypdf` (fast, plain).
   - **Extract tables**: `pdfplumber.extract_tables()` first; `camelot` only if `pdfplumber` misses ruled tables.
   - **Merge / split / reorder pages**: `pypdf` (zero-dep, fast).
   - **Form fill**: `pypdf` with `update_page_form_field_values`. For flattening, `pypdf` + `reportlab` overlay.
   - **OCR**: `ocrmypdf` (wraps Tesseract) — produces a text-layer PDF from a scanned one.
   - **Render to image**: `pdf2image` (poppler-backed).
3. For extraction, never assume column order — always extract with layout coords and sort by `(top, left)` yourself if reading order matters.
4. For form fill, inspect fields first: `reader.get_form_text_fields()`. Match on field name, not position. Save with `AppendImagesHandler` to preserve signature fields.
5. After merge/split, always re-check page count: `len(PdfReader(out).pages)`. Silent off-by-one is the #1 bug.
6. For OCR on a scanned document, run `ocrmypdf --rotate-pages --deskew --force-ocr` so rotation and skew are normalized.
7. Save outputs to the task artifact directory with operation in the filename: `merged.pdf`, `pages-1-5.pdf`, `form-filled.pdf`.

## Example invocation

```python
# Merge two PDFs
from pypdf import PdfWriter
writer = PdfWriter()
for path in ['contract.pdf', 'appendix.pdf']:
    writer.append(path)
writer.write('.pollypm/artifacts/task-47/merged.pdf')

# Extract tables
import pdfplumber
with pdfplumber.open('report.pdf') as pdf:
    tables = []
    for page in pdf.pages:
        tables.extend(page.extract_tables())
```

## Outputs

- The operation's artifact(s) in the task artifact directory.
- A one-line report: "Merged 3 files (12 + 8 + 4 = 24 pages) -> merged.pdf (24 pages)."
- Fields extracted, tables found, or OCR text layer confirmed.

## Common failure modes

- Using `pypdf` for table extraction — misses ruled tables entirely.
- Assuming read order matches file order of text runs; always sort by position.
- Merging without post-count check and silently dropping pages.
- Running OCR on a text-native PDF — produces worse output than the source.
