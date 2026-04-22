"""Worker turn-end auto-reprompt — #302.

Workers are never supposed to require a user touch: every turn must end
with a task state flip (done / bounce / notify). When #296's drift
detector catches a worker that ended its turn without advancing the
task, this module decides what to do next:

1. If the worker's recent transcript carries blocker/question language
   ("unclear", "waiting for", "need decision", etc.) OR an unfulfilled
   ``pm notify`` attempt, we treat the turn-end as an implicit
   escalation and create a ``blocking_question`` inbox item targeted
   at the project's PM. The worker stays parked — the PM is the one
   who unblocks.

2. Otherwise we assume the worker stalled without asking and send a
   standard reprompt via ``pm send --force``, reminding it that turns
   must always flip task state.

One path or the other — never both. The decision is a simple keyword
heuristic; an LLM assist is a v2 concern (Sam's call). Non-worker
sessions (PM / architect / reviewer) skip this module entirely; they
fall back to the existing log + alert behaviour from #296.

Keep this module pure-ish: the ``determine_worker_response`` function
is a pure classifier over a transcript string, and the two apply
helpers accept already-resolved services (work_service,
session_service, state_store) so tests can wire fakes without
bootstrapping a real runtime.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Tail size the heuristic scans for blocker language. ~2000 characters
# gives us plenty of surface without pulling the whole transcript —
# workers that stall with a question almost always put it near the end.
_TRANSCRIPT_TAIL_CHARS = 2000

# Maximum number of characters a blocking_question excerpt carries into
# the inbox body. Long enough to preserve a full paragraph, short
# enough that the PM can skim it without scroll fatigue.
_EXCERPT_MAX_CHARS = 800

# Subject truncation for the inbox task title — work_service titles are
# free-form but a shorter header reads better in the inbox list.
_TITLE_EXCERPT_CHARS = 80

# Phrases that mark the worker as blocked or asking a question. Match
# is case-insensitive substring; any single hit triggers the
# blocking_question classification. Keep the list tight — broad
# phrasing catches too much normal completion chatter.
_BLOCKER_PHRASES: tuple[str, ...] = (
    "can't proceed",
    "cannot proceed",
    "waiting for",
    "unclear",
    "blocking",
    "question:",
    "need clarification",
    "need approval",
    "need decision",
    "not sure how",
    "stuck on",
)


# Reprompt text sent to the worker when no blocker language is detected.
# Sam dictated this copy verbatim; do not reflow without his sign-off.
WORKER_REPROMPT_TEXT = (
    "Your turn ended without transitioning the task. Workers must "
    "always end with a state flip.\n"
    "\n"
    "If you have a blocking question that only the PM can answer, "
    "ask them:\n"
    "  pm notify --to pm '<task_id>: <your question here>' "
    "--priority immediate\n"
    "\n"
    "If you can work this through to completion, do so:\n"
    "  pm task done <task_id> --actor worker --output '<summary>'\n"
    "\n"
    "If you can't complete it but don't have a specific blocker "
    "question, make your best attempt and proceed. We can iterate via "
    "review."
)


@dataclass(slots=True, frozen=True)
class WorkerResponse:
    """Classification of a worker's turn-end.

    ``kind`` — ``"blocking_question"`` when blocker language is
    detected, ``"reprompt"`` otherwise.

    ``question_excerpt`` — the sanitized tail of the transcript the
    PM will see in the blocking_question inbox item. Empty for the
    reprompt path.
    """

    kind: str
    question_excerpt: str = ""


def _normalize_transcript(transcript: str) -> str:
    """Strip ANSI escape codes and control junk from a tmux capture.

    The tail window already limits size; this pass only removes the
    bytes that would make an excerpt unreadable in a web UI. Leaves
    newlines and normal punctuation alone.
    """
    if not transcript:
        return ""
    # Drop ANSI CSI escapes (colors, cursor moves) — they're common in
    # captured panes and make excerpts unreadable outside a terminal.
    text = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", transcript)
    # Drop other C0 control bytes except tab + newline.
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)
    return text


def _transcript_tail(transcript: str) -> str:
    """Return the last ``_TRANSCRIPT_TAIL_CHARS`` of the transcript."""
    if not transcript:
        return ""
    if len(transcript) <= _TRANSCRIPT_TAIL_CHARS:
        return transcript
    return transcript[-_TRANSCRIPT_TAIL_CHARS:]


def _unfulfilled_pm_notify(tail: str, work_service: Any) -> bool:
    """True when the tail mentions ``pm notify`` / ``pm task block``
    but there's no record the notify actually succeeded.

    Best-effort: in v1 we simply check for the command-shaped text in
    the tail. If we see the intent and no corresponding
    task-assignment notification row landed in the unified messages
    ledger, we treat the turn-end as an escalation attempt that the
    agent gave up on mid-flight. ``work_service`` is unused in v1 but
    the parameter stays so a v2 stricter match can correlate on
    subject/body without changing callers.
    """
    _ = work_service
    lower = tail.lower()
    if "pm notify" in lower or "pm task block" in lower:
        return True
    return False


def determine_worker_response(
    task: Any,
    session: Any,
    work_service: Any,
    transcript: str | None,
) -> WorkerResponse:
    """Classify a worker's turn-end as blocking_question or reprompt.

    Heuristic (v1):

    * Scan the last 2000 chars of the transcript for any phrase in
      ``_BLOCKER_PHRASES`` → blocking_question.
    * Otherwise, if the tail shows a ``pm notify`` / ``pm task block``
      intent that has no matching task-assignment notification row →
      blocking_question.
    * Otherwise → reprompt.

    The ``task`` and ``session`` arguments are here so a v2 can
    refine the classification using task metadata (flow kind, node
    position) or session signals (idle time). The v1 heuristic only
    reads the transcript.
    """
    _ = task, session  # reserved for v2 signals
    tail = _transcript_tail(_normalize_transcript(transcript or ""))
    if not tail:
        return WorkerResponse(kind="reprompt")

    lower = tail.lower()
    for phrase in _BLOCKER_PHRASES:
        if phrase in lower:
            return WorkerResponse(
                kind="blocking_question",
                question_excerpt=_extract_excerpt(tail, phrase),
            )
    if _unfulfilled_pm_notify(tail, work_service):
        return WorkerResponse(
            kind="blocking_question",
            question_excerpt=_extract_excerpt(tail, "pm notify"),
        )
    return WorkerResponse(kind="reprompt")


def _extract_excerpt(tail: str, anchor: str) -> str:
    """Pull a human-readable excerpt around ``anchor`` out of ``tail``.

    Finds the first occurrence (case-insensitive), walks back to the
    previous paragraph break (or 300 chars), and returns up to
    ``_EXCERPT_MAX_CHARS`` of context.
    """
    lower_anchor = anchor.lower()
    lower_tail = tail.lower()
    idx = lower_tail.find(lower_anchor)
    if idx < 0:
        # Anchor not found — return the final paragraph of the tail.
        return tail.strip()[-_EXCERPT_MAX_CHARS:]
    # Walk back to the nearest blank-line boundary, capped at 300 chars.
    window_start = max(0, idx - 300)
    segment = tail[window_start:]
    # Prefer to start at the beginning of a paragraph for readability.
    lines = segment.split("\n")
    # Drop leading junk lines (prompts like "╭── 2m ──╮" etc.).
    while lines and not lines[0].strip():
        lines.pop(0)
    excerpt = "\n".join(lines).strip()
    if len(excerpt) > _EXCERPT_MAX_CHARS:
        excerpt = excerpt[:_EXCERPT_MAX_CHARS].rstrip() + "\u2026"
    return excerpt


# ---------------------------------------------------------------------------
# Apply helpers — side-effectful, but the services are injected so tests
# can wire fakes without booting a real runtime.
# ---------------------------------------------------------------------------


def _resolve_pm_actor(task: Any, config: Any) -> str:
    """Return the persona name the blocking_question should be targeted at.

    Looks up ``config.projects[<key>].persona_name`` first; falls back
    to ``"polly"`` when nothing is configured. Case is preserved
    because downstream session resolution is case-sensitive for some
    session names.
    """
    project_key = getattr(task, "project", "") or ""
    if not project_key or config is None:
        return "polly"
    try:
        projects = getattr(config, "projects", {}) or {}
        project = projects.get(project_key)
    except Exception:  # noqa: BLE001
        return "polly"
    if project is None:
        return "polly"
    persona = getattr(project, "persona_name", None)
    if isinstance(persona, str) and persona.strip():
        return persona.strip()
    return "polly"


def create_blocking_question_inbox_item(
    task: Any,
    session_name: str,
    question_excerpt: str,
    work_service: Any,
    *,
    config: Any = None,
    state_store: Any = None,
    msg_store: Any = None,
) -> Any:
    """Create a ``blocking_question`` inbox task addressed at the PM.

    Returns the created :class:`Task` (or ``None`` on any failure —
    this helper is best-effort so a flaky work-service never crashes
    the sweep).

    Mirrors the label shape from the plan_review flow (#297): a
    canonical top-level label identifies the kind, sidecar labels
    carry structured metadata so the inbox UI can branch without
    parsing the body.
    """
    if work_service is None or task is None:
        return None
    task_id = getattr(task, "task_id", "") or ""
    project_key = getattr(task, "project", "") or ""
    pm_actor = _resolve_pm_actor(task, config)

    title_excerpt = (question_excerpt or "").strip().replace("\n", " ")
    if len(title_excerpt) > _TITLE_EXCERPT_CHARS:
        title_excerpt = (
            title_excerpt[: _TITLE_EXCERPT_CHARS - 1] + "\u2026"
        )
    title = f"Blocking question from worker ({task_id}): {title_excerpt}"

    body_parts = [
        f"Worker session **{session_name}** ended its turn without "
        f"transitioning task **{task_id}** and appears to be blocked.",
        "",
        "## Excerpt from worker transcript",
        "",
        question_excerpt.strip() or "(no excerpt available)",
        "",
        "## How to resolve",
        "",
        "- Reply in the inbox (r) — your reply is sent to the worker "
        "via `pm send --force` so the worker can resume.",
        f"- Or jump to the worker directly (d) to converse in its "
        f"session ({session_name}).",
        "- Archive (a) once the blocker is resolved.",
        "",
        f"Worker task: `pm task get {task_id}`",
    ]
    body = "\n".join(body_parts)

    labels = [
        "blocking_question",
        f"project:{project_key}" if project_key else "project:inbox",
        f"task:{task_id}" if task_id else "",
        f"blocking_worker:{session_name}",
    ]
    labels = [label for label in labels if label]

    try:
        inbox_task = work_service.create(
            title=title,
            description=body,
            type="task",
            project=project_key or "inbox",
            flow_template="chat",
            roles={"requester": session_name, "operator": pm_actor},
            priority="normal",
            created_by=session_name,
            labels=labels,
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "worker_turn_end: failed to create blocking_question for %s",
            task_id,
        )
        return None

    # Emit a ledger event so the activity feed / audit trail sees the
    # blocking_question creation even if the inbox UI doesn't render
    # it right away. #349: the audit row lives on the unified ``messages``
    # table when a Store is threaded through; fall back to the legacy
    # ``StateStore.record_event`` for callers that haven't migrated yet.
    message = (
        f"worker {session_name} blocked on {task_id} — "
        f"created inbox item {inbox_task.task_id} for "
        f"{pm_actor}"
    )
    if msg_store is not None:
        try:
            msg_store.append_event(
                scope=session_name,
                sender=session_name,
                subject="inbox.blocking_question.created",
                payload={
                    "message": message,
                    "task_id": task_id,
                    "inbox_task_id": getattr(inbox_task, "task_id", ""),
                    "pm_actor": pm_actor,
                },
            )
        except Exception:  # noqa: BLE001
            pass
    elif state_store is not None:
        try:
            state_store.record_event(
                session_name,
                "inbox.blocking_question.created",
                message,
            )
        except Exception:  # noqa: BLE001
            pass
    return inbox_task


def send_standard_reprompt(
    session_name: str,
    task: Any,
    session_service: Any,
    *,
    state_store: Any = None,
    msg_store: Any = None,
) -> bool:
    """Nudge the worker with the canonical turn-end reprompt.

    Uses ``session_service.send(..., press_enter=True)`` directly —
    the supervisor-level ``pm send --force`` semantics are emulated
    here (no lease check, no owner prefix) because the drift sweep
    already owns its side-effect window. Returns ``True`` on a
    successful send; ``False`` on any failure (best-effort).
    """
    if session_service is None or not session_name:
        return False
    try:
        session_service.send(session_name, WORKER_REPROMPT_TEXT)
    except Exception:  # noqa: BLE001
        logger.exception(
            "worker_turn_end: reprompt send to %s failed", session_name,
        )
        return False
    # #349: audit event moves to the unified ``messages`` table via Store;
    # fall back to the legacy StateStore.record_event when a Store isn't
    # wired through (older callers / test fixtures).
    task_id = getattr(task, "task_id", "") or ""
    message = (
        f"worker {session_name} reprompted on {task_id} "
        f"(turn ended without transition)"
    )
    if msg_store is not None:
        try:
            msg_store.append_event(
                scope=session_name,
                sender=session_name,
                subject="inbox.worker_reprompted",
                payload={"message": message, "task_id": task_id},
            )
        except Exception:  # noqa: BLE001
            pass
    elif state_store is not None:
        try:
            state_store.record_event(
                session_name, "inbox.worker_reprompted", message,
            )
        except Exception:  # noqa: BLE001
            pass
    return True


def is_worker_session_name(session_name: str) -> bool:
    """True when the session follows the ``worker-<project>`` or
    ``worker_<project>`` convention.

    This is how the sweep side (which lacks a SessionConfig lookup)
    decides whether to run the worker-specific path. Matches
    :func:`pollypm.work.task_assignment.role_candidate_names` where
    the worker role expands to exactly these two prefixes.
    """
    if not session_name:
        return False
    return session_name.startswith("worker-") or session_name.startswith(
        "worker_",
    )


def load_transcript_tail(
    session_service: Any,
    session_name: str,
    *,
    tail_chars: int = _TRANSCRIPT_TAIL_CHARS,
) -> str:
    """Best-effort pane-text capture used to feed the classifier.

    Prefers ``session_service.capture(name, lines=...)`` (what tmux
    exposes); falls back to reading the transcript log file on disk
    when the service doesn't implement ``capture``. Any error yields
    an empty string so the caller routes to the reprompt path.
    """
    if session_service is None or not session_name:
        return ""
    capture = getattr(session_service, "capture", None)
    if callable(capture):
        try:
            text = capture(session_name, lines=200)
            if isinstance(text, str):
                return text[-tail_chars:]
        except Exception:  # noqa: BLE001
            pass
    # Transcript-file fallback.
    transcript_fn = getattr(session_service, "transcript", None)
    if callable(transcript_fn):
        try:
            stream = transcript_fn(session_name)
            path = getattr(stream, "path", None) if stream else None
            if path is not None:
                data = Path(path).read_text(
                    encoding="utf-8", errors="replace",
                )
                return data[-tail_chars:]
        except Exception:  # noqa: BLE001
            return ""
    return ""


def handle_worker_turn_end(
    task: Any,
    session_name: str,
    *,
    work_service: Any,
    session_service: Any,
    state_store: Any,
    config: Any = None,
    msg_store: Any = None,
) -> str:
    """Top-level entry point invoked from the drift sweep.

    Decides between ``blocking_question`` and ``reprompt``, applies
    the chosen action, and returns the action name (``"blocking_question"``
    / ``"reprompt"`` / ``"skipped"``). Never raises — the sweep is a
    best-effort loop.

    ``msg_store`` (optional) is the #349 unified-messages Store. When
    provided, audit events land on ``messages`` instead of the legacy
    ``events`` table so the writer migration flips in lockstep with
    :mod:`pollypm.plugins_builtin.core_recurring.plugin`.
    """
    if not is_worker_session_name(session_name):
        return "skipped"
    transcript = load_transcript_tail(session_service, session_name)
    response = determine_worker_response(
        task, session_name, work_service, transcript,
    )
    if response.kind == "blocking_question":
        create_blocking_question_inbox_item(
            task,
            session_name,
            response.question_excerpt,
            work_service,
            config=config,
            state_store=state_store,
            msg_store=msg_store,
        )
        return "blocking_question"
    send_standard_reprompt(
        session_name, task, session_service,
        state_store=state_store,
        msg_store=msg_store,
    )
    return "reprompt"
