# 0025 History Import Superseded Facts And Extraction Test Hardening

## Goal

Track superseded project-history facts during LLM extraction, emit a `deprecated-facts.md` artifact for that history, and harden the extraction test surface so the behavior stays covered without brittle model-string assumptions.

## Acceptance Criteria

- `history_import.py` records superseded overview/list facts while `_extract_with_llm()` walks later chunks
- generated history-import docs include `deprecated-facts.md` when superseded facts exist
- unit coverage proves `_extract_with_llm()` captures deprecated facts from later chunk replacements
- unit coverage proves `generate_docs()` writes `deprecated-facts.md`
- knowledge-extraction integration coverage asserts stable semantic behavior and memory-entry side effects without relying on exact model phrasing
- full repo test suite passes after the changes

## Verification

- `pytest -q tests/test_history_import.py` passes
- `pytest -q tests/test_history_import.py tests/integration/test_knowledge_extract_integration.py` passes
- `pytest -q tests/test_scheduler_backend.py tests/e2e/test_full_lifecycle.py` passes
- `pytest -q` passes
