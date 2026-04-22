**Last Verified:** 2026-04-22

## Summary

PollyPM keeps its own transcript archive because provider JSONL files drift, rotate, and sometimes vanish. The ingester tails each provider adapter's declared `TranscriptSource` roots, normalizes the events into a standardized JSONL shape, and appends them under `<project>/.pollypm/transcripts/`. Cursors per-file prevent re-ingesting already-seen bytes. A per-source scan cache keeps the tailer cheap — transcript roots with thousands of archived jsonls were pinning the rail daemon at ~170% CPU before the cache landed.

Touch this module when a provider changes its JSONL layout, when a new provider lands, or when changing the event normalization shape. Do not drop the cursor — the tailer is rate-limit-sensitive and must not re-scan.

## Core Contracts

```python
# src/pollypm/transcript_ingest.py
POLL_INTERVAL_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 30.0
FULL_RESCAN_SECONDS = 60.0
HOT_SCAN_WINDOW_SECONDS = 60.0 * 60.0
HOT_SCAN_FILE_LIMIT = 32

@dataclass(slots=True)
class TranscriptFileCursor:
    offset: int
    session_id: str | None
    cwd: str | None
    model_name: str | None
    mtime: float

@dataclass(slots=True)
class TranscriptCursorState:
    files: dict[str, TranscriptFileCursor]

@dataclass(slots=True)
class TranscriptSourceScanCache: ...

def sync_transcripts_once(config) -> None: ...

class TranscriptIngestor:
    def __init__(self, config) -> None: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...

def start_transcript_ingestion(config) -> TranscriptIngestor: ...
```

## File Structure

- `src/pollypm/transcript_ingest.py` — the ingester.
- `src/pollypm/transcript_ledger.py` — token-usage ledger populated from ingested events.
- `src/pollypm/provider_sdk.py` — `TranscriptSource` type providers declare.
- `src/pollypm/providers/claude/` + `providers/codex/` — per-provider source declarations + normalizers.
- `src/pollypm/plugins_builtin/core_recurring/plugin.py` — schedules `transcript.ingest` handler `@every 5m`.

## Implementation Details

- **Cursor model.** Each tracked file has a `TranscriptFileCursor` with `offset`, provider-specific metadata (`session_id`, `cwd`, `model_name`), and the file's `mtime` at end-of-last-scan. A file with unchanged mtime is skipped — open / seek / readline on thousands of files was the hot path.
- **Scan cache.** `TranscriptSourceScanCache` remembers the known file list and directory mtimes per root. The ingester only re-walks directories when the root mtime or a subdir mtime advanced (`_dir_snapshot_changed`).
- **Hot vs full scans.** `HOT_SCAN_WINDOW_SECONDS = 3600` and `HOT_SCAN_FILE_LIMIT = 32` — within the last hour, only the 32 most recently touched files are scanned for deltas. A full scan happens on `FULL_RESCAN_SECONDS = 60` boundaries.
- **Normalization.** `_normalize_claude_line` and `_normalize_codex_line` translate raw provider events into the archive shape: `{ "ts", "session_id", "provider", "account", "role", "text", "model", "tokens_in", "tokens_out", ...}`. Each row is appended atomically to `<project>/.pollypm/transcripts/<date>.jsonl`.
- **Token ledger.** `transcript_ledger.sync_token_ledger` walks the archive to roll up per-account token usage into the state DB for capacity tracking.
- **Timeout.** The `transcript.ingest` handler is registered with `timeout_seconds=600.0` (10 minutes) after the 2026-04-19 incident where first-cold-scan on a fresh install couldn't complete within the default 30s window.

## Related Docs

- [modules/providers.md](providers.md) — declares `TranscriptSource` lists.
- [modules/checkpoints.md](checkpoints.md) — consumes transcript tails.
- [modules/state-store.md](state-store.md) — token ledger tables.
