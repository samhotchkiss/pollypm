"""Rollup sub-item ``ListItem`` widget used by the cockpit inbox.

Contract:
- Inputs: ``index`` (slot in the expanded thread), ``item`` (dict payload
  with ``subject``/``body``/``actor``/``payload``/...), ``expanded``
  (whether the body is shown), and ``focused`` (whether this is the
  keyboard-focused row inside the rollup).
- Outputs: a ``ListItem`` subclass that renders a single sub-item row.
- Side effects: none beyond mounting a ``Static`` child.
- Invariants: this module owns one widget class and nothing else; the
  inbox app handles click/keyboard wiring via its own ``on`` decorator.
- Allowed dependencies: Textual primitives, ``cockpit_markup`` for the
  ``_escape``/``_escape_body`` helpers, and ``pollypm.tz`` /
  ``cockpit_ui._md_to_rich`` via local imports (the latter to avoid an
  import cycle while ``_md_to_rich`` still lives in ``cockpit_ui``).
- Private: ``_RollupItem`` is underscore-prefixed and re-exported via
  ``cockpit_ui`` for back-compat (see #1354).

Wedge of the cockpit_ui.py god-module split tracked by #1354.
"""

from __future__ import annotations

from textual.widgets import ListItem, Static

from pollypm.cockpit_markup import _escape, _escape_body


class _RollupItem(ListItem):
    """One sub-item in a rollup's expanded thread.

    We inherit ListItem for consistent hover/click semantics, but the
    widget lives inside a ``Vertical`` (not a ``ListView``), so it
    behaves as a click target only — no cursor selection. Click emits a
    ``Clicked`` message which the inbox app handles via ``on``.
    """

    def __init__(
        self,
        *,
        index: int,
        item: dict,
        expanded: bool,
        focused: bool,
    ) -> None:
        self.index = index
        self.item = item
        self.expanded = expanded
        self._body = Static(self._build_text(), markup=True)
        classes = ["rollup-item"]
        if expanded:
            classes.append("-expanded")
        if focused:
            classes.append("-focused")
        super().__init__(self._body, classes=" ".join(classes))

    def _build_text(self) -> str:
        from pollypm.tz import format_relative
        # Local import: ``_md_to_rich`` still lives in ``cockpit_ui`` and
        # importing it at module load time would create a cycle (cockpit_ui
        # imports this module for the re-export shim).
        from pollypm.cockpit_ui import _md_to_rich
        subject = self.item.get("subject") or "(no subject)"
        created = self.item.get("created_at") or ""
        age = format_relative(created) if created else ""
        payload = self.item.get("payload") or {}
        ref_bits: list[str] = []
        for key in ("commit", "pr", "pull_request", "url"):
            val = payload.get(key)
            if val:
                ref_bits.append(f"{key}={val}")
        marker = "\u25bc" if self.expanded else "\u25b8"
        header = f"[b]{marker} {_escape(subject)}[/b]"
        if age:
            header += f"  [dim]{_escape(age)}[/dim]"
        lines = [header]
        if ref_bits:
            separator = " · "
            lines.append(f"[dim]{_escape(separator.join(ref_bits))}[/dim]")
        if self.expanded:
            body = (self.item.get("body") or "").strip()
            if body:
                lines.append("")
                lines.append(_md_to_rich(_escape_body(body)))
            actor = self.item.get("actor") or ""
            source_project = self.item.get("source_project") or ""
            meta_bits: list[str] = []
            if actor:
                meta_bits.append(actor)
            if source_project:
                meta_bits.append(source_project)
            if meta_bits:
                lines.append("")
                meta_separator = " · "
                lines.append(f"[dim]{_escape(meta_separator.join(meta_bits))}[/dim]")
        return "\n".join(lines)


__all__ = ["_RollupItem"]
