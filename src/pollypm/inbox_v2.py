"""Inbox v2 — threaded messages with context and history.

Each message is a folder:
  .pollypm/inbox/messages/<id>/
    context.md    — project context, related files, commits
    history.md    — conversation summary (updated on each message)
    0001-<ts>.md  — first message
    0002-<ts>.md  — reply
    ...

The agent reads context.md + history.md (token-efficient) and only
drills into individual messages when needed.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from pollypm.atomic_io import atomic_write_text


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class InboxMessage:
    id: str
    subject: str
    status: str  # open, waiting, resolved, closed
    owner: str   # who owes the next response: "user", "polly", "worker_X"
    created_at: str
    updated_at: str
    message_count: int
    sender: str
    project: str = ""
    path: Path = field(default_factory=lambda: Path("."))


@dataclass(slots=True)
class MessageEntry:
    index: int
    sender: str
    timestamp: str
    body: str
    path: Path = field(default_factory=lambda: Path("."))


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _inbox_root(project_root: Path) -> Path:
    root = project_root / ".pollypm" / "inbox" / "messages"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _msg_dir(project_root: Path, msg_id: str) -> Path:
    return _inbox_root(project_root) / msg_id


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

def create_message(
    project_root: Path,
    *,
    sender: str,
    subject: str,
    body: str,
    project: str = "",
    owner: str = "polly",
) -> InboxMessage:
    """Create a new inbox message with context and history."""
    ts = datetime.now(UTC)
    msg_id = ts.strftime("%Y%m%dT%H%M%SZ") + "-" + _slugify(subject)
    msg_dir = _msg_dir(project_root, msg_id)
    msg_dir.mkdir(parents=True, exist_ok=True)

    # Write the first message
    msg_path = msg_dir / f"0001-{ts.strftime('%Y%m%dT%H%M%SZ')}.md"
    msg_path.write_text(
        f"From: {sender}\n"
        f"Date: {ts.isoformat()}\n"
        f"Subject: {subject}\n\n"
        f"{body.rstrip()}\n"
    )

    # Generate context.md
    context = _generate_context(project_root, subject=subject, project=project)
    atomic_write_text(msg_dir / "context.md", context)

    # Generate initial history.md
    history = _render_history([
        {"index": 1, "sender": sender, "timestamp": ts.isoformat(), "summary": body[:200]},
    ])
    atomic_write_text(msg_dir / "history.md", history)

    # Write state
    state = {
        "id": msg_id,
        "subject": subject,
        "status": "open",
        "owner": owner,
        "sender": sender,
        "project": project,
        "created_at": ts.isoformat(),
        "updated_at": ts.isoformat(),
        "message_count": 1,
    }
    atomic_write_text(msg_dir / "state.json", json.dumps(state, indent=2) + "\n")

    return InboxMessage(
        id=msg_id, subject=subject, status="open", owner=owner,
        created_at=ts.isoformat(), updated_at=ts.isoformat(),
        message_count=1, sender=sender, project=project, path=msg_dir,
    )


# ---------------------------------------------------------------------------
# Reply
# ---------------------------------------------------------------------------

def reply_to_message(
    project_root: Path,
    msg_id: str,
    *,
    sender: str,
    body: str,
    new_owner: str | None = None,
    context_update: str = "",
) -> MessageEntry:
    """Add a reply to an existing message thread.

    The caller MUST provide context_update if the reply changes the
    state of the work (e.g., "tests now passing", "deployed to staging").
    """
    msg_dir = _msg_dir(project_root, msg_id)
    if not msg_dir.exists():
        raise FileNotFoundError(f"Message not found: {msg_id}")

    state = json.loads((msg_dir / "state.json").read_text())
    ts = datetime.now(UTC)
    index = state["message_count"] + 1

    # Write the reply message
    msg_path = msg_dir / f"{index:04d}-{ts.strftime('%Y%m%dT%H%M%SZ')}.md"
    msg_path.write_text(
        f"From: {sender}\n"
        f"Date: {ts.isoformat()}\n"
        f"Subject: Re: {state['subject']}\n\n"
        f"{body.rstrip()}\n"
    )

    # Update history.md
    existing_history = (msg_dir / "history.md").read_text() if (msg_dir / "history.md").exists() else ""
    history_entries = _parse_history_entries(existing_history)
    history_entries.append({
        "index": index, "sender": sender,
        "timestamp": ts.isoformat(), "summary": body[:200],
    })
    atomic_write_text(msg_dir / "history.md", _render_history(history_entries))

    # Update context.md if provided
    if context_update:
        existing_context = (msg_dir / "context.md").read_text() if (msg_dir / "context.md").exists() else ""
        updated_context = existing_context.rstrip() + f"\n\n## Update ({ts.strftime('%Y-%m-%d %H:%M')})\n{context_update}\n"
        atomic_write_text(msg_dir / "context.md", updated_context)

    # Update state
    state["message_count"] = index
    state["updated_at"] = ts.isoformat()
    if new_owner:
        state["owner"] = new_owner
    elif sender == "user":
        state["owner"] = "polly"  # User replied, now Polly owes a response
    elif sender in ("polly", "heartbeat", "system"):
        state["owner"] = "user"  # Agent replied, ball is in user's court
    atomic_write_text(msg_dir / "state.json", json.dumps(state, indent=2) + "\n")

    return MessageEntry(
        index=index, sender=sender, timestamp=ts.isoformat(),
        body=body, path=msg_path,
    )


# ---------------------------------------------------------------------------
# Close
# ---------------------------------------------------------------------------

def close_message(
    project_root: Path,
    msg_id: str,
    *,
    sender: str,
    note: str,
) -> None:
    """Close a message with a required closing note."""
    msg_dir = _msg_dir(project_root, msg_id)
    if not msg_dir.exists():
        raise FileNotFoundError(f"Message not found: {msg_id}")

    # Add closing note as a message
    reply_to_message(project_root, msg_id, sender=sender, body=f"[Closed] {note}")

    # Update state
    state = json.loads((msg_dir / "state.json").read_text())
    state["status"] = "closed"
    state["updated_at"] = datetime.now(UTC).isoformat()
    atomic_write_text(msg_dir / "state.json", json.dumps(state, indent=2) + "\n")


# ---------------------------------------------------------------------------
# List / Read
# ---------------------------------------------------------------------------

def list_messages(project_root: Path, *, status: str = "open") -> list[InboxMessage]:
    """List messages by status."""
    root = _inbox_root(project_root)
    messages: list[InboxMessage] = []
    for msg_dir in sorted(root.iterdir(), reverse=True):
        if not msg_dir.is_dir():
            continue
        state_path = msg_dir / "state.json"
        if not state_path.exists():
            continue
        try:
            state = json.loads(state_path.read_text())
            if status == "all" or state.get("status") == status:
                messages.append(InboxMessage(
                    id=state["id"],
                    subject=state["subject"],
                    status=state["status"],
                    owner=state["owner"],
                    created_at=state["created_at"],
                    updated_at=state["updated_at"],
                    message_count=state["message_count"],
                    sender=state["sender"],
                    project=state.get("project", ""),
                    path=msg_dir,
                ))
        except (json.JSONDecodeError, KeyError):
            continue
    return messages


def read_message(project_root: Path, msg_id: str) -> tuple[str, str, list[MessageEntry]]:
    """Read a message: returns (context, history, entries)."""
    msg_dir = _msg_dir(project_root, msg_id)
    if not msg_dir.exists():
        raise FileNotFoundError(f"Message not found: {msg_id}")

    context = (msg_dir / "context.md").read_text() if (msg_dir / "context.md").exists() else ""
    history = (msg_dir / "history.md").read_text() if (msg_dir / "history.md").exists() else ""

    entries: list[MessageEntry] = []
    for msg_file in sorted(msg_dir.glob("[0-9]*.md")):
        text = msg_file.read_text()
        sender = ""
        timestamp = ""
        body_lines: list[str] = []
        in_body = False
        for line in text.splitlines():
            if not in_body:
                if line.startswith("From: "):
                    sender = line[6:]
                elif line.startswith("Date: "):
                    timestamp = line[6:]
                elif line == "":
                    in_body = True
            else:
                body_lines.append(line)
        entries.append(MessageEntry(
            index=len(entries) + 1,
            sender=sender,
            timestamp=timestamp,
            body="\n".join(body_lines).strip(),
            path=msg_file,
        ))

    return context, history, entries


def find_message(project_root: Path, query: str) -> InboxMessage | None:
    """Find a message by partial ID or subject match."""
    for msg in list_messages(project_root, status="all"):
        if query.lower() in msg.id.lower() or query.lower() in msg.subject.lower():
            return msg
    return None


# ---------------------------------------------------------------------------
# Context generation
# ---------------------------------------------------------------------------

def _generate_context(project_root: Path, *, subject: str, project: str) -> str:
    """Generate context.md for a new message."""
    lines = [
        f"# Context: {subject}",
        "",
    ]

    # Project info
    if project:
        project_path = project_root if not project else None
        # Try to find the project path from config
        try:
            from pollypm.config import load_config, DEFAULT_CONFIG_PATH
            config = load_config(DEFAULT_CONFIG_PATH)
            proj = config.projects.get(project)
            if proj:
                project_path = proj.path
                lines.extend([
                    f"## Project: {proj.display_label()}",
                    f"Path: {proj.path}",
                    "",
                ])
        except Exception:  # noqa: BLE001
            pass

        # Recent commits
        if project_path and (project_path / ".git").exists():
            try:
                result = subprocess.run(
                    ["git", "log", "--oneline", "-5"],
                    cwd=project_path, capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0 and result.stdout.strip():
                    lines.extend(["## Recent Commits", ""])
                    for commit_line in result.stdout.strip().splitlines():
                        lines.append(f"- {commit_line}")
                    lines.append("")
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass

    lines.extend([
        "## Related",
        "",
        f"Created: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
    ])

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# History rendering
# ---------------------------------------------------------------------------

def _render_history(entries: list[dict]) -> str:
    """Render history.md from entry dicts."""
    lines = ["# Conversation History", ""]
    for entry in entries:
        sender = entry.get("sender", "?")
        ts = entry.get("timestamp", "")[:16]
        summary = entry.get("summary", "")
        lines.append(f"**{sender}** ({ts}): {summary}")
        lines.append("")
    return "\n".join(lines)


def _parse_history_entries(content: str) -> list[dict]:
    """Parse existing history.md back into entry dicts."""
    import re
    entries: list[dict] = []
    # Match: **sender** (timestamp): summary
    pattern = re.compile(r"\*\*(.+?)\*\*\s*\(([^)]*)\):\s*(.*)")
    for line in content.splitlines():
        m = pattern.match(line)
        if m:
            entries.append({
                "sender": m.group(1).strip(),
                "timestamp": m.group(2).strip(),
                "summary": m.group(3).strip(),
                "index": len(entries) + 1,
            })
    return entries


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slugify(text: str) -> str:
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
    return slug[:60] or "message"
