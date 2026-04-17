---
name: xlsx-create
description: Generate Excel .xlsx workbooks with formulas, charts, formatted tables, and reproducible data analysis.
when_to_trigger:
  - spreadsheet
  - excel
  - csv to xlsx
  - xlsx generation
  - workbook
kind: magic_skill
attribution: https://github.com/travisvn/awesome-claude-skills
---

# XLSX Create

## When to use

Use when the user needs a real Excel workbook — formulas that recompute, charts that update when data changes, named ranges that business users can reference. Reach for `pdf-toolkit` if they just want a frozen report. XLSX is for live, editable data.

## Process

1. Pick the library: `openpyxl` for formulas + charts + formatting (most general), `xlsxwriter` when you need the richest chart options and write-only speed, `pandas.to_excel(engine='openpyxl')` when the input is a DataFrame.
2. Structure: one sheet per logical dataset, never mix unrelated data on one sheet. Add a `README` sheet as sheet 1 describing each subsequent sheet.
3. Use named ranges for anything referenced by formula: `workbook.defined_names['tax_rate'] = ...`. Formulas that reference magic cell addresses (`=B2*0.07`) are unmaintainable.
4. Tables: convert a data range into an Excel Table (`ws.tables.add`) so filters, banding, and structured references work. Tables beat raw ranges every time.
5. Formulas, not values, when the user will change inputs. Precomputed values defeat the point of XLSX.
6. Charts: add native `LineChart`, `BarChart`, or `PieChart`. Reference data via `Reference` objects so the chart tracks the source range even after edits.
7. Format numbers explicitly: `cell.number_format = '#,##0.00'` for currency, `'0.0%'` for percent, `'yyyy-mm-dd'` for dates. Default formatting lies about types.
8. Save to artifacts with `.xlsx` extension. Confirm round-trip by opening with `load_workbook` and checking one named range resolves.

## Example invocation

```python
from openpyxl import Workbook
from openpyxl.chart import LineChart, Reference
from openpyxl.workbook.defined_name import DefinedName

wb = Workbook()
ws = wb.active
ws.title = 'Sales'
ws.append(['Month', 'Revenue', 'Target'])
for m, r, t in [('Jan', 100, 90), ('Feb', 120, 100), ('Mar', 115, 110)]:
    ws.append([m, r, t])

# Named range
wb.defined_names['revenue_range'] = DefinedName('revenue_range', attr_text='Sales!$B$2:$B$4')

# Chart
chart = LineChart()
chart.title = 'Revenue vs Target'
data = Reference(ws, min_col=2, max_col=3, min_row=1, max_row=4)
chart.add_data(data, titles_from_data=True)
chart.x_axis.title = 'Month'
ws.add_chart(chart, 'E2')

wb.save('.pollypm/artifacts/task-47/sales.xlsx')
```

## Outputs

- An `.xlsx` workbook with one sheet per logical dataset.
- A `README` sheet at index 0 describing the others.
- Named ranges for anything a formula references.
- Native charts that track source data.

## Common failure modes

- Writing precomputed values when the user expects live formulas.
- Magic cell addresses in formulas instead of named ranges.
- Skipping `number_format` — dates render as serial numbers.
- Mixing four datasets on one sheet; every use case wants a separate tab.
