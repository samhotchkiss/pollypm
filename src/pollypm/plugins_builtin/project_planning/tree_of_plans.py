"""Tree-of-plans helpers for stages 2, 5, and 6 of plan_project.

Spec §3 stage 2: the architect emits 2-3 candidate decompositions, not
one. Each candidate is its own artifact (`candidate_A.md`,
`candidate_B.md`, `candidate_C.md`) written into the planning worktree.
Stage 5 critics evaluate **all** candidates and emit per-candidate
scores via the critique JSON. Stage 6 synthesis reads the critic JSONs,
picks the winner via per-critic consensus, and writes the rationale
into the planning session log.

This module owns three concerns:

1. Candidate identifiers and path resolution (``CandidateId``,
   ``candidate_artifact_path``). Capped at 3 per spec.
2. The decompose-stage prompt augmentation that spells out the
   tree-of-plans contract the architect must honour.
3. The synthesis algorithm: given a list of critic JSONs, return the
   winning candidate id + a structured rationale the architect folds
   into ``docs/project-plan.md`` and ``docs/planning-session-log.md``.

The algorithm is deliberately simple for v1: pick the candidate with
the highest average score across critics; ties broken by preferred-
candidate votes; remaining ties broken by alphabetic id. Book Ch 17
(Tree of Thoughts) only prescribes "branch + select"; the selection
heuristic is an implementation detail we can tune later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


MAX_CANDIDATES: int = 3
"""Spec cap — no more than 3 candidate decompositions per planning run."""

MIN_CANDIDATES: int = 2
"""Spec floor — at least 2 candidates so there's actually a choice."""

CANDIDATE_IDS: tuple[str, ...] = ("A", "B", "C")
"""Stable candidate identifiers in emission order."""


def candidate_artifact_path(project_root: str | Path, candidate_id: str) -> Path:
    """Return the path for candidate_<ID>.md under the planning worktree.

    Candidate ids are upper-case single letters (A, B, C). Raises
    ``ValueError`` for anything else so typos don't silently create
    rogue files.
    """
    if candidate_id not in CANDIDATE_IDS:
        raise ValueError(
            f"Candidate id must be one of {CANDIDATE_IDS}; got {candidate_id!r}"
        )
    return Path(project_root) / "docs" / "planning" / f"candidate_{candidate_id}.md"


def decompose_stage_prompt() -> str:
    """Instruction block for the architect at stage 2 (Decompose)."""
    return (
        "<decompose-stage>\n"
        "You are in Stage 2 (Decompose) of the PollyPM planning flow. Do "
        "NOT emit a single decomposition — emit 2 or 3 alternatives.\n\n"
        "## Tree-of-plans contract\n"
        "- Emit at least 2 and at most 3 candidate decompositions, labeled "
        "A, B, C.\n"
        "- Write each to `docs/planning/candidate_<ID>.md` at the project "
        "root. One file per candidate.\n"
        "- Every candidate stands alone: it must parse as a complete plan "
        "on its own terms. No cross-references between candidates.\n"
        "- Candidates should differ *meaningfully* — different seams, "
        "different plugin boundaries, different sequencing. Not the same "
        "plan with cosmetic rewording.\n\n"
        "## Required sections per candidate\n"
        "- `# Candidate <ID>: <short memorable name>`\n"
        "- `## Thesis` — one paragraph on why this decomposition fits.\n"
        "- `## Modules` — numbered list; each module has purpose, "
        "interface, estimated size, and rough dependencies.\n"
        "- `## Tradeoffs` — what this candidate gives up compared to the "
        "other(s). Be honest; the critic panel will read this.\n"
        "- `## Sequencing` — which module ships first and why.\n\n"
        "Stage 3 (Test strategy) and Stage 4 (Magic) will iterate across "
        "each candidate you emit. Stage 5's critics evaluate every "
        "candidate. Stage 6 picks the winner with explicit rationale. "
        "Emitting one candidate fails the stage.\n"
        "</decompose-stage>"
    )


def synthesis_stage_prompt() -> str:
    """Instruction block appended to the architect persona at stage 6.

    Ensures the architect knows to emit ``docs/downtime-backlog.md`` as
    part of synthesis output — the downtime plugin reads this file to
    source exploration candidates (see docs/downtime-plugin-spec.md §4
    / §8). The architect may also invoke the programmatic helper
    :func:`pollypm.plugins_builtin.project_planning.downtime_backlog.write_backlog`
    from a post-synthesis hook; this prompt is the persona-facing
    contract.
    """
    return (
        "<synthesis-stage>\n"
        "You are in Stage 6 (Synthesis) of the PollyPM planning flow. In "
        "addition to `docs/project-plan.md`, the Risk Ledger, and "
        "`docs/planning-session-log.md`, you must emit **one more** "
        "artifact:\n\n"
        "## `docs/downtime-backlog.md`\n"
        "A markdown table the downtime plugin reads during idle LLM "
        "budget windows. Columns — keep the header exact:\n\n"
        "    | title | kind | source | priority | description | why_deprioritized |\n\n"
        "What to include:\n"
        "- Every non-winning tree-of-plans candidate (kind=try_alt_approach).\n"
        "- Every magic-stage idea critics deprioritized but didn't reject "
        "outright (kind=spec_feature or build_speculative, per the "
        "idea's concreteness).\n"
        "- Any 'explore later' note you logged during the session.\n\n"
        "Rules:\n"
        "- `kind` must be one of: spec_feature, build_speculative, "
        "audit_docs, security_scan, try_alt_approach.\n"
        "- `source` is always `planner` for entries you write.\n"
        "- `priority` is 1–5 (5 highest).\n"
        "- Merge, don't overwrite: if a previous planner run already "
        "populated the file, preserve existing rows and append new ones "
        "dedup'd by title. The downtime plugin tolerates a missing "
        "file, so first-run writes are fine.\n"
        "</synthesis-stage>"
    )


def plan_review_stage_prompt() -> str:
    """Instruction block appended to the architect persona at stage 6.5.

    The ``plan_review`` node sits between ``synthesize`` and
    ``user_approval``. The architect (same role) re-reads the
    synthesized plan body plus every critic verdict, reflects, and
    rewrites the canonical plan document so that the decisions most
    likely to draw user pushback are surfaced at the top — not buried
    inside the body.

    The contract is deliberately silent on flag count. The architect
    is expected to emit *however many genuine flags exist*; that may
    be zero on a tightly-scoped brief, or ten-plus on an ambitious
    greenfield project. Forcing a fixed count would either pad
    nothing-burgers onto easy plans or truncate real surface area on
    hard ones — both fail the user.

    See #1399 for the acceptance criteria.
    """
    return (
        "<plan-review-stage>\n"
        "You are in Stage 6.5 (Plan review) of the PollyPM planning flow. "
        "The synthesize stage has already picked a winning candidate, "
        "folded critic objections into a Risk Ledger, and written "
        "`docs/project-plan.md`. Your job here is NOT to re-analyse — "
        "it is to REFLECT and to RESHAPE the canonical plan document "
        "so the user sees the load-bearing decisions before the body.\n\n"
        "## Inputs you MUST read before writing\n"
        "- `docs/project-plan.md` — the synthesized plan body.\n"
        "- Every `docs/planning/candidate_*.md` — so you remember which "
        "decompositions were live before synthesis picked the winner.\n"
        "- Every critic JSON output from the panel (the work-service "
        "executions on the critic_panel children) — including their "
        "`objections_for_risk_ledger` lists and `preferred_candidate` votes.\n"
        "- `docs/planning-session-log.md` — for the rationale narrative "
        "you wrote at synthesize.\n\n"
        "## Reflection lens\n"
        "Ask yourself, decision by decision: *if Sam reads this plan "
        "and pushes back, which call is he most likely to push back on?* "
        "Plan reviews fail when the architect buries a controversial "
        "judgment call (a plugin boundary that's premature, a data "
        "source that's product-wrong, a magic flourish that expands "
        "scope, a sequencing call that delays the user-visible move) "
        "inside three pages of decomposition. Surface those calls at "
        "the top so the user can spend their review budget on the few "
        "decisions that actually matter.\n\n"
        "## Output document structure (rewrite `docs/project-plan.md`)\n"
        "Replace the existing plan body with a single document that "
        "follows this exact section order:\n\n"
        "1. **Summary** — 1-2 sentences. What is being approved? Plain "
        "English; no node names, no jargon.\n"
        "2. **Judgment calls** — a flat list with however many genuine "
        "flags exist. Could be 0. Could be 10+. Do NOT invent flags to "
        "pad the section, and do NOT collapse two real flags into one "
        "to look terse. Each item is exactly:\n"
        "   - One line stating the decision (`Decision:` prefix is fine).\n"
        "   - One or two sentences on why this could go either way — "
        "what the alternative was, who argued for it, what the cost is "
        "if Sam disagrees with the call.\n"
        "   If there are zero genuine judgment calls, write a single "
        "sentence saying so and explain why (e.g. \"Brief was tightly "
        "scoped; every decision followed from the stated constraints.\"). "
        "Do NOT skip the section header.\n"
        "3. **Plan body** — the decomposition (modules with name, "
        "purpose, user-level test description, acceptance criteria, "
        "dependencies, magic note, estimated size), the test strategy, "
        "the magic list, and the sequencing — i.e. the same content "
        "synthesize wrote, preserved verbatim or lightly tightened. Do "
        "NOT lose modules during the rewrite.\n"
        "4. **Critic synthesis** — where critics disagreed and how you "
        "resolved each disagreement. One row per material disagreement: "
        "what was contested, who took which side, your call, why. The "
        "Risk Ledger from synthesize stays where it is (under the plan "
        "body); this section is the *meta-narrative* about the panel.\n\n"
        "## Constraints\n"
        "- Variable-length judgment calls list. The acceptance gate is "
        "*authentic flags only*; a list of three real flags beats a "
        "list of seven padded ones.\n"
        "- Hoist, don't duplicate. If a judgment call is also a Risk "
        "Ledger row, the top-of-doc flag is the short form (1-2 "
        "sentences); the Risk Ledger row stays the canonical entry.\n"
        "- The document you produce IS the canonical plan rendered to "
        "the user at user_approval. The visual-explainer skill will "
        "render this version, not the pre-review draft.\n"
        "- The `log_present` gate still applies — append a `Stage 6.5 "
        "plan review` entry to `docs/planning-session-log.md` "
        "narrating which judgment calls you flagged and why.\n\n"
        "Then call `pm task done <task_id> --actor architect --output "
        "...` to advance plan_review → user_approval.\n"
        "</plan-review-stage>"
    )


def critic_panel_prompt() -> str:
    """Instruction block appended to the critic persona on stage 5.

    Ensures every critic knows they are evaluating multiple candidates
    and must score each one. Individual critic personas carry their
    lens; this block carries the structural contract they share.
    """
    return (
        "<critic-panel-contract>\n"
        "You are one of 5 critics on the PollyPM planning panel. Read "
        "every `docs/planning/candidate_*.md` file the architect emitted.\n\n"
        "- Evaluate EVERY candidate, not just your favourite.\n"
        "- Emit per-candidate scores in your structured JSON output (1-10, "
        "higher = better along your lens).\n"
        "- Emit a single `preferred_candidate` id — which candidate you'd "
        "ship today, given your lens. Ties are allowed; say `tie:A,B`.\n"
        "- Emit `objections_for_risk_ledger` as a flat list — the architect "
        "folds these into the plan's Risk Ledger at stage 6.\n"
        "- Submit via `pm task done --output '<json>'`. The `output_present` "
        "gate blocks terminal until your JSON is non-empty.\n"
        "</critic-panel-contract>"
    )


@dataclass(slots=True)
class CriticVerdict:
    """Normalised view of one critic's JSON output.

    Critic personas emit a ``payload`` dict inside their Work Output
    artifact. ``from_payload`` pulls the fields we need for synthesis;
    anything extra stays in ``raw`` for the session log.
    """

    critic_name: str
    candidate_scores: dict[str, float]
    preferred_candidate: str | None
    objections: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, critic_name: str, payload: dict[str, Any]) -> "CriticVerdict":
        scores: dict[str, float] = {}
        for entry in payload.get("candidates", []) or []:
            cid = entry.get("id")
            score = entry.get("score")
            if cid in CANDIDATE_IDS and isinstance(score, (int, float)):
                scores[cid] = float(score)
        preferred = payload.get("preferred_candidate")
        if isinstance(preferred, str) and preferred not in CANDIDATE_IDS:
            # Allow ``tie:A,B`` form — for selection purposes, treat as no
            # single preference; the tiebreak falls through to scores.
            if not preferred.startswith("tie:"):
                preferred = None
        objections = [
            str(item)
            for item in (payload.get("objections_for_risk_ledger") or [])
            if str(item).strip()
        ]
        return cls(
            critic_name=critic_name,
            candidate_scores=scores,
            preferred_candidate=preferred,
            objections=objections,
            raw=payload,
        )


@dataclass(slots=True)
class SynthesisResult:
    """Outcome of the tree-of-plans synthesis step.

    ``winner`` — the selected candidate id (``A``, ``B``, or ``C``).
    ``average_scores`` — mean score per candidate across critics.
    ``preferred_votes`` — per-candidate count of ``preferred_candidate``
    votes (ignoring ties / unknowns).
    ``rationale`` — human-readable paragraph the architect drops into
    ``docs/planning-session-log.md`` explaining the pick.
    ``risk_ledger_seeds`` — concatenated ``objections`` across critics,
    ready for the architect to triage into the plan's Risk Ledger.
    """

    winner: str
    average_scores: dict[str, float]
    preferred_votes: dict[str, int]
    rationale: str
    risk_ledger_seeds: list[str]


def synthesize(verdicts: list[CriticVerdict]) -> SynthesisResult:
    """Select the winning candidate from a panel of critic verdicts.

    Algorithm:

    1. Compute mean score per candidate across all critics that scored
       that candidate. Candidates with no scores at all are excluded.
    2. Pick the candidate with the highest mean. On tie, use
       ``preferred_candidate`` vote count as the first tiebreak.
    3. On further tie, pick the alphabetically-first id (A before B).

    Raises ``ValueError`` if fewer than ``MIN_CANDIDATES`` candidates
    were evaluated by any critic — that means the architect produced
    only one decomposition and Stage 2 was violated.
    """
    if not verdicts:
        raise ValueError("synthesize() requires at least one critic verdict.")

    # Collect scores + votes.
    all_scores: dict[str, list[float]] = {}
    preferred_votes: dict[str, int] = {cid: 0 for cid in CANDIDATE_IDS}
    for v in verdicts:
        for cid, score in v.candidate_scores.items():
            all_scores.setdefault(cid, []).append(score)
        if v.preferred_candidate in CANDIDATE_IDS:
            preferred_votes[v.preferred_candidate] += 1

    if len(all_scores) < MIN_CANDIDATES:
        raise ValueError(
            f"Tree-of-plans synthesis requires at least {MIN_CANDIDATES} "
            f"candidates with critic scores; got {len(all_scores)}."
        )

    average_scores = {
        cid: round(sum(scores) / len(scores), 2)
        for cid, scores in all_scores.items()
    }

    # Primary ordering: highest mean score.
    # Secondary ordering: most preferred-candidate votes.
    # Tertiary ordering: alphabetic id.
    def rank_key(cid: str) -> tuple[float, int, str]:
        return (-average_scores[cid], -preferred_votes.get(cid, 0), cid)

    ordered = sorted(average_scores.keys(), key=rank_key)
    winner = ordered[0]

    # Build the narrative rationale.
    lines = [
        f"Selected candidate {winner} after tree-of-plans synthesis.",
        "",
        "Average scores:",
    ]
    for cid in sorted(average_scores.keys()):
        lines.append(f"- {cid}: {average_scores[cid]:.2f}")
    lines.append("")
    lines.append("Preferred-candidate votes:")
    for cid in sorted(preferred_votes.keys()):
        if preferred_votes[cid] > 0:
            lines.append(f"- {cid}: {preferred_votes[cid]}")
    if not any(preferred_votes.values()):
        lines.append("- (no critic expressed a single preference)")

    # Risk-ledger seeds.
    seeds: list[str] = []
    for v in verdicts:
        for item in v.objections:
            seeds.append(f"[{v.critic_name}] {item}")

    return SynthesisResult(
        winner=winner,
        average_scores=average_scores,
        preferred_votes=preferred_votes,
        rationale="\n".join(lines),
        risk_ledger_seeds=seeds,
    )
