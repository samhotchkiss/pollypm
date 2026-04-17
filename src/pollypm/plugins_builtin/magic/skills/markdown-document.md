---
name: markdown-document
description: Produce well-structured markdown documents with TOC, sections, tables, and consistent heading hierarchy.
when_to_trigger:
  - writeup
  - documentation
  - spec doc
  - markdown doc
  - readme
kind: magic_skill
attribution: https://github.com/travisvn/awesome-claude-skills
---

# Markdown Document

## When to use

Use when the deliverable lives in a repo, wiki, or static site — any place markdown renders natively. This skill produces documents that are scannable via headings, correct on GitHub and MkDocs, and easy for reviewers to leave inline comments on. For Word-oriented output, use `docx-create`.

## Process

1. Lock the heading hierarchy before writing. One `#` H1 at the top (the title). H2 for top-level sections. H3 for subsections. Never skip levels. Never use `#####` — if you need H5, the doc needs restructuring.
2. Start with a TOC. Generate it via `markdown-toc`-style hand output or `mkdocs-material`'s `toc:` extension. Omit for docs <400 lines.
3. Front-load the summary. First paragraph answers: what is this, who should read it, what is the one thing to know. Readers scroll to decide whether to read more — earn the scroll.
4. Use tables for anything with 3+ parallel attributes. Markdown tables render on GitHub, GitLab, Obsidian, MkDocs. Never use HTML tables unless you need `rowspan`.
5. Code blocks always specify a language: `bash, `python, `tsx. Syntax highlighting depends on the fence language.
6. Images always have alt text. `![throughput chart](./throughput.png)` not `![](./throughput.png)`. Screen readers and empty-state fallbacks both need it.
7. Links: prefer relative paths inside a repo, absolute URLs outside. Never bare URLs; always `[descriptive text](url)`.
8. End with a "See also" or "Next steps" section. A doc with no outbound links is a dead end.

## Example invocation

```markdown
# Work Service Architecture

A sealed task-management layer that owns lifecycle, status, and artifact
storage. Plugins interact through `WorkService` — the SQLite backend is an
implementation detail.

## Contents

- [Contract](#contract)
- [Storage](#storage)
- [Migration path](#migration-path)

## Contract

The `WorkService` protocol exposes six methods: `create`, `list`, `get`,
`update`, `cancel`, and `archive`. See `src/pollypm/work/service.py` for
the canonical type signatures.

## Storage

| Backend  | When to use         | Notes                        |
| -------- | ------------------- | ---------------------------- |
| SQLite   | Default             | Zero-ops, single-node        |
| Postgres | Multi-node Polly    | Requires plugin `work_pg`    |

## See also

- [Plugin authoring](./plugin-authoring.md)
- [Memory system](./memory-system-review.md)
```

## Outputs

- A `.md` file with H1 title, TOC (if long), and H2/H3 hierarchy.
- Tables for parallel data, code fences with languages.
- All images carry alt text; all links have descriptive text.
- A closing "See also" or "Next steps" section.

## Common failure modes

- Skipping heading levels — H2 then H4 — breaks TOC generators.
- Plain-text URLs instead of `[label](url)`; renders as raw text.
- Code fences without languages; no syntax highlighting.
- No summary paragraph; reader bounces at the TOC.
