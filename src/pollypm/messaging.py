from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import shutil
import re


OPEN_DIR = "00-open"
CLOSED_DIR = "01-closed"


@dataclass(slots=True)
class InboxMessage:
    path: Path
    subject: str
    sender: str
    created_at: str
    body: str


def inbox_root(root_dir: Path) -> Path:
    root_dir = root_dir.resolve()
    new_root = root_dir / "pollypm" / "inbox"
    old_root = root_dir / "pollypm" / "inbox"
    if old_root.exists() and not new_root.exists():
        new_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(old_root), str(new_root))
        try:
            if old_root.parent.exists() and not any(old_root.parent.iterdir()):
                old_root.parent.rmdir()
        except OSError:
            pass
    return new_root


def ensure_inbox(root_dir: Path) -> Path:
    root = inbox_root(root_dir)
    (root / OPEN_DIR).mkdir(parents=True, exist_ok=True)
    (root / CLOSED_DIR).mkdir(parents=True, exist_ok=True)
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


def list_closed_messages(root_dir: Path) -> list[InboxMessage]:
    root = ensure_inbox(root_dir)
    return _read_messages(root / CLOSED_DIR)


def close_message(root_dir: Path, name: str) -> Path:
    root = ensure_inbox(root_dir)
    source = root / OPEN_DIR / name
    if not source.exists():
        raise FileNotFoundError(name)
    target = root / CLOSED_DIR / source.name
    source.rename(target)
    return target


def _read_messages(directory: Path) -> list[InboxMessage]:
    items: list[InboxMessage] = []
    for path in sorted(directory.glob("*.md")):
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
        items.append(
            InboxMessage(
                path=path,
                subject=subject or path.stem,
                sender=sender or "unknown",
                created_at=created_at,
                body=body,
            )
        )
    return items


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-") or "message"
