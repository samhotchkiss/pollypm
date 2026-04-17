"""Belt-and-suspenders validator for downtime flows.

The ``downtime_explore`` flow shipped with this plugin has a fixed shape
— see ``flows/downtime_explore.yaml`` and spec §5. Operators may
override it via a user-global or project-local flow of the same name.
The flow-engine parser (``pollypm.work.flow_engine.validate_flow``) only
checks generic graph invariants (start node, terminals, reachability).
It will happily accept a "downtime" flow that lacks a human-approval
node — which would quietly silently break the never-auto-deploy rule
(spec §10).

This module adds a named validator the downtime plugin can run against
a candidate flow. It's called from two places:

* Plugin ``initialize`` — validates the built-in flow at load time so a
  shipping glitch fails loudly.
* Task creation — callers that create downtime tasks should call
  :func:`assert_downtime_flow_shape` before queuing.

Rules enforced:

1. Exactly one node with ``actor_type == human`` exists (the approval
   node). Keeps the human touchpoint singular so there's no ambiguous
   "is this *the* approval?" at runtime.
2. The approval node has the ``inbox_notification_sent`` hard gate.
3. The approval node's ``next_node`` reaches a subsequent work node
   (the ``apply`` node) before reaching a terminal — so "approved"
   routes through the commit/archive dispatch rather than short-
   circuiting to ``done``.
4. No non-human node can bypass the approval node on the way to the
   terminal. That is: from ``start_node``, every path to a terminal
   must pass through the human node.

Rejection is a raised :class:`DowntimeFlowValidationError`. The plugin
host catches this and disables the plugin; individual callers can
catch and degrade more gracefully.
"""
from __future__ import annotations

from pollypm.work.models import ActorType, FlowNode, FlowTemplate, NodeType


_REQUIRED_GATE = "inbox_notification_sent"


class DowntimeFlowValidationError(Exception):
    """Raised when a downtime flow fails the never-auto-deploy shape rules."""


def _find_human_nodes(flow: FlowTemplate) -> list[FlowNode]:
    return [n for n in flow.nodes.values() if n.actor_type == ActorType.HUMAN]


def _paths_through_node(flow: FlowTemplate, required_node: str) -> tuple[bool, str]:
    """Walk every path from ``start_node`` to a terminal.

    Returns ``(True, "")`` if every such path passes through
    ``required_node``; otherwise ``(False, "<offending path>")``.
    """
    # Breadth-first exploration with explicit path tracking. The flow
    # graph is small (≤ a few dozen nodes) — no need for cycle-aware
    # memoisation beyond "don't revisit within the same path".
    start = flow.start_node
    if not start or start not in flow.nodes:
        return False, "missing start_node"

    stack: list[tuple[str, tuple[str, ...]]] = [(start, (start,))]
    while stack:
        current, path = stack.pop()
        node = flow.nodes.get(current)
        if node is None:
            continue
        if node.type == NodeType.TERMINAL:
            if required_node not in path:
                return False, " -> ".join(path)
            continue
        for edge in (node.next_node_id, node.reject_node_id):
            if edge is None:
                continue
            if edge in path:
                # Cycle back to a node already in this path — treat the
                # cycle as a terminal leaf for validation purposes so
                # we don't infinite-loop. If the cycle closes without
                # crossing the required node, that's a bypass.
                if required_node not in path:
                    return False, " -> ".join(path + (edge,))
                continue
            stack.append((edge, path + (edge,)))
    return True, ""


def assert_downtime_flow_shape(flow: FlowTemplate) -> None:
    """Validate a flow against the never-auto-deploy rules.

    Raises :class:`DowntimeFlowValidationError` with a concrete
    explanation on failure. Pass means the flow is safe to use as a
    downtime flow.
    """
    errors: list[str] = []

    humans = _find_human_nodes(flow)
    if len(humans) == 0:
        errors.append(
            "downtime flow must have exactly one human-actor node "
            "(actor_type=human); found 0."
        )
    elif len(humans) > 1:
        names = ", ".join(sorted(n.name for n in humans))
        errors.append(
            f"downtime flow must have exactly one human-actor node; "
            f"found {len(humans)}: {names}."
        )

    approval_node: FlowNode | None = humans[0] if len(humans) == 1 else None

    if approval_node is not None:
        if _REQUIRED_GATE not in approval_node.gates:
            errors.append(
                f"downtime approval node '{approval_node.name}' must declare "
                f"the '{_REQUIRED_GATE}' hard gate; found gates: "
                f"{list(approval_node.gates)}."
            )
        # Approval must route forward to a non-terminal "apply" step
        # before the terminal, to give the apply handler a chance to
        # commit/archive. If next_node is a terminal, approval skips
        # apply — that's the auto-deploy-via-short-circuit hole.
        next_id = approval_node.next_node_id
        if next_id is None:
            errors.append(
                f"downtime approval node '{approval_node.name}' must set "
                f"next_node (the apply step)."
            )
        else:
            next_node = flow.nodes.get(next_id)
            if next_node is None:
                errors.append(
                    f"downtime approval node '{approval_node.name}' "
                    f"next_node '{next_id}' does not exist."
                )
            elif next_node.type == NodeType.TERMINAL:
                errors.append(
                    f"downtime approval node '{approval_node.name}' "
                    f"next_node '{next_id}' is terminal — approval must "
                    f"route through an apply step before terminating."
                )

        # Every path from start must pass through the human node.
        ok, offending = _paths_through_node(flow, approval_node.name)
        if not ok:
            errors.append(
                f"downtime flow has a path from start to terminal that "
                f"bypasses the human approval node '{approval_node.name}': "
                f"{offending or '<unknown>'}."
            )

    if errors:
        raise DowntimeFlowValidationError(
            f"Flow '{flow.name}' is not a valid downtime flow:\n"
            + "\n".join(f"  - {e}" for e in errors)
        )


def is_downtime_flow(flow: FlowTemplate) -> bool:
    """Heuristic: a flow is a downtime flow if its name starts with
    ``downtime_`` or contains the label ``downtime`` in its description.

    Used by the plugin's initialize hook to decide which flows to
    re-validate under the stricter downtime rules. Callers may pass an
    explicit flow to :func:`assert_downtime_flow_shape` to override the
    heuristic.
    """
    if not flow.name:
        return False
    if flow.name.startswith("downtime_") or flow.name == "downtime":
        return True
    desc = (flow.description or "").lower()
    return "downtime" in desc and "explor" in desc
