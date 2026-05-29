"""Recovery actions for blocked / on-hold tasks (#1016).

The project Dashboard, Tasks pane row, and individual task detail view
all describe *why* a task is stuck — but until this module landed they
never told the operator *what to do next*. ``recovery_action_for(task)``
parses the task's hold/blocker reason against a small dispatch table and
returns a typed :class:`RecoveryAction` the rendering layers can show
verbatim.

The dispatch table covers the canonical reason prefixes emitted today:

* ``paused: dirty project root <path>`` /
  ``paused: project root has uncommitted changes`` —
  produce ``git -C <path> add … && git commit …`` followed by
  ``pm task resume <task>``.
* ``paused: review passed but auto-merge refused: <reason>`` /
  ``cannot auto-merge`` — surface the underlying reason and a retry
  affordance (``pm task approve <task> --retry``).
* ``blocked: waiting on <dep>`` — point the operator at the upstream
  task to unblock first.
* ``on_hold: waiting on operator decision: <question>`` /
  ``Waiting on operator: …`` — open the inbox so the operator can
  respond.
* ``on_hold: permission prompt`` — point at the pane that has the
  prompt open.

Anything else falls through to the generic
``Open inbox / task to investigate`` affordance so the renderer always
has *some* concrete next step to show, even for novel reasons.

The renderers (``cockpit_ui._render_pipeline``, the Tasks pane row, and
``cockpit_tasks._render_overview``) call :func:`recovery_action_for`
once per task and decide layout. The keybinding wiring lives with the
renderers — this module is pure data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pollypm.operator_holds import (
    human_needed_hold_prompt,
    is_human_needed_hold_reason,
)


@dataclass(frozen=True)
class RecoveryAction:
    """The canonical operator response for a stuck task.

    ``title`` is a short header (``Commit project root``).
    ``detail`` is one prose line explaining the why.
    ``cli_steps`` are the exact commands to run, top to bottom.
    ``keybinding`` names a cockpit shortcut when one exists; renderers
    use it to render ``[press <key> to do all of this]`` hints.
    """

    title: str
    detail: str
    cli_steps: list[str] = field(default_factory=list)
    keybinding: str | None = None


def _task_id(task: object) -> str:
    """Return ``project/N`` for a Task-like value, or ``""`` if absent.

    Handles both hydrated ``Task`` objects (attribute access) and the
    dashboard's bucket-dict shape (key access).
    """
    if isinstance(task, dict):
        direct = task.get("task_id")
        if direct:
            return str(direct)
        project = task.get("project")
        number = task.get("task_number")
        if project and number is not None:
            return f"{project}/{number}"
        return ""
    direct = getattr(task, "task_id", None)
    if direct:
        return str(direct)
    project = getattr(task, "project", None)
    number = getattr(task, "task_number", None)
    if project and number is not None:
        return f"{project}/{number}"
    return ""


def _task_reason(task: object) -> str:
    """Pull the most-recent on_hold / blocked reason off ``task``.

    The cockpit dashboard already reads transitions in
    ``_dashboard_pipeline_buckets`` (cockpit_ui.py) and stores the
    derived string as ``hold_reason`` on the bucket dict. To keep this
    helper callable from BOTH a hydrated ``Task`` and a bucket dict,
    we look at:

    1. ``task.reason`` — convenience attr if a caller threads one in.
    2. ``task.hold_reason`` — the dashboard bucket field.
    3. The last ``on_hold`` transition's ``reason`` on
       ``task.transitions``.
    4. The last ``blocked`` transition's ``reason`` (for
       ``waiting_on_plan`` / ``blocked`` rows).
    """
    for attr in ("reason", "hold_reason"):
        value = getattr(task, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(task, dict):  # bucket dict path
            value = task.get(attr)
            if isinstance(value, str) and value.strip():
                return value.strip()
    transitions = getattr(task, "transitions", None) or []
    for target in ("on_hold", "blocked"):
        for transition in reversed(list(transitions)):
            if getattr(transition, "to_state", "") == target:
                value = getattr(transition, "reason", None)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return ""


def _task_status(task: object) -> str:
    if isinstance(task, dict):
        value = task.get("work_status") or task.get("status")
        if value:
            return str(value)
        return ""
    status = getattr(task, "work_status", None)
    if status is not None:
        value = getattr(status, "value", None)
        if value:
            return str(value)
    return ""


# Regexes for the dispatch table. They live at module scope so importers
# can monkey-patch / extend the dispatch in tests if the prose drifts.

_DIRTY_ROOT_PATH_RE = re.compile(
    # Match "project root <path> has [adj]* uncommitted <files>" so a
    # filler word like "unrelated" between ``has`` and ``uncommitted``
    # (the canonical bikepath/8 wording) doesn't slip past the parser.
    # Files can include leading dots (``.gitignore``) so we use a
    # non-greedy match terminated by a sentence boundary or end-of-line.
    r"project root\s+(?P<path>/\S+)\s+has\s+(?:\w+\s+)?uncommitted\s+(?P<files>.+?)(?:\.\s|\.$|$)",
    re.IGNORECASE,
)
_DIRTY_ROOT_GENERIC_RE = re.compile(
    r"project root\s+has\s+(?:\w+\s+)?uncommitted",
    re.IGNORECASE,
)
_AUTO_MERGE_RE = re.compile(
    # Match the canonical service-emitted strings ("cannot auto-merge",
    # "could not auto-merge") and the natural-language variant
    # operators sometimes type by hand ("auto-merge refused", "auto
    # merge failed").
    r"(?:(?:cannot|could not)\s+auto[- ]merge|auto[- ]merge\s+(?:refused|failed))",
    re.IGNORECASE,
)
_OPERATOR_RE = re.compile(
    r"waiting on operator(?:\s+(?:decision|input))?\s*[:\-—]?\s*(?P<question>.*)",
    re.IGNORECASE,
)
_PERMISSION_RE = re.compile(
    r"permission\s+prompt", re.IGNORECASE,
)
_BLOCKED_DEP_RE = re.compile(
    r"(?:blocked|waiting)\s+(?:on|by)\s+(?P<dep>[A-Za-z0-9_][A-Za-z0-9_-]*/\d+)",
    re.IGNORECASE,
)


def _split_dirty_files(raw: str) -> list[str]:
    """Turn ``" .gitignore, docs/, and issues/"`` into the
    list ``[".gitignore", "docs/", "issues/"]``.

    We split on commas and the connecting word ``and`` so the produced
    ``git add`` line names the same files the operator already sees in
    the hold reason — without picking up trailing punctuation.
    """
    text = raw.strip().rstrip(".")
    # Drop a trailing "and " inside the last comma-segment first
    # (``", and "`` is the natural English separator), then split on
    # commas so each token is a file path with no leading "and ".
    text = re.sub(r",?\s+and\s+", ", ", text)
    parts = text.split(",")
    cleaned: list[str] = []
    for part in parts:
        token = part.strip().rstrip(".")
        if token:
            cleaned.append(token)
    return cleaned


def _action_dirty_root(reason: str, task_label: str) -> RecoveryAction | None:
    """``paused: dirty project root <path>`` family."""
    match = _DIRTY_ROOT_PATH_RE.search(reason)
    if match:
        path = match.group("path").rstrip(".,")
        files = _split_dirty_files(match.group("files"))
        if files:
            add_cmd = (
                f"git -C {path} add " + " ".join(files)
            )
        else:
            add_cmd = f"git -C {path} add ."
        commit_cmd = (
            f"git -C {path} commit -m \"Project root setup\""
        )
        return RecoveryAction(
            title=f"Recovery action for {task_label} — uncommitted project root",
            detail=f"Commit unrelated root state in {path}, then resume.",
            cli_steps=[
                add_cmd,
                commit_cmd,
                f"pm task approve {task_label} --retry",
            ],
            keybinding="R",
        )
    if _DIRTY_ROOT_GENERIC_RE.search(reason):
        return RecoveryAction(
            title=f"Recovery action for {task_label} — uncommitted project root",
            detail=(
                "Commit or stash uncommitted changes in the project "
                "root, then retry the approve."
            ),
            cli_steps=[
                "# review uncommitted files",
                "git status",
                "# commit (or stash) the unrelated changes",
                "git add . && git commit -m \"Project root setup\"",
                f"pm task approve {task_label} --retry",
            ],
            keybinding="R",
        )
    return None


def _action_auto_merge(reason: str, task_label: str) -> RecoveryAction | None:
    """``review passed but auto-merge refused`` / ``cannot auto-merge``."""
    if not _AUTO_MERGE_RE.search(reason):
        return None
    # Strip anything before the auto-merge clause for ``detail`` so the
    # one-liner reads as the underlying cause, not a re-quote of the
    # full hold reason.
    cause = reason
    pivot = re.search(_AUTO_MERGE_RE, cause)
    if pivot:
        cause = cause[pivot.start():]
    cause = cause.strip().rstrip(".") + "."
    return RecoveryAction(
        title=f"Recovery action for {task_label} — auto-merge refused",
        detail=cause,
        cli_steps=[
            "# inspect the merge state and resolve the refusal",
            "git status",
            f"pm task approve {task_label} --retry",
        ],
        keybinding="R",
    )


def _action_blocked_dep(reason: str, task_label: str) -> RecoveryAction | None:
    """``blocked: waiting on <dep>``."""
    match = _BLOCKED_DEP_RE.search(reason)
    if not match:
        return None
    dep = match.group("dep")
    return RecoveryAction(
        title=f"Recovery action for {task_label} — blocked on {dep}",
        detail=f"Unblock {dep} first; this task picks up automatically.",
        cli_steps=[
            f"pm task get {dep}",
            f"# resolve {dep}, then this task will move on its own",
        ],
        keybinding="R",
    )


def _action_operator_decision(reason: str, task_label: str) -> RecoveryAction | None:
    """``Waiting on operator: <question>`` / ``waiting on operator decision``.

    Run AFTER ``_action_dirty_root`` and ``_action_auto_merge`` because
    auto-emitted reasons often start with ``Waiting on operator:`` and
    *then* describe a more-specific problem (the auto-merge case is the
    canonical example: the bikepath reason starts ``Waiting on
    operator: code review passed, but pm task approve cannot
    auto-merge…``).
    """
    match = _OPERATOR_RE.search(reason)
    if not match:
        return None
    question = match.group("question").strip().rstrip(".").strip()
    if question:
        detail = f"Operator decision needed: {question}."
    else:
        detail = "Operator decision needed — open the inbox to respond."
    return RecoveryAction(
        title=f"Recovery action for {task_label} — operator decision",
        detail=detail,
        cli_steps=[
            f"pm task get {task_label}",
            "# open the inbox item, decide, and resume:",
            f"pm task resume {task_label}",
        ],
        keybinding="R",
    )


def _action_permission_prompt(reason: str, task_label: str) -> RecoveryAction | None:
    if not _PERMISSION_RE.search(reason):
        return None
    return RecoveryAction(
        title=f"Recovery action for {task_label} — permission prompt",
        detail=(
            "A permission prompt is open in the worker pane. "
            "Approve or reject it to unblock the task."
        ),
        cli_steps=[
            f"# attach to the worker pane for {task_label} and"
            " approve/reject the prompt",
            f"pm task get {task_label}",
        ],
        keybinding="R",
    )


def _action_human_needed(reason: str, task_label: str) -> RecoveryAction | None:
    if not is_human_needed_hold_reason(reason):
        return None
    prompt = human_needed_hold_prompt(reason)
    detail = (
        f"Operator input needed: {prompt}."
        if prompt
        else "Operator input needed before Polly can continue."
    )
    return RecoveryAction(
        title=f"Recovery action for {task_label} - operator input",
        detail=detail,
        cli_steps=[
            f"pm task get {task_label}",
            "# provide the requested input, credentials, or decision",
            f"pm task resume {task_label}",
        ],
        keybinding="R",
    )


def _action_generic(task_label: str, status: str) -> RecoveryAction:
    """Fall-through affordance.

    Even when the dispatch can't pattern-match the reason, we still
    return *something*: the fix's UX promise is that anywhere a stuck
    task shows up, the operator sees a concrete next step. The generic
    step is "go look at the task" — better than the wall-of-text
    status quo the issue describes.
    """
    if status == "blocked":
        detail = "This task is blocked. Open it to see the dependency chain."
    elif status == "on_hold":
        detail = "This task is on hold. Open it to read the reason and decide."
    else:
        detail = "Open the task to investigate the next step."
    return RecoveryAction(
        title=f"Recovery action for {task_label} — investigate",
        detail=detail,
        cli_steps=[
            f"pm task get {task_label}",
            f"pm inbox --task {task_label}",
        ],
        keybinding="R",
    )


# Order matters: the more-specific patterns run first so a hold reason
# that mentions BOTH ``operator`` AND ``auto-merge`` (the bikepath/8
# canonical case) routes to the auto-merge / dirty-root handler, not
# the generic operator-decision branch.
_DISPATCH = (
    _action_dirty_root,
    _action_auto_merge,
    _action_blocked_dep,
    _action_permission_prompt,
    _action_human_needed,
    _action_operator_decision,
)


def recovery_action_for(task: object) -> RecoveryAction | None:
    """Return the canonical operator response for a stuck task.

    Returns ``None`` only when the task is not in a stuck state
    (i.e. its status is not one of ``on_hold``, ``blocked``,
    ``waiting_on_plan``). Stuck tasks always get a
    :class:`RecoveryAction` — the dispatch table handles known reason
    prefixes; everything else falls through to a generic "investigate"
    affordance.
    """
    status = _task_status(task)
    if status not in {"on_hold", "blocked", "waiting_on_plan"}:
        return None
    label = _task_id(task) or "this task"
    reason = _task_reason(task)
    if reason:
        for handler in _DISPATCH:
            action = handler(reason, label)
            if action is not None:
                return action
    return _action_generic(label, status)


def render_recovery_action_block(
    action: RecoveryAction,
    *,
    indent: str = "   ",
    bullet: str = "◆",
) -> list[str]:
    """Render a :class:`RecoveryAction` as a list of dashboard lines.

    Used by the project Dashboard and the individual task detail view —
    both want the full block. The Tasks pane row uses
    :func:`render_recovery_action_summary` instead because it has only
    one line of vertical budget per row.
    """
    lines = [f"{bullet} {action.title}"]
    lines.append(f"{indent}{action.detail}")
    if action.cli_steps:
        for step in action.cli_steps:
            if step.startswith("#"):
                # Comment lines render dim — no ``$`` prefix.
                lines.append(f"{indent}  {step}")
            else:
                lines.append(f"{indent}  $ {step}")
    if action.keybinding:
        lines.append(
            f"{indent}[press {action.keybinding} to do all of this]"
        )
    return lines


def render_recovery_action_summary(action: RecoveryAction) -> str:
    """One-line summary for the Tasks pane row.

    Format: ``→ <title-tail> (press R)``. The title's "Recovery
    action for #N — " preamble is dropped because the row already
    names the task; what we want inline is the *what to do* tail.
    """
    title = action.title
    marker = " — "
    if marker in title:
        tail = title.split(marker, 1)[1]
    else:
        tail = title
    suffix = (
        f" (press {action.keybinding})" if action.keybinding else ""
    )
    return f"→ recovery: {tail}{suffix}"
