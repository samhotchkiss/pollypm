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
    to: str = ""  # recipient: "user", "polly", "worker_X"
    delivery_state: str = "pending"  # pending, delivered, failed, not_applicable
    last_delivered_at: str = ""
    parent_id: str = ""  # links to parent thread (e.g., user request that spawned this task)
    read: bool = False  # has the recipient read this message?
    project: str = ""
    path: Path = field(default_factory=lambda: Path("."))

    @property
    def name(self) -> str:
        """Backward-compatible alias for integrations that still expect .name."""
        return self.id


@dataclass(slots=True)
class MessageEntry:
    index: int
    sender: str
    timestamp: str
    body: str
    to: str = ""
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

def _default_recipient(sender: str) -> str:
    """Determine the default recipient based on who's sending."""
    if sender in ("user", "human"):
        return "polly"
    return "user"


def _append_audit(state: dict, action: str, by: str, **extra: str) -> None:
    """Append an entry to the message's audit trail."""
    audit = state.setdefault("audit", [])
    entry = {"at": datetime.now(UTC).isoformat(), "action": action, "by": by}
    entry.update(extra)
    audit.append(entry)


def create_message(
    project_root: Path,
    *,
    sender: str,
    subject: str,
    body: str,
    project: str = "",
    owner: str = "polly",
    to: str = "",
    parent_id: str = "",
) -> InboxMessage:
    """Create a new inbox message with context and history."""
    ts = datetime.now(UTC)
    msg_id = ts.strftime("%Y%m%dT%H%M%SZ") + "-" + _slugify(subject)
    msg_dir = _msg_dir(project_root, msg_id)
    msg_dir.mkdir(parents=True, exist_ok=True)

    # Compute recipient and delivery state
    recipient = to or _default_recipient(sender)
    delivery_state = "not_applicable" if recipient == "user" else "pending"

    # Write the first message
    msg_path = msg_dir / f"0001-{ts.strftime('%Y%m%dT%H%M%SZ')}.md"
    msg_path.write_text(
        f"From: {sender}\n"
        f"To: {recipient}\n"
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
    state: dict = {
        "id": msg_id,
        "subject": subject,
        "status": "open",
        "owner": owner,
        "sender": sender,
        "to": recipient,
        "delivery_state": delivery_state,
        "last_delivered_at": "",
        "parent_id": parent_id,
        "project": project,
        "created_at": ts.isoformat(),
        "updated_at": ts.isoformat(),
        "message_count": 1,
        "audit": [],
    }
    _append_audit(state, "created", by=sender, to=recipient)
    atomic_write_text(msg_dir / "state.json", json.dumps(state, indent=2) + "\n")

    # Sync to DB for efficient querying
    try:
        from pollypm.config import load_config, DEFAULT_CONFIG_PATH
        from pollypm.storage.state import StateStore
        config = load_config(DEFAULT_CONFIG_PATH)
        store = StateStore(config.project.state_db)
        store.upsert_inbox_message(
            id=msg_id, subject=subject, status="open", owner=owner,
            sender=sender, project=project, message_count=1,
            created_at=ts.isoformat(), updated_at=ts.isoformat(),
        )
        store.close()
    except Exception:  # noqa: BLE001
        pass  # DB sync is best-effort, files are source of truth

    return InboxMessage(
        id=msg_id, subject=subject, status="open", owner=owner,
        created_at=ts.isoformat(), updated_at=ts.isoformat(),
        message_count=1, sender=sender, to=recipient,
        delivery_state=delivery_state, parent_id=parent_id,
        project=project, path=msg_dir,
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

    # Compute recipient — reply flips direction
    recipient = _default_recipient(sender)
    delivery_state = "not_applicable" if recipient == "user" else "pending"

    # Append quality enforcement notes based on message direction
    quality_note = ""
    if recipient == "polly" and sender not in ("user", "human", "polly", "heartbeat", "system"):
        # Worker → Polly: review checklist
        quality_note = (
            "\n\n---\n"
            "**Review checklist (complete before notifying user):**\n"
            "- [ ] Did the worker actually produce anything? Check git log for commits, "
            "check for new/changed files. If nothing was produced, do NOT notify the user.\n"
            "- [ ] Does this meet the user's stated goal?\n"
            "- [ ] Is the work committed to git?\n"
            "- [ ] Is it deployed/live (if applicable)?\n"
            "- [ ] Are tests passing?\n"
            "- [ ] Quality bar: would the user say 'holy shit, that's done'?\n"
            "- [ ] Send notification with specifics: what changed, key commits, how to verify. "
            "`pm notify \"Done: ...\" \"...\" --to user`"
        )
    elif sender in ("polly",) and recipient not in ("user", "human", "polly", "heartbeat", "system"):
        # Polly → Worker: quality expectations
        quality_note = (
            "\n\n---\n"
            "**Before you report back:**\n"
            "- Do the whole thing. Don't stop at 80% and ask if you should continue.\n"
            "- Commit your work with a clear message.\n"
            "- Run tests. If they fail, fix them.\n"
            "- If this involves a deploy, deploy it.\n"
            "- Reply to this thread with what you did, what changed, and how to verify.\n"
            "- Use `pm reply <this_thread_id> 'your report'`"
        )

    # Write the reply message
    msg_path = msg_dir / f"{index:04d}-{ts.strftime('%Y%m%dT%H%M%SZ')}.md"
    msg_path.write_text(
        f"From: {sender}\n"
        f"To: {recipient}\n"
        f"Date: {ts.isoformat()}\n"
        f"Subject: Re: {state['subject']}\n\n"
        f"{body.rstrip()}{quality_note}\n"
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

    # Update state — flip owner, set new recipient and delivery state
    state["message_count"] = index
    state["updated_at"] = ts.isoformat()
    state["to"] = recipient
    state["delivery_state"] = delivery_state
    _append_audit(state, "replied", by=sender, to=recipient)
    state["last_delivered_at"] = ""
    if new_owner:
        state["owner"] = new_owner
    elif sender == "user":
        state["owner"] = "polly"
    elif sender in ("polly", "heartbeat", "system"):
        state["owner"] = "user"
    atomic_write_text(msg_dir / "state.json", json.dumps(state, indent=2) + "\n")

    # Sync to DB
    try:
        from pollypm.config import load_config, DEFAULT_CONFIG_PATH
        from pollypm.storage.state import StateStore
        config = load_config(DEFAULT_CONFIG_PATH)
        store = StateStore(config.project.state_db)
        store.upsert_inbox_message(
            id=state["id"], subject=state["subject"], status=state["status"],
            owner=state["owner"], sender=state["sender"],
            project=state.get("project", ""), message_count=index,
            updated_at=ts.isoformat(),
        )
        store.close()
    except Exception:  # noqa: BLE001
        pass

    return MessageEntry(
        index=index, sender=sender, timestamp=ts.isoformat(),
        body=body, path=msg_path,
    )


# ---------------------------------------------------------------------------
# Close
# ---------------------------------------------------------------------------

_ACK_PATTERNS = {
    "thank", "thanks", "thx", "ty", "ok", "okay", "k", "got it",
    "sounds good", "perfect", "great", "awesome", "cool", "nice",
    "noted", "acknowledged", "ack", "roger", "copy", "understood",
    "will do", "on it", "done", "yep", "yes", "yup", "sure",
}


def _is_simple_ack(text: str) -> bool:
    """Check if a message is a simple acknowledgment with no further action."""
    cleaned = text.strip().lower().rstrip(".!,")
    return cleaned in _ACK_PATTERNS or len(cleaned.split()) <= 3 and any(
        p in cleaned for p in _ACK_PATTERNS
    )


def _user_was_notified(project_root: Path, parent_msg_id: str) -> bool:
    """Check if a user-facing notification exists that references this thread.

    Looks for any message TO the user with this msg_id as parent_id,
    or whose subject contains the parent's subject.
    """
    try:
        parent_dir = _msg_dir(project_root, parent_msg_id)
        parent_state = json.loads((parent_dir / "state.json").read_text())
        parent_subject = parent_state.get("subject", "")
    except Exception:  # noqa: BLE001
        return False

    # Check all messages for one that notifies the user about this thread
    for msg in list_messages(project_root, status="all"):
        if msg.to == "user" and msg.id != parent_msg_id:
            # Check parent_id link
            try:
                msg_state = json.loads((_msg_dir(project_root, msg.id) / "state.json").read_text())
                if msg_state.get("parent_id") == parent_msg_id:
                    return True
            except Exception:  # noqa: BLE001
                pass
            # Check subject overlap (fuzzy match for related notifications)
            if parent_subject and parent_subject[:30].lower() in msg.subject.lower():
                return True
    return False


def close_message(
    project_root: Path,
    msg_id: str,
    *,
    sender: str,
    note: str,
) -> None:
    """Close a message with a required closing note.

    If an agent is closing a message where the user's last reply requested
    action, the agent MUST have already replied with what they did. The close
    note alone is not enough — the user needs a response in the thread.
    """
    msg_dir = _msg_dir(project_root, msg_id)
    if not msg_dir.exists():
        raise FileNotFoundError(f"Message not found: {msg_id}")

    # Enforce close guards for agents
    if sender not in ("user", "human"):
        _ctx, _hist, entries = read_message(project_root, msg_id)
        state = json.loads((msg_dir / "state.json").read_text())

        # Check if the user is involved in this thread
        user_involved = any(e.sender in ("user", "human") for e in entries)
        last_user_msg = None
        for entry in entries:
            if entry.sender in ("user", "human"):
                last_user_msg = entry

        if user_involved:
            # User is in this thread — they must ack before agent can close
            if last_user_msg and not _is_simple_ack(last_user_msg.body):
                raise ValueError(
                    f"Cannot close: only the user can archive this thread. "
                    f"Reply with `pm reply {msg_id} '<what you did>'` and the user will archive when ready."
                )
        else:
            # Agent-to-agent thread (e.g., Polly → worker)
            # Can only close if a user notification was sent about the outcome.
            # Check: is there an open or closed message TO the user that references this thread?
            user_notified = _user_was_notified(project_root, msg_id)
            senders_in_thread = {e.sender for e in entries}
            both_replied = len(senders_in_thread - {"system", "heartbeat"}) >= 2

            if both_replied and user_notified:
                pass  # Both agents agreed, user was notified — OK to close
            elif not user_notified:
                raise ValueError(
                    f"Cannot close: the user has not been notified of the outcome. "
                    f"Send `pm notify '<what was done>' '<details>' --to user` first, then close."
                )
            else:
                raise ValueError(
                    f"Cannot close: both agents involved must have replied before closing. "
                    f"The thread only has messages from: {', '.join(sorted(senders_in_thread))}"
                )

    # Add closing note as a message
    reply_to_message(project_root, msg_id, sender=sender, body=f"[Closed] {note}")

    # Update state
    state = json.loads((msg_dir / "state.json").read_text())
    state["status"] = "closed"
    ts = datetime.now(UTC).isoformat()
    state["updated_at"] = ts
    state["closed_at"] = ts
    _append_audit(state, "closed", by=sender, note=note[:100])
    atomic_write_text(msg_dir / "state.json", json.dumps(state, indent=2) + "\n")

    # Sync to DB
    try:
        from pollypm.config import load_config, DEFAULT_CONFIG_PATH
        from pollypm.storage.state import StateStore
        config = load_config(DEFAULT_CONFIG_PATH)
        store = StateStore(config.project.state_db)
        store.upsert_inbox_message(
            id=state["id"], subject=state["subject"], status="closed",
            owner=state.get("owner", ""), sender=state.get("sender", ""),
            project=state.get("project", ""), message_count=state.get("message_count", 1),
            updated_at=ts,
        )
        store.close()
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# List / Read
# ---------------------------------------------------------------------------

def list_messages(
    project_root: Path,
    *,
    status: str = "open",
    owner: str | None = None,
) -> list[InboxMessage]:
    """List messages by status and optionally filter by owner."""
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
            if status != "all" and state.get("status") != status:
                continue
            if owner is not None and state.get("owner") != owner:
                continue
            # Backward compat: derive 'to' from 'owner' if missing
            msg_to = state.get("to", "")
            if not msg_to:
                msg_to = state["owner"] if state["owner"] != "user" else "user"
            msg_ds = state.get("delivery_state", "pending" if msg_to != "user" else "not_applicable")
            messages.append(InboxMessage(
                id=state["id"],
                subject=state["subject"],
                status=state["status"],
                owner=state["owner"],
                created_at=state["created_at"],
                updated_at=state["updated_at"],
                message_count=state["message_count"],
                sender=state["sender"],
                to=msg_to,
                delivery_state=msg_ds,
                last_delivered_at=state.get("last_delivered_at", ""),
                parent_id=state.get("parent_id", ""),
                read=bool(state.get("read", False)),
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
        recipient = ""
        timestamp = ""
        body_lines: list[str] = []
        in_body = False
        for line in text.splitlines():
            if not in_body:
                if line.startswith("From: "):
                    sender = line[6:]
                elif line.startswith("To: "):
                    recipient = line[4:]
                elif line.startswith("Date: "):
                    timestamp = line[6:]
                elif line == "":
                    in_body = True
            else:
                body_lines.append(line)
        entries.append(MessageEntry(
            index=len(entries) + 1,
            sender=sender,
            to=recipient,
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

def mark_delivered(
    project_root: Path,
    msg_id: str,
    *,
    state: str = "delivered",
) -> None:
    """Update delivery_state and last_delivered_at in state.json."""
    msg_dir = _msg_dir(project_root, msg_id)
    state_path = msg_dir / "state.json"
    if not state_path.exists():
        return
    data = json.loads(state_path.read_text())
    data["delivery_state"] = state
    _append_audit(data, state, by="system", to=data.get("to", ""))
    if state == "delivered":
        data["last_delivered_at"] = datetime.now(UTC).isoformat()
    atomic_write_text(state_path, json.dumps(data, indent=2) + "\n")


def mark_read(project_root: Path, msg_id: str) -> None:
    """Mark a message as read."""
    msg_dir = _msg_dir(project_root, msg_id)
    state_path = msg_dir / "state.json"
    if not state_path.exists():
        return
    data = json.loads(state_path.read_text())
    if data.get("read"):
        return  # Already read
    data["read"] = True
    data["read_at"] = datetime.now(UTC).isoformat()
    _append_audit(data, "read", by=data.get("to", "user"))
    atomic_write_text(state_path, json.dumps(data, indent=2) + "\n")


def _slugify(text: str) -> str:
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
    return slug[:60] or "message"
