"""Type-aware memory extractors (M04 / #233).

Six focused extractors — one per ``MemoryType`` — replace the old
generic delta-extraction in :mod:`pollypm.knowledge_extract`. Each is a
Haiku call with a type-specific prompt that produces candidate memories
tagged with a confidence score. The coordinator filters on
``confidence >= 0.6`` and writes surviving candidates into the
:class:`FileMemoryBackend`.

Why this shape:

* **One prompt per type.** The old generic prompt asked Haiku to return
  six categories at once and inevitably drifted. A narrow prompt with a
  single purpose produces tighter output.
* **Confidence scoring in the prompt.** The model tells us how sure it
  is; a simple threshold keeps the low-signal candidates out of the
  store. No reviewer-agent needed for the happy path.
* **Idempotent.** The coordinator checks for an existing memory with
  the same (scope, type, salient field) and skips duplicates so
  re-running on the same events doesn't multiply entries.

Episodic memory is auto-written at session-end by a separate hook (see
issue spec §Out of scope) and is intentionally not produced by the
extractor coordinator here.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from pollypm.llm_runner import run_haiku_json
from pollypm.memory_backends import (
    EpisodicMemory,
    FeedbackMemory,
    MemoryBackend,
    MemoryType,
    PatternMemory,
    ProjectMemory,
    ReferenceMemory,
    TypedMemory,
    UserMemory,
)

logger = logging.getLogger(__name__)


# Minimum confidence score a candidate must carry to be persisted.
# Below this threshold the candidate is discarded silently (per spec).
CONFIDENCE_THRESHOLD = 0.6


@dataclass(slots=True)
class MemoryCandidate:
    """One candidate memory produced by a type-aware extractor.

    ``memory`` is the fully-formed typed-memory dataclass ready to be
    passed to :meth:`MemoryBackend.write_entry`. ``confidence`` is the
    model's self-reported certainty in ``[0, 1]``. The coordinator
    filters on ``confidence >= CONFIDENCE_THRESHOLD`` and ignores the
    rest.
    """

    memory: TypedMemory
    confidence: float

    def meets_threshold(self) -> bool:
        return float(self.confidence) >= CONFIDENCE_THRESHOLD


# ---------------------------------------------------------------------------
# Prompt builders — one per type
# ---------------------------------------------------------------------------


_COMMON_RULES = (
    "Return ONLY valid JSON (no markdown fences). "
    "Each candidate MUST include a numeric `confidence` in [0,1] reflecting "
    "how sure you are this is a real memory of the named type. "
    "If nothing of this type is present, return {\"candidates\": []}. "
    "Never include secrets, tokens, or passwords in any field."
)


def _events_as_json(events: list[dict[str, Any]], *, max_events: int = 200) -> str:
    return json.dumps(events[:max_events], indent=2)


def _user_prompt(events: list[dict[str, Any]]) -> str:
    return "\n".join(
        [
            "Extract USER memories from transcript events.",
            "A USER memory captures a stable fact about the operator: role,",
            "skill, or preference they reveal about themselves (not the project).",
            "Good examples: 'Senior engineer, prefers small modules',",
            "'Dislikes mocked tests, insists on real integration tests'.",
            "",
            "Return JSON with shape:",
            '{"candidates": [',
            '  {"name": "<short label>", "description": "<one-line summary>",',
            '   "body": "<paragraph>", "confidence": <0..1>}',
            "]}",
            "",
            _COMMON_RULES,
            "",
            "Events:",
            _events_as_json(events),
        ]
    )


def _feedback_prompt(events: list[dict[str, Any]]) -> str:
    return "\n".join(
        [
            "Extract FEEDBACK memories from transcript events.",
            "A FEEDBACK memory records a correction or notable confirmation",
            "from the operator — something the agent should remember as a",
            "rule for future behavior.",
            "Good example: rule='Never use --no-verify',",
            "why='Hooks catch test regressions before push',",
            "how_to_apply='If a pre-commit hook fails, fix the issue then re-commit'.",
            "",
            "Return JSON with shape:",
            '{"candidates": [',
            '  {"rule": "<rule>", "why": "<rationale>",',
            '   "how_to_apply": "<guidance>", "confidence": <0..1>}',
            "]}",
            "",
            _COMMON_RULES,
            "",
            "Events:",
            _events_as_json(events),
        ]
    )


def _project_prompt(events: list[dict[str, Any]]) -> str:
    return "\n".join(
        [
            "Extract PROJECT memories from transcript events.",
            "A PROJECT memory captures a decision, constraint, or",
            "non-derivable fact about this project.",
            "Good example: fact='Test runner is pytest with --tb=short',",
            "why='Agreed convention across the team',",
            "how_to_apply='Run uv run python -m pytest --tb=short -q before commit'.",
            "",
            "Return JSON with shape:",
            '{"candidates": [',
            '  {"fact": "<fact>", "why": "<rationale>",',
            '   "how_to_apply": "<guidance>", "confidence": <0..1>}',
            "]}",
            "",
            _COMMON_RULES,
            "",
            "Events:",
            _events_as_json(events),
        ]
    )


def _reference_prompt(events: list[dict[str, Any]]) -> str:
    return "\n".join(
        [
            "Extract REFERENCE memories from transcript events.",
            "A REFERENCE memory is a pointer to an external system: URL,",
            "issue tracker, dashboard, docs site, shared doc.",
            "Good example: pointer='https://github.com/foo/bar/issues',",
            "description='GitHub issues for the Foo project'.",
            "",
            "Return JSON with shape:",
            '{"candidates": [',
            '  {"pointer": "<url or reference>",',
            '   "description": "<one-line summary>", "confidence": <0..1>}',
            "]}",
            "",
            _COMMON_RULES,
            "",
            "Events:",
            _events_as_json(events),
        ]
    )


def _pattern_prompt(events: list[dict[str, Any]]) -> str:
    return "\n".join(
        [
            "Extract PATTERN memories from transcript events.",
            "A PATTERN memory is a 'when X, do Y' rule — a how-to pattern",
            "for this project.",
            "Good example: when='Cockpit crashes unexpectedly',",
            "then='Run pm down && pm up to reset the supervisor'.",
            "",
            "Return JSON with shape:",
            '{"candidates": [',
            '  {"when": "<condition>", "then": "<action>", "confidence": <0..1>}',
            "]}",
            "",
            _COMMON_RULES,
            "",
            "Events:",
            _events_as_json(events),
        ]
    )


# ---------------------------------------------------------------------------
# Individual extractors — each takes events + returns list of candidates
# ---------------------------------------------------------------------------


# Type alias for the LLM runner injection — keeps tests from needing to
# mock the real ``run_haiku_json``. When ``llm_runner`` is None the
# extractor returns an empty list (offline / no accounts available).
LLMRunner = Callable[[str], dict[str, Any] | None]


def _call_llm(prompt: str, llm_runner: LLMRunner | None) -> dict[str, Any] | None:
    runner = llm_runner or run_haiku_json
    try:
        return runner(prompt)
    except Exception as exc:  # noqa: BLE001 — defensive: LLM failures must not bubble
        logger.warning("Memory extractor Haiku call failed: %s", exc)
        return None


def _safe_candidate_list(response: dict[str, Any] | None) -> list[dict[str, Any]]:
    if response is None:
        return []
    raw = response.get("candidates")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _confidence(item: dict[str, Any]) -> float:
    try:
        return float(item.get("confidence", 0.0))
    except (TypeError, ValueError):
        return 0.0


def extract_user_memory(
    events: list[dict[str, Any]],
    *,
    scope: str = "operator",
    llm_runner: LLMRunner | None = None,
) -> list[MemoryCandidate]:
    """Produce USER memory candidates from ``events``.

    ``scope`` defaults to ``"operator"`` — user memories live under the
    operator tier so they follow the user across projects (see M03 tier
    model). Callers can override if they want a project-local user note.
    """
    if not events:
        return []
    response = _call_llm(_user_prompt(events), llm_runner)
    candidates: list[MemoryCandidate] = []
    for item in _safe_candidate_list(response):
        name = str(item.get("name") or "").strip()
        description = str(item.get("description") or "").strip()
        body = str(item.get("body") or "").strip()
        if not (name and description and body):
            continue
        candidates.append(
            MemoryCandidate(
                memory=UserMemory(
                    name=name,
                    description=description,
                    body=body,
                    scope=scope,
                    source="extractor",
                ),
                confidence=_confidence(item),
            )
        )
    return candidates


def extract_feedback_memory(
    events: list[dict[str, Any]],
    *,
    scope: str,
    llm_runner: LLMRunner | None = None,
) -> list[MemoryCandidate]:
    if not events:
        return []
    response = _call_llm(_feedback_prompt(events), llm_runner)
    candidates: list[MemoryCandidate] = []
    for item in _safe_candidate_list(response):
        rule = str(item.get("rule") or "").strip()
        why = str(item.get("why") or "").strip()
        how_to_apply = str(item.get("how_to_apply") or "").strip()
        if not (rule and why and how_to_apply):
            continue
        candidates.append(
            MemoryCandidate(
                memory=FeedbackMemory(
                    rule=rule,
                    why=why,
                    how_to_apply=how_to_apply,
                    scope=scope,
                    source="extractor",
                ),
                confidence=_confidence(item),
            )
        )
    return candidates


def extract_project_memory(
    events: list[dict[str, Any]],
    *,
    scope: str,
    llm_runner: LLMRunner | None = None,
) -> list[MemoryCandidate]:
    if not events:
        return []
    response = _call_llm(_project_prompt(events), llm_runner)
    candidates: list[MemoryCandidate] = []
    for item in _safe_candidate_list(response):
        fact = str(item.get("fact") or "").strip()
        why = str(item.get("why") or "").strip()
        how_to_apply = str(item.get("how_to_apply") or "").strip()
        if not (fact and why and how_to_apply):
            continue
        candidates.append(
            MemoryCandidate(
                memory=ProjectMemory(
                    fact=fact,
                    why=why,
                    how_to_apply=how_to_apply,
                    scope=scope,
                    source="extractor",
                ),
                confidence=_confidence(item),
            )
        )
    return candidates


def extract_reference_memory(
    events: list[dict[str, Any]],
    *,
    scope: str,
    llm_runner: LLMRunner | None = None,
) -> list[MemoryCandidate]:
    if not events:
        return []
    response = _call_llm(_reference_prompt(events), llm_runner)
    candidates: list[MemoryCandidate] = []
    for item in _safe_candidate_list(response):
        pointer = str(item.get("pointer") or "").strip()
        description = str(item.get("description") or "").strip()
        if not (pointer and description):
            continue
        candidates.append(
            MemoryCandidate(
                memory=ReferenceMemory(
                    pointer=pointer,
                    description=description,
                    scope=scope,
                    source="extractor",
                ),
                confidence=_confidence(item),
            )
        )
    return candidates


def extract_pattern_memory(
    events: list[dict[str, Any]],
    *,
    scope: str,
    llm_runner: LLMRunner | None = None,
) -> list[MemoryCandidate]:
    if not events:
        return []
    response = _call_llm(_pattern_prompt(events), llm_runner)
    candidates: list[MemoryCandidate] = []
    for item in _safe_candidate_list(response):
        when = str(item.get("when") or "").strip()
        then = str(item.get("then") or "").strip()
        if not (when and then):
            continue
        candidates.append(
            MemoryCandidate(
                memory=PatternMemory(
                    when=when,
                    then=then,
                    scope=scope,
                    source="extractor",
                ),
                confidence=_confidence(item),
            )
        )
    return candidates


# ---------------------------------------------------------------------------
# Episodic extractor — session-end auto-write path
# ---------------------------------------------------------------------------


def extract_episodic_memory(
    *,
    session_id: str,
    started_at: str,
    ended_at: str,
    summary: str,
    scope: str,
    importance: int = 2,
) -> MemoryCandidate | None:
    """Build an episodic candidate for session-end auto-write.

    Unlike the other extractors, episodic memories don't go through an
    LLM call — they capture an already-synthesized session summary
    (produced upstream by session_intelligence or checkpoint logic) and
    stamp it with the session timeline. The confidence is pinned to 1.0
    because we're not guessing; the session happened.

    Returns ``None`` when the summary is empty (no episodic memory for a
    dead / never-ran session).
    """
    cleaned = summary.strip()
    if not cleaned:
        return None
    return MemoryCandidate(
        memory=EpisodicMemory(
            summary=cleaned,
            session_id=session_id,
            started_at=started_at,
            ended_at=ended_at,
            scope=scope,
            importance=int(importance),
            source="extractor",
        ),
        confidence=1.0,
    )


# ---------------------------------------------------------------------------
# Coordinator — runs the five non-episodic extractors and writes survivors
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ExtractionResult:
    """Summary of a coordinator pass — useful for jobs and tests."""

    attempted: int = 0            # candidate count across all extractors
    filtered_low_confidence: int = 0
    duplicates_skipped: int = 0
    written: int = 0
    written_ids: list[int] = field(default_factory=list)


def run_extractors(
    events: list[dict[str, Any]],
    backend: MemoryBackend,
    *,
    project_scope: str,
    user_scope: str = "operator",
    llm_runner: LLMRunner | None = None,
    confidence_threshold: float = CONFIDENCE_THRESHOLD,
) -> ExtractionResult:
    """Run all five type-aware extractors, filter, dedupe, and write.

    * ``project_scope`` — scope for feedback / project / reference /
      pattern memories. Typically the project name.
    * ``user_scope`` — scope for user memories; defaults to
      ``"operator"`` so user preferences accumulate across projects.
    * ``llm_runner`` — dependency injection point for tests. Pass a
      callable that returns the parsed JSON response for each prompt.
    * ``confidence_threshold`` — override the default 0.6 floor.

    Idempotency: before writing, we consult
    :meth:`MemoryBackend.list_entries` filtered by scope+type and skip
    any candidate whose salient field already exists in the store.
    This is intentionally cheap (list per type, set lookup) rather than
    a fancy near-duplicate detector — curator handles that in M06.
    """
    result = ExtractionResult()
    if not events:
        return result

    all_candidates: list[MemoryCandidate] = []
    all_candidates.extend(extract_user_memory(events, scope=user_scope, llm_runner=llm_runner))
    all_candidates.extend(extract_feedback_memory(events, scope=project_scope, llm_runner=llm_runner))
    all_candidates.extend(extract_project_memory(events, scope=project_scope, llm_runner=llm_runner))
    all_candidates.extend(extract_reference_memory(events, scope=project_scope, llm_runner=llm_runner))
    all_candidates.extend(extract_pattern_memory(events, scope=project_scope, llm_runner=llm_runner))

    result.attempted = len(all_candidates)

    # Filter below-threshold candidates.
    accepted: list[MemoryCandidate] = []
    for candidate in all_candidates:
        if float(candidate.confidence) < float(confidence_threshold):
            result.filtered_low_confidence += 1
            continue
        accepted.append(candidate)

    # Idempotency — look up existing rows per (scope, type) combination
    # once, build a salient-field set, and skip duplicates.
    existing_by_key: dict[tuple[str, str], set[str]] = {}

    def _existing_set(scope: str, type_value: str) -> set[str]:
        key = (scope, type_value)
        if key not in existing_by_key:
            rows = backend.list_entries(scope=scope, type=type_value, limit=500)
            existing_by_key[key] = {
                _salient_field(row.title, row.body, type_value)
                for row in rows
            }
        return existing_by_key[key]

    for candidate in accepted:
        memory = candidate.memory
        scope = str(memory.scope or "project")
        type_value = memory.TYPE.value
        salient = _salient_for_candidate(memory)
        if salient in _existing_set(scope, type_value):
            result.duplicates_skipped += 1
            continue
        try:
            entry = backend.write_entry(memory)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to write %s memory: %s", type_value, exc)
            continue
        result.written += 1
        result.written_ids.append(entry.entry_id)
        existing_by_key[(scope, type_value)].add(salient)

    return result


# ---------------------------------------------------------------------------
# Salient-field helpers — extract the identifying field per type for
# duplicate detection. Kept as module-private helpers so the coordinator
# is the sole call site.
# ---------------------------------------------------------------------------


def _salient_for_candidate(memory: TypedMemory) -> str:
    if isinstance(memory, UserMemory):
        return _normalize(memory.name)
    if isinstance(memory, FeedbackMemory):
        return _normalize(memory.rule)
    if isinstance(memory, ProjectMemory):
        return _normalize(memory.fact)
    if isinstance(memory, ReferenceMemory):
        return _normalize(memory.pointer)
    if isinstance(memory, PatternMemory):
        return _normalize(memory.when)
    # Episodic memories never flow through the coordinator; use the
    # summary as the identifier if one ever does.
    return _normalize(getattr(memory, "summary", "") or getattr(memory, "name", ""))


def _salient_field(title: str, body: str, type_value: str) -> str:
    """Best-effort reconstruction of the salient field from a stored row.

    The file backend renders the salient value into the title (the first
    80-120 chars) for every type. We normalize the title for comparison;
    this is an approximation but good enough for the idempotency pass —
    curator's M06 dedup does the semantic version.
    """
    if type_value == MemoryType.USER.value:
        # UserMemory title is ``name`` (name is the first-choice title).
        return _normalize(title)
    if type_value == MemoryType.FEEDBACK.value:
        # FeedbackMemory title is truncated ``rule``.
        return _normalize(title)
    if type_value == MemoryType.PROJECT.value:
        return _normalize(title)
    if type_value == MemoryType.REFERENCE.value:
        # Title prefers description; fall back to pointer. Both fields
        # land in the body — scrape from there when possible.
        for line in body.splitlines():
            if line.startswith("**Pointer:**"):
                return _normalize(line.split(":", 1)[1])
        return _normalize(title)
    if type_value == MemoryType.PATTERN.value:
        # PatternMemory title is ``When: <when>``; strip prefix for
        # stable comparison with new candidates.
        stripped = title
        if stripped.lower().startswith("when:"):
            stripped = stripped.split(":", 1)[1]
        return _normalize(stripped)
    return _normalize(title)


def _normalize(text: str) -> str:
    """Case/whitespace-fold for duplicate detection."""
    return " ".join((text or "").lower().split())
