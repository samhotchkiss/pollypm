"""Downtime management plugin — autonomous exploration during idle LLM budget.

See ``docs/downtime-plugin-spec.md``. Load-bearing principle: **nothing
produced in downtime ever auto-deploys**. Every exploration ends with an
inbox message awaiting explicit user approval.
"""
