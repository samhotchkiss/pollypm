"""Flow engine — YAML loading, validation, and override chain resolution.

Parses flow definition YAML files into FlowTemplate + FlowNode objects,
validates the graph structure, and resolves flows through the three-tier
override chain (built-in < user-global < project-local).
"""

from __future__ import annotations

import importlib.resources
from pathlib import Path

import yaml

from pollypm.work.models import ActorType, FlowNode, FlowTemplate, NodeType


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------


class FlowValidationError(Exception):
    """Raised when a flow definition fails validation."""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _parse_node(name: str, data: dict) -> FlowNode:
    """Parse a single node dict from YAML into a FlowNode."""
    node_type_str = data.get("type", "")
    try:
        node_type = NodeType(node_type_str)
    except ValueError:
        raise FlowValidationError(
            f"Node '{name}': invalid type '{node_type_str}'. "
            f"Valid types: {[t.value for t in NodeType]}"
        )

    actor_type: ActorType | None = None
    actor_type_str = data.get("actor_type")
    if actor_type_str is not None:
        try:
            actor_type = ActorType(actor_type_str)
        except ValueError:
            raise FlowValidationError(
                f"Node '{name}': invalid actor_type '{actor_type_str}'. "
                f"Valid actor types: {[a.value for a in ActorType]}"
            )

    return FlowNode(
        name=name,
        type=node_type,
        actor_type=actor_type,
        actor_role=data.get("actor_role"),
        next_node_id=data.get("next_node"),
        reject_node_id=data.get("reject_node"),
        gates=data.get("gates", []),
    )


def parse_flow_yaml(text: str) -> FlowTemplate:
    """Parse a YAML string into a FlowTemplate, then validate it.

    Raises FlowValidationError on any structural problem.
    """
    raw = yaml.safe_load(text)
    if not isinstance(raw, dict):
        raise FlowValidationError("Flow YAML must be a mapping at the top level.")

    name = raw.get("name", "")
    if not name:
        raise FlowValidationError("Flow definition must have a 'name' field.")

    description = raw.get("description", "")
    roles: dict[str, dict] = raw.get("roles", {})
    if not isinstance(roles, dict):
        raise FlowValidationError("'roles' must be a mapping.")

    raw_nodes = raw.get("nodes", {})
    if not isinstance(raw_nodes, dict):
        raise FlowValidationError("'nodes' must be a mapping.")

    nodes: dict[str, FlowNode] = {}
    for node_name, node_data in raw_nodes.items():
        if not isinstance(node_data, dict):
            raise FlowValidationError(
                f"Node '{node_name}' must be a mapping, got {type(node_data).__name__}."
            )
        nodes[node_name] = _parse_node(node_name, node_data)

    start_node = raw.get("start_node", "")
    version = raw.get("version", 1)

    template = FlowTemplate(
        name=name,
        description=description,
        roles=roles,
        nodes=nodes,
        start_node=start_node,
        version=version,
    )

    validate_flow(template)
    return template


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_flow(template: FlowTemplate) -> None:
    """Validate a FlowTemplate's graph structure.

    Raises FlowValidationError with a description of the problem.
    """
    errors: list[str] = []
    nodes = template.nodes
    node_names = set(nodes.keys())

    # start_node must exist
    if not template.start_node:
        errors.append("Flow must define a 'start_node'.")
    elif template.start_node not in node_names:
        errors.append(
            f"start_node '{template.start_node}' does not exist in nodes. "
            f"Available nodes: {sorted(node_names)}"
        )

    # At least one terminal node
    terminal_nodes = [n for n in nodes.values() if n.type == NodeType.TERMINAL]
    if not terminal_nodes:
        errors.append("Flow must have at least one terminal node (type: terminal).")

    for name, node in nodes.items():
        # next_node references must be valid
        if node.next_node_id is not None and node.next_node_id not in node_names:
            errors.append(
                f"Node '{name}': next_node '{node.next_node_id}' does not exist."
            )

        # reject_node references must be valid
        if node.reject_node_id is not None and node.reject_node_id not in node_names:
            errors.append(
                f"Node '{name}': reject_node '{node.reject_node_id}' does not exist."
            )

        # Only review nodes may have a reject_node
        if node.reject_node_id is not None and node.type != NodeType.REVIEW:
            errors.append(
                f"Node '{name}': only review nodes may have a reject_node, "
                f"but this node has type '{node.type.value}'."
            )

        # Review nodes MUST have a reject_node
        if node.type == NodeType.REVIEW and node.reject_node_id is None:
            errors.append(
                f"Node '{name}': review nodes must have a reject_node."
            )

        # Role-typed nodes must specify actor_role that exists in roles
        if node.actor_type == ActorType.ROLE:
            if not node.actor_role:
                errors.append(
                    f"Node '{name}': actor_type is 'role' but no actor_role specified."
                )
            elif node.actor_role not in template.roles:
                errors.append(
                    f"Node '{name}': actor_role '{node.actor_role}' not found in "
                    f"flow roles. Available roles: {sorted(template.roles.keys())}"
                )

    # No orphan nodes: every non-start node must be reachable from start
    if template.start_node and template.start_node in node_names:
        reachable: set[str] = set()
        frontier = [template.start_node]
        while frontier:
            current = frontier.pop()
            if current in reachable:
                continue
            reachable.add(current)
            node = nodes.get(current)
            if node is None:
                continue
            if node.next_node_id and node.next_node_id not in reachable:
                frontier.append(node.next_node_id)
            if node.reject_node_id and node.reject_node_id not in reachable:
                frontier.append(node.reject_node_id)

        orphans = node_names - reachable
        if orphans:
            errors.append(
                f"Orphan nodes not reachable from start_node "
                f"'{template.start_node}': {sorted(orphans)}"
            )

    if errors:
        raise FlowValidationError(
            f"Flow '{template.name}' failed validation:\n"
            + "\n".join(f"  - {e}" for e in errors)
        )


# ---------------------------------------------------------------------------
# File loading helpers
# ---------------------------------------------------------------------------


def _builtin_flows_dir() -> Path:
    """Return the path to the built-in flows directory inside the package."""
    ref = importlib.resources.files("pollypm.work") / "flows"
    # importlib.resources may return a Traversable; for our purposes we
    # need a real filesystem path (the YAML files are on disk in the package).
    return Path(str(ref))


def _user_global_flows_dir() -> Path:
    """Return ~/.pollypm/flows/."""
    return Path.home() / ".pollypm" / "flows"


def _project_flows_dir(project_path: str | Path) -> Path:
    """Return <project>/.pollypm/flows/."""
    return Path(project_path) / ".pollypm" / "flows"


def _load_flow_from_file(path: Path) -> FlowTemplate:
    """Load and validate a single flow YAML file."""
    text = path.read_text(encoding="utf-8")
    return parse_flow_yaml(text)


def _list_flow_files(directory: Path) -> dict[str, Path]:
    """List .yaml flow files in a directory, keyed by stem (flow name)."""
    if not directory.is_dir():
        return {}
    result: dict[str, Path] = {}
    for p in sorted(directory.iterdir()):
        if p.suffix in (".yaml", ".yml") and p.is_file():
            result[p.stem] = p
    return result


# ---------------------------------------------------------------------------
# Override chain resolution
# ---------------------------------------------------------------------------


def resolve_flow(name: str, project_path: str | Path | None = None) -> FlowTemplate:
    """Resolve a flow by name through the three-tier override chain.

    Precedence (highest first):
    1. Project-local: <project>/.pollypm/flows/<name>.yaml
    2. User-global:   ~/.pollypm/flows/<name>.yaml
    3. Built-in:      pollypm/work/flows/<name>.yaml

    Raises FlowValidationError if the flow is not found at any level,
    or if the found YAML is invalid.
    """
    search_paths: list[Path] = []

    if project_path is not None:
        proj_dir = _project_flows_dir(project_path)
        search_paths.append(proj_dir)

    search_paths.append(_user_global_flows_dir())
    search_paths.append(_builtin_flows_dir())

    for directory in search_paths:
        for suffix in (".yaml", ".yml"):
            candidate = directory / f"{name}{suffix}"
            if candidate.is_file():
                return _load_flow_from_file(candidate)

    raise FlowValidationError(
        f"Flow '{name}' not found. Searched:\n"
        + "\n".join(f"  - {d}" for d in search_paths)
    )


def available_flows(
    project_path: str | Path | None = None,
) -> dict[str, Path]:
    """Return all available flow names after override resolution.

    Returns a dict mapping flow name -> the path of the winning file
    (highest precedence). The dict is ordered by flow name.
    """
    # Start with built-in (lowest precedence), then overlay higher levels.
    merged: dict[str, Path] = {}
    merged.update(_list_flow_files(_builtin_flows_dir()))
    merged.update(_list_flow_files(_user_global_flows_dir()))
    if project_path is not None:
        merged.update(_list_flow_files(_project_flows_dir(project_path)))
    return dict(sorted(merged.items()))
