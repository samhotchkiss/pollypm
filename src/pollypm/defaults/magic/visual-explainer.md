Description: Create beautiful visual reports and documentation
Trigger: when creating reports, analysis, or documentation that the user will review

# Visual Explainer

## What It Does
Creates polished, beautiful HTML reports with diagrams, tables, charts, and professional formatting. These are deliverables the user opens in their browser — not text dumped into chat.

## When To Use It
- Any time the user asks for analysis, reports, competitive research, or documentation
- When comparing options or presenting recommendations
- When explaining architecture, workflows, or system design
- When the deliverable needs to look professional and be shareable

## How To Create It

1. Create an HTML file with embedded CSS for professional styling:
   ```
   <project>/reports/<descriptive-name>.html
   ```

2. Include:
   - Clean typography (system font stack, proper spacing)
   - A table of contents for long reports
   - Data tables with alternating row colors
   - Mermaid.js diagrams for flows and architecture (include the CDN script)
   - Executive summary at the top
   - Clear section headings
   - Actionable recommendations highlighted

3. After creating the file, send an inbox message:
   ```
   pm notify "Report: <title>" "Your report is ready at <path>. Open in a browser to review. Key findings: <1-2 sentence summary>"
   ```

## Template Structure

```html
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Report Title</title>
  <script src="https://cdn.jsdelivr.net/npm/mermaid/dist/mermaid.min.js"></script>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 900px; margin: 0 auto; padding: 2rem; color: #1a1a2e; background: #f8f9fa; }
    h1 { color: #16213e; border-bottom: 3px solid #0f3460; padding-bottom: 0.5rem; }
    h2 { color: #0f3460; margin-top: 2rem; }
    .summary { background: #e8f4f8; padding: 1.5rem; border-radius: 8px; border-left: 4px solid #0f3460; margin: 1.5rem 0; }
    .recommendation { background: #f0fff4; padding: 1rem; border-radius: 8px; border-left: 4px solid #38a169; margin: 1rem 0; }
    .warning { background: #fffbeb; padding: 1rem; border-radius: 8px; border-left: 4px solid #d69e2e; margin: 1rem 0; }
    table { width: 100%; border-collapse: collapse; margin: 1rem 0; }
    th { background: #0f3460; color: white; padding: 0.75rem; text-align: left; }
    td { padding: 0.75rem; border-bottom: 1px solid #e2e8f0; }
    tr:nth-child(even) { background: #f7fafc; }
    .mermaid { text-align: center; margin: 1.5rem 0; }
  </style>
</head>
<body>
  <h1>Report Title</h1>
  <div class="summary"><strong>Executive Summary:</strong> Key findings here.</div>
  <!-- Content -->
  <script>mermaid.initialize({startOnLoad: true});</script>
</body>
</html>
```

## Quality Bar
The report should look like something from a top consulting firm. The user should open it and think "this is professional." Not a markdown dump — a real deliverable.
