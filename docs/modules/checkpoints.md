**Last Verified:** 2026-04-22

## Summary

Checkpoints are the recovery data PollyPM writes at key moments so a fresh provider CLI can pick up where the old one left off. The module implements a **three-tier** model:

- **Level 0 (mechanical)** — captured synchronously at checkpoint time. Transcript tail, git branch / status / diff stat, files changed, commands observed, test results, worktree path, provider / account, lease holder, snapshot hash.
- **Level 1 (AI summary)** — Haiku-generated objective, sub-step, work completed, blockers, unresolved questions, recommended next step, and confidence. Heuristic fallback if the LLM call fails.
- **Level 2 (deep)** — Haiku-generated progress percentage, approach assessment, drift analysis, risk factors, alternative approaches, cross-session context. Used by recovery prompts and review panels.

Checkpoints are stored under `<project>/.pollypm/artifacts/checkpoints/` as paired JSON + markdown summary files, with a row in the `checkpoints` state-DB table pointing at them. Completion checkpoints (`create_issue_completion_checkpoint`) are created at task / issue close for audit.

Touch this module when adding a new checkpoint trigger, a new field, or changing the LLM extraction path. Do not remove heuristic fallbacks — they keep recovery working when the LLM call fails.

## Core Contracts

```python
# src/pollypm/checkpoints.py
HAIKU_MODEL = "claude-3-5-haiku-latest"
TRANSCRIPT_CAP_CHARS = 16000   # ~4000 tokens

@dataclass(slots=True)
class CheckpointArtifact:
    json_path: Path
    summary_path: Path
    summary_text: str

@dataclass(slots=True)
class CheckpointData:
    # Metadata
    checkpoint_id: str
    session_name: str
    project: str
    role: str
    level: int                # 0, 1, or 2
    trigger: str
    created_at: str
    parent_checkpoint_id: str
    is_canonical: bool
    # Level 0
    transcript_tail: list[str]
    files_changed: list[str]
    git_branch: str
    git_status: str
    git_diff_stat: str
    commands_observed: list[str]
    test_results: dict[str, int]
    worktree_path: str
    provider: str
    account: str
    lease_holder: str
    snapshot_hash: str
    # Level 1
    objective: str
    sub_step: str
    work_completed: list[str]
    blockers: list[str]
    unresolved_questions: list[str]
    recommended_next_step: str
    confidence: str
    # Level 2
    progress_pct: int
    approach_assessment: str
    drift_analysis: str
    risk_factors: list[str]
    alternative_approaches: list[str]
    cross_session_context: str

def snapshot_hash(content: str) -> str: ...
def create_level0_checkpoint(...) -> CheckpointArtifact: ...
def create_level1_checkpoint(...) -> CheckpointArtifact: ...
def create_level2_checkpoint(...) -> CheckpointArtifact: ...
def create_issue_completion_checkpoint(...) -> CheckpointArtifact: ...
def load_canonical_checkpoint(...) -> CheckpointData | None: ...
def write_mechanical_checkpoint(...) -> CheckpointArtifact: ...
def record_checkpoint(...) -> None: ...
def has_meaningful_work(data: CheckpointData) -> bool: ...
```

## File Structure

- `src/pollypm/checkpoints.py` — the module.
- `src/pollypm/recovery_prompt.py` — renders a provider-specific recovery prompt from the canonical checkpoint.
- `src/pollypm/storage/state.py` — owns the `checkpoints` table.
- `tests/test_checkpoints.py`, `tests/test_checkpoint_levels.py` — pinned behavior.

## Implementation Details

- **Haiku only.** LLM-driven extraction uses `claude-3-5-haiku-latest` via `pollypm.llm_runner.run_haiku` / `run_haiku_json`. This is a cost decision — Haiku is cheap enough to run on every checkpoint.
- **Heuristic fallback.** `_extract_l1_heuristic` / `_extract_l2_heuristic` reconstruct a usable summary when the Haiku call fails (network, rate limit, parse error). Fallback outputs are plainly less insightful but still shape-valid.
- **Canonical chain.** A level-1 checkpoint is marked `is_canonical=True` and references its parent level-0 via `parent_checkpoint_id`. `load_canonical_checkpoint` walks the chain to return the most recent canonical snapshot a recovery prompt can consume.
- **Transcript cap.** Transcript tails are capped at `TRANSCRIPT_CAP_CHARS = 16000` (≈ 4K tokens). Longer tails are truncated before the LLM call.
- **`has_meaningful_work`.** Guards completion checkpoints so we don't persist a full checkpoint for a turn that did nothing.
- **Triggers.** Commonly triggered pre-failover, pre-stop, on recovery-pane-detection, and on task completion. Each trigger records its name in `CheckpointData.trigger`.

## Related Docs

- [modules/recovery.md](recovery.md) — consumes checkpoints for recovery-prompt assembly.
- [modules/state-store.md](state-store.md) — owns the `checkpoints` table.
- [modules/transcript-ingest.md](transcript-ingest.md) — feeds transcript tails.
