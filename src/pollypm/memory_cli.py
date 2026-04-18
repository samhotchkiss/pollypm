"""Operator CLI for the memory store (``pm memory ...``).

Implements M07 / #236:

    pm memory list    [--scope X] [--type Y] [--importance N] [--limit N] [--json]
    pm memory show    <id>       [--json]
    pm memory edit    <id>       [--body ...] [--importance N] [--tags a,b,c]
    pm memory forget  <id>       [--yes]
    pm memory recall  "query"    [--scope X] [--limit N] [--json]
    pm memory stats                             [--json]

Thin façade over :class:`FileMemoryBackend` public methods.
``--config`` picks the project whose ``state.db`` we connect to —
matches the convention used by ``pm jobs`` and friends.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer

from pollypm.config import DEFAULT_CONFIG_PATH, load_config, resolve_config_path
from pollypm.memory_backends import FileMemoryBackend, MemoryEntry, get_memory_backend


__all__ = ["memory_app", "build_backend_for_config"]


memory_app = typer.Typer(
    help=(
        "Inspect and manage the memory store.\n\n"
        "Examples:\n\n"
        "• pm memory list                     — show memories for the user\n"
        "• pm memory show <id>                — print one memory's body\n"
        "• pm memory search <query>           — full-text search across memories\n"
    )
)


# ---------------------------------------------------------------------------
# Backend wiring (tests override via ``set_backend_factory``)
# ---------------------------------------------------------------------------


_backend_factory = None  # type: ignore[assignment]


def set_backend_factory(factory) -> None:
    """Install a backend factory. Tests call this to inject a stub."""
    global _backend_factory
    _backend_factory = factory


def build_backend_for_config(config_path: Path) -> FileMemoryBackend:
    """Default factory: resolve the config, open a backend against the project."""
    resolved = resolve_config_path(config_path)
    if not resolved.exists():
        from pollypm.errors import format_config_not_found_error

        raise typer.BadParameter(format_config_not_found_error(resolved))
    config = load_config(resolved)
    project_root = config.project.root_dir
    return get_memory_backend(project_root, "file")  # type: ignore[return-value]


def _open_backend(config_path: Path) -> FileMemoryBackend:
    if _backend_factory is not None:
        return _backend_factory(config_path)
    return build_backend_for_config(config_path)


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _entry_to_dict(entry: MemoryEntry) -> dict[str, Any]:
    return {
        "id": entry.entry_id,
        "scope": entry.scope,
        "scope_tier": entry.scope_tier,
        "type": entry.type,
        "kind": entry.kind,
        "title": entry.title,
        "body": entry.body,
        "tags": list(entry.tags),
        "source": entry.source,
        "importance": entry.importance,
        "superseded_by": entry.superseded_by,
        "ttl_at": entry.ttl_at,
        "created_at": entry.created_at,
        "updated_at": entry.updated_at,
        "file_path": str(entry.file_path),
    }


def _emit_json(payload: object) -> None:
    typer.echo(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _print_entry_row(entry: MemoryEntry) -> None:
    tags = ",".join(entry.tags) if entry.tags else "-"
    typer.echo(
        f"{entry.entry_id:>5}  "
        f"{entry.type:<9}  "
        f"i{entry.importance}  "
        f"tier={entry.scope_tier:<7}  "
        f"scope={entry.scope:<16}  "
        f"tags={tags}  "
        f"{entry.title[:60]}"
    )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@memory_app.command("list")
def memory_list(
    scope: str | None = typer.Option(None, "--scope", help="Filter by scope."),
    type_: str | None = typer.Option(None, "--type", help="Filter by memory type."),
    importance_min: int = typer.Option(
        1, "--importance", help="Minimum importance (1-5).",
    ),
    limit: int = typer.Option(50, "--limit", help="Maximum rows to return."),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path.",
    ),
) -> None:
    """Browse memory entries, most recent first."""
    if limit <= 0:
        raise typer.BadParameter("--limit must be >= 1")
    if not (1 <= importance_min <= 5):
        raise typer.BadParameter("--importance must be in 1..5")

    backend = _open_backend(config_path)
    # ``recall("")`` returns everything — we re-filter by importance here
    # since the backend's ``list_entries`` only filters on scope/type.
    results = backend.recall(
        "",
        scope=scope,
        types=[type_] if type_ else None,
        importance_min=importance_min,
        limit=limit,
    )
    entries = [r.entry for r in results]

    if json_output:
        _emit_json([_entry_to_dict(e) for e in entries])
        return
    if not entries:
        typer.echo("No entries match.")
        return
    for entry in entries:
        _print_entry_row(entry)


@memory_app.command("show")
def memory_show(
    entry_id: int = typer.Argument(..., help="Memory entry id."),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path.",
    ),
) -> None:
    """Show full entry contents plus supersession chain."""
    backend = _open_backend(config_path)
    entry = backend.read_entry(entry_id)
    if entry is None:
        typer.echo(f"Entry {entry_id} not found.", err=True)
        raise typer.Exit(code=1)

    chain = _supersession_chain(backend, entry)
    payload = _entry_to_dict(entry)
    payload["supersession_chain"] = [
        {"id": e.entry_id, "title": e.title, "superseded_by": e.superseded_by}
        for e in chain
    ]
    if json_output:
        _emit_json(payload)
        return

    typer.echo(f"id         = {entry.entry_id}")
    typer.echo(f"type       = {entry.type}")
    typer.echo(f"importance = {entry.importance}")
    typer.echo(f"scope      = {entry.scope} (tier={entry.scope_tier})")
    typer.echo(f"tags       = {', '.join(entry.tags) or '-'}")
    typer.echo(f"source     = {entry.source}")
    typer.echo(f"created_at = {entry.created_at}")
    typer.echo(f"updated_at = {entry.updated_at}")
    typer.echo(f"ttl_at     = {entry.ttl_at or '-'}")
    if entry.superseded_by is not None:
        typer.echo(f"superseded_by = {entry.superseded_by}")
    typer.echo(f"file_path  = {entry.file_path}")
    typer.echo("")
    typer.echo(f"## {entry.title}")
    typer.echo("")
    typer.echo(entry.body.rstrip())

    if len(chain) > 1:
        typer.echo("")
        typer.echo("## Supersession chain")
        for link in chain:
            typer.echo(
                f"  #{link.entry_id} {link.title[:60]} "
                f"{'(current)' if link.entry_id == entry.entry_id else ''}"
            )


@memory_app.command("edit")
def memory_edit(
    entry_id: int = typer.Argument(..., help="Memory entry id."),
    body: str | None = typer.Option(None, "--body", help="Replace the entry body."),
    importance: int | None = typer.Option(
        None, "--importance", help="Set importance (1-5).",
    ),
    tags: str | None = typer.Option(
        None, "--tags", help="Comma-separated tag list (replaces current).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path.",
    ),
) -> None:
    """Edit an entry's body, importance, or tags."""
    if body is None and importance is None and tags is None:
        raise typer.BadParameter(
            "Pass at least one of --body, --importance, --tags."
        )
    if importance is not None and not (1 <= importance <= 5):
        raise typer.BadParameter("--importance must be in 1..5")

    tag_list = None
    if tags is not None:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]

    backend = _open_backend(config_path)
    updated = backend.update(
        entry_id,
        body=body,
        importance=importance,
        tags=tag_list,
    )
    if updated is None:
        typer.echo(f"Entry {entry_id} not found.", err=True)
        raise typer.Exit(code=1)

    if json_output:
        _emit_json(_entry_to_dict(updated))
        return
    typer.echo(f"Updated entry {updated.entry_id}: importance={updated.importance}")


@memory_app.command("forget")
def memory_forget(
    entry_id: int = typer.Argument(..., help="Memory entry id."),
    yes: bool = typer.Option(False, "--yes", help="Skip interactive confirmation."),
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path.",
    ),
) -> None:
    """Hard-delete an entry. Requires --yes or interactive confirmation."""
    backend = _open_backend(config_path)
    entry = backend.read_entry(entry_id)
    if entry is None:
        typer.echo(f"Entry {entry_id} not found.", err=True)
        raise typer.Exit(code=1)

    if not yes:
        typer.echo(f"About to forget entry #{entry.entry_id} ({entry.type}): {entry.title}")
        confirm = typer.confirm("Delete this entry?", default=False)
        if not confirm:
            typer.echo("Aborted.")
            raise typer.Exit(code=1)

    removed = backend.forget(entry_id)
    if removed:
        typer.echo(f"Forgot entry #{entry_id}.")
    else:
        typer.echo(f"Could not forget entry #{entry_id}.", err=True)
        raise typer.Exit(code=1)


@memory_app.command("recall")
def memory_recall(
    query: str = typer.Argument(..., help="Free-text query."),
    scope: str | None = typer.Option(None, "--scope", help="Filter by scope."),
    type_: str | None = typer.Option(None, "--type", help="Filter by type."),
    importance_min: int = typer.Option(
        1, "--importance", help="Minimum importance (1-5).",
    ),
    limit: int = typer.Option(10, "--limit", help="Maximum rows to return."),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path.",
    ),
) -> None:
    """Run the recall API from the CLI (for diagnostics)."""
    if limit <= 0:
        raise typer.BadParameter("--limit must be >= 1")
    backend = _open_backend(config_path)
    results = backend.recall(
        query,
        scope=scope,
        types=[type_] if type_ else None,
        importance_min=importance_min,
        limit=limit,
    )
    if json_output:
        _emit_json([
            {
                **_entry_to_dict(r.entry),
                "score": round(r.score, 3),
                "match_rationale": r.match_rationale,
            }
            for r in results
        ])
        return
    if not results:
        typer.echo("No entries match.")
        return
    for r in results:
        typer.echo(
            f"{r.entry.entry_id:>5}  score={r.score:.2f}  "
            f"{r.entry.type:<9}  "
            f"i{r.entry.importance}  "
            f"scope={r.entry.scope:<16}  "
            f"{r.entry.title[:60]}"
        )


@memory_app.command("stats")
def memory_stats(
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path.",
    ),
) -> None:
    """Show aggregate memory counts by scope/type/importance/tier."""
    backend = _open_backend(config_path)
    stats = backend.stats()
    if json_output:
        _emit_json(stats)
        return
    typer.echo(f"Total entries: {stats['total']}")
    typer.echo(f"Superseded:    {stats['superseded']}")
    typer.echo(f"Expired:       {stats['expired']}")
    typer.echo("")
    typer.echo("By type:")
    for t, n in sorted((stats.get("by_type") or {}).items()):
        typer.echo(f"  {t:<12} {n}")
    typer.echo("")
    typer.echo("By scope tier:")
    for t, n in sorted((stats.get("by_tier") or {}).items()):
        typer.echo(f"  {t:<12} {n}")
    typer.echo("")
    typer.echo("By importance:")
    for level in range(1, 6):
        n = (stats.get("by_importance") or {}).get(str(level), 0)
        typer.echo(f"  {level}: {n}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _supersession_chain(
    backend: FileMemoryBackend,
    entry: MemoryEntry,
    *,
    max_depth: int = 12,
) -> list[MemoryEntry]:
    """Walk forwards through the supersession chain.

    Returns a list ordered oldest→newest. Bounded by ``max_depth`` so a
    cyclic graph (never produced by the write path but possible after
    manual editing) can't loop forever.
    """
    chain: list[MemoryEntry] = [entry]
    seen = {entry.entry_id}
    current = entry
    while current.superseded_by is not None and len(chain) < max_depth:
        successor = backend.read_entry(int(current.superseded_by))
        if successor is None or successor.entry_id in seen:
            break
        chain.append(successor)
        seen.add(successor.entry_id)
        current = successor
    return chain
