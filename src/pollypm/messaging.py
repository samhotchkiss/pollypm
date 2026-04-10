from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import re
import shutil


OPEN_DIR = "open"
THREADS_DIR = "threads"
CLOSED_DIR = "closed"
MESSAGES_DIR = "messages"
STATE_FILE = "state.json"
HANDOFF_FILE = "handoff.json"
THREAD_STATES = ("open", "threaded", "waiting-on-pa", "waiting-on-pm", "resolved", "closed")
THREAD_STATE_INDEX = {state: index for index, state in enumerate(THREAD_STATES)}


@dataclass(slots=True)
class InboxMessage:
    path: Path
    subject: str
    sender: str
    created_at: str
    body: str


@dataclass(slots=True)
class InboxStateTransition:
    state: str
    actor: str
    at: str
    note: str = ""


@dataclass(slots=True)
class InboxThread:
    thread_id: str
    path: Path
    state: str
    subject: str
    sender: str
    created_at: str
    updated_at: str
    owner: str
    item_name: str
    message_paths: list[Path]
    transitions: list[InboxStateTransition]


def inbox_root(root_dir: Path) -> Path:
    root_dir = root_dir.resolve()
    new_root = root_dir / ".pollypm" / "inbox"
    legacy_root = root_dir / "pollypm" / "inbox"
    if legacy_root.exists() and not new_root.exists():
        new_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(legacy_root), str(new_root))
        try:
            if legacy_root.parent.exists() and not any(legacy_root.parent.iterdir()):
                legacy_root.parent.rmdir()
        except OSError:
            pass
    return new_root


def ensure_inbox(root_dir: Path) -> Path:
    root = inbox_root(root_dir)
    for name in (OPEN_DIR, THREADS_DIR, CLOSED_DIR):
        (root / name).mkdir(parents=True, exist_ok=True)
    gitignore_path = root.parent / ".gitignore"
    existing = gitignore_path.read_text().splitlines() if gitignore_path.exists() else []
    if "inbox/" not in existing:
        if existing and existing[-1] != "":
            existing.append("")
        existing.append("inbox/")
        gitignore_path.write_text("\n".join(existing).rstrip() + "\n")
    return root


def create_message(root_dir: Path, *, sender: str, subject: str, body: str) -> Path:
    root = ensure_inbox(root_dir)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    slug = _slugify(subject or "message")
    path = root / OPEN_DIR / f"{stamp}-{slug}.md"
    content = "\n".join(
        [
            f"Subject: {subject}",
            f"Sender: {sender}",
            f"Created-At: {datetime.now(UTC).isoformat()}",
            "",
            body.rstrip(),
            "",
        ]
    )
    path.write_text(content)
    return path


def list_open_messages(root_dir: Path) -> list[InboxMessage]:
    root = ensure_inbox(root_dir)
    return _read_messages(root / OPEN_DIR)


def close_message(root_dir: Path, name: str) -> Path:
    root = ensure_inbox(root_dir)
    source = root / OPEN_DIR / name
    if not source.exists():
        raise FileNotFoundError(name)
    target = root / CLOSED_DIR / source.name
    source.rename(target)
    return target


def create_thread(root_dir: Path, item_name: str, *, actor: str, owner: str = "pm") -> InboxThread:
    root = ensure_inbox(root_dir)
    source = root / OPEN_DIR / item_name
    if not source.exists():
        raise FileNotFoundError(item_name)
    message = _read_message(source)
    thread_id = source.stem
    thread_root = root / THREADS_DIR / thread_id
    messages_dir = thread_root / MESSAGES_DIR
    messages_dir.mkdir(parents=True, exist_ok=True)
    first_message = messages_dir / f"0001-{source.name}"
    source.rename(first_message)
    transition = _transition_record("threaded", actor, "")
    state = {
        "thread_id": thread_id,
        "item_name": item_name,
        "subject": message.subject,
        "sender": message.sender,
        "state": "threaded",
        "owner": owner,
        "created_at": message.created_at or _now(),
        "updated_at": transition["at"],
        "closed_at": None,
        "messages": [str(first_message.relative_to(thread_root))],
        "transitions": [transition],
    }
    handoff = {
        "owner": owner,
        "actor": actor,
        "updated_at": transition["at"],
        "note": "",
    }
    _write_json(thread_root / STATE_FILE, state)
    _write_json(thread_root / HANDOFF_FILE, handoff)
    return get_thread(root_dir, thread_id)


def list_threads(root_dir: Path, *, include_closed: bool = False) -> list[InboxThread]:
    root = ensure_inbox(root_dir)
    items: list[InboxThread] = []
    for directory in sorted((root / THREADS_DIR).iterdir()):
        if not directory.is_dir():
            continue
        thread = get_thread(root_dir, directory.name)
        if include_closed or thread.state != "closed":
            items.append(thread)
    return items


def get_thread(root_dir: Path, thread_id: str) -> InboxThread:
    root = ensure_inbox(root_dir)
    thread_root = _thread_dir(root, thread_id)
    state = _read_json(thread_root / STATE_FILE)
    messages = [
        thread_root / relative_path
        for relative_path in state.get("messages", [])
        if isinstance(relative_path, str)
    ]
    transitions = [
        InboxStateTransition(
            state=str(item.get("state", "")),
            actor=str(item.get("actor", "")),
            at=str(item.get("at", "")),
            note=str(item.get("note", "")),
        )
        for item in state.get("transitions", [])
        if isinstance(item, dict)
    ]
    return InboxThread(
        thread_id=thread_id,
        path=thread_root,
        state=str(state.get("state", "threaded")),
        subject=str(state.get("subject", thread_id)),
        sender=str(state.get("sender", "unknown")),
        created_at=str(state.get("created_at", "")),
        updated_at=str(state.get("updated_at", "")),
        owner=str(state.get("owner", "pm")),
        item_name=str(state.get("item_name", f"{thread_id}.md")),
        message_paths=messages,
        transitions=transitions,
    )


def transition_thread(
    root_dir: Path,
    thread_id: str,
    state: str,
    *,
    actor: str,
    note: str = "",
) -> InboxThread:
    if state not in THREAD_STATE_INDEX:
        raise ValueError(f"Unknown inbox state: {state}")
    root = ensure_inbox(root_dir)
    thread_root = _thread_dir(root, thread_id)
    payload = _read_json(thread_root / STATE_FILE)
    current_state = str(payload.get("state", "threaded"))
    _validate_transition(current_state, state)
    transition = _transition_record(state, actor, note)
    transitions = payload.get("transitions", [])
    if not isinstance(transitions, list):
        transitions = []
    transitions.append(transition)
    payload["transitions"] = transitions
    payload["state"] = state
    payload["updated_at"] = transition["at"]
    if state == "closed":
        payload["closed_at"] = transition["at"]
        closed_root = root / CLOSED_DIR / thread_id
        if closed_root.exists():
            shutil.rmtree(closed_root)
        closed_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(thread_root), str(closed_root))
        thread_root = closed_root
    _write_json(thread_root / STATE_FILE, payload)
    return get_thread(root_dir, thread_id)


def set_handoff(root_dir: Path, thread_id: str, *, owner: str, actor: str, note: str = "") -> Path:
    root = ensure_inbox(root_dir)
    thread_root = _thread_dir(root, thread_id)
    payload = {
        "owner": owner,
        "actor": actor,
        "updated_at": _now(),
        "note": note,
    }
    path = thread_root / HANDOFF_FILE
    _write_json(path, payload)
    state = _read_json(thread_root / STATE_FILE)
    state["owner"] = owner
    state["updated_at"] = payload["updated_at"]
    _write_json(thread_root / STATE_FILE, state)
    return path


def read_handoff(root_dir: Path, thread_id: str) -> dict[str, object]:
    root = ensure_inbox(root_dir)
    return _read_json(_thread_dir(root, thread_id) / HANDOFF_FILE)


def append_thread_message(root_dir: Path, thread_id: str, *, sender: str, subject: str, body: str) -> Path:
    root = ensure_inbox(root_dir)
    thread_root = _thread_dir(root, thread_id)
    messages_dir = thread_root / MESSAGES_DIR
    messages_dir.mkdir(parents=True, exist_ok=True)
    index = len(list(messages_dir.glob("*.md"))) + 1
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    slug = _slugify(subject or "message")
    path = messages_dir / f"{index:04d}-{stamp}-{slug}.md"
    content = "\n".join(
        [
            f"Subject: {subject}",
            f"Sender: {sender}",
            f"Created-At: {_now()}",
            "",
            body.rstrip(),
            "",
        ]
    )
    path.write_text(content)
    state = _read_json(thread_root / STATE_FILE)
    messages = state.get("messages", [])
    if not isinstance(messages, list):
        messages = []
    messages.append(str(path.relative_to(thread_root)))
    state["messages"] = messages
    state["updated_at"] = _now()
    _write_json(thread_root / STATE_FILE, state)
    return path


def list_closed_messages(root_dir: Path) -> list[InboxMessage]:
    root = ensure_inbox(root_dir)
    items: list[InboxMessage] = []
    for path in sorted((root / CLOSED_DIR).glob("*.md")):
        items.append(_read_message(path))
    return items


def _thread_dir(root: Path, thread_id: str) -> Path:
    active = root / THREADS_DIR / thread_id
    if active.exists():
        return active
    closed = root / CLOSED_DIR / thread_id
    if closed.exists():
        return closed
    raise FileNotFoundError(thread_id)


def _read_messages(directory: Path) -> list[InboxMessage]:
    items: list[InboxMessage] = []
    for path in sorted(directory.glob("*.md")):
        items.append(_read_message(path))
    return items


def _read_message(path: Path) -> InboxMessage:
    subject = ""
    sender = ""
    created_at = ""
    lines = path.read_text().splitlines()
    body_start = 0
    for index, line in enumerate(lines):
        if not line.strip():
            body_start = index + 1
            break
        if line.startswith("Subject: "):
            subject = line.removeprefix("Subject: ").strip()
        if line.startswith("Sender: "):
            sender = line.removeprefix("Sender: ").strip()
        if line.startswith("Created-At: "):
            created_at = line.removeprefix("Created-At: ").strip()
    body = "\n".join(lines[body_start:]).strip()
    return InboxMessage(
        path=path,
        subject=subject or path.stem,
        sender=sender or "unknown",
        created_at=created_at,
        body=body,
    )


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text()) if path.exists() else {}


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def _transition_record(state: str, actor: str, note: str) -> dict[str, object]:
    return {
        "state": state,
        "actor": actor,
        "at": _now(),
        "note": note,
    }


def _validate_transition(current_state: str, next_state: str) -> None:
    current_index = THREAD_STATE_INDEX.get(current_state)
    next_index = THREAD_STATE_INDEX.get(next_state)
    if current_index is None or next_index is None:
        raise ValueError(f"Unknown inbox state transition: {current_state} -> {next_state}")
    # Allow forward steps, PM/PA cycling (waiting-on-pa <-> waiting-on-pm),
    # and PM jumping to resolved/closed from any state
    if next_state in ("resolved", "closed"):
        return  # PM can always close or resolve
    if current_state == "waiting-on-pa" and next_state == "waiting-on-pm":
        return  # PA returns to PM
    if current_state == "waiting-on-pm" and next_state == "waiting-on-pa":
        return  # PM routes back to PA
    if next_index != current_index + 1:
        raise ValueError(f"Illegal inbox state transition: {current_state} -> {next_state}")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-") or "message"
