**Last Verified:** 2026-04-22

## Summary

`memory_curator` runs a daily pass over every known project's memory store and keeps recall sharp as entries accumulate. **No embeddings, no LLM calls, no per-type policy.** Four local rules:

1. **TTL sweep** — delete rows whose `ttl_at` is in the past.
2. **Dedup** — merge near-duplicate pairs (same `(scope, type)`, Jaccard similarity ≥ 0.8 on normalized keywords). Keep the higher-importance entry and append the loser's body as context.
3. **Episodic → pattern promotion** — when ≥ 3 episodic entries in the same project cluster at similarity ≥ 0.45, queue a pattern-candidate inbox entry for user approval.
4. **Importance decay** — entries older than 90 days and unread for 30 drop importance by 1 (floor 1).

Every action is appended to `.pollypm/memory-curator.jsonl` for audit. A daily inbox summary under `.pollypm/curator/<date>.md` surfaces non-trivial runs; quiet days stay quiet.

Touch this plugin when tuning thresholds, adding a new rule, or changing the summary layout. Do not add LLM-driven curation here — the point is deterministic, auditable heuristics.

## Core Contracts

Registered:

- `memory.curate` job handler (max_attempts=1, timeout=300s).
- Roster entry `@every 24h` → `memory.curate` with `dedupe_key="memory.curate"`.

Handler result shape:

```python
{
    "ok": True,
    "projects_scanned": int,
    "ttl_deleted": int,
    "duplicates_merged": int,
    "decayed": int,
    "promotion_candidates": int,
}
```

## File Structure

- `src/pollypm/plugins_builtin/memory_curator/plugin.py` — registration + handler.
- `src/pollypm/plugins_builtin/memory_curator/pollypm-plugin.toml` — manifest.
- `src/pollypm/memory_curator.py` — the actual curation logic + `build_inbox_summary`.
- `src/pollypm/knowledge_extract.py` — supplies `_all_project_roots(config)`.

## Implementation Details

- **Thresholds.**
  - `DEDUP_SIMILARITY_THRESHOLD = 0.8` — high, so routine boilerplate does not trigger a merge.
  - `EPISODIC_PROMOTION_MIN = 3`, `EPISODIC_PROMOTION_SIMILARITY = 0.45` — looser clustering since patterns can share surface keywords.
  - `DECAY_AGE_DAYS = 90`, `DECAY_UNREAD_DAYS = 30`, `DECAY_FLOOR = 1`.
- **Audit log.** `<project>/.pollypm/memory-curator.jsonl` — one JSON per action (type, ids touched, reason). Atomic append.
- **Inbox summary.** Written to `<project>/.pollypm/curator/<YYYY-MM-DD>.md`. The file-directory scan by the inbox reader picks it up without the plugin needing to know which inbox implementation is active.
- **Silent days.** `build_inbox_summary` returns an empty string when nothing happened. No summary = no file = no inbox noise.
- **Error isolation.** Per-project exceptions are logged with `project_root` context and do not abort the overall pass; the next project still runs.

## Related Docs

- [modules/memory.md](../modules/memory.md) — the recall system whose store this curates.
- [features/inbox-and-notify.md](../features/inbox-and-notify.md) — where the summary surfaces.
