"""Inbox primitives — leaf-level types shared by every surface.

This package is the canonical home for inbox shape and behaviour that
multiple surfaces (cockpit, CLI, web API, work-service, plan-review
emit sites) need to agree on. Modules here MUST stay dependency-light:
no cockpit imports, no Supervisor imports, no DB access. They are
pure types + predicates so the work-service, the rail, the cockpit,
and tests can all import them without circular dependencies.

Current contents (#1565, #1566):

* :class:`InboxItemKind` — the structured kind taxonomy stamped on
  every inbox row (messages + work-service task rows that surface in
  the inbox view).
* :func:`awaits_user` — the single canonical predicate answering
  "does this inbox item need the user?"
"""

from pollypm.inbox.kind import InboxItemKind
from pollypm.inbox.predicates import awaits_user

__all__ = ["InboxItemKind", "awaits_user"]
