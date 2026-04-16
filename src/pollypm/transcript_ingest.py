from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pollypm.models import AccountConfig, ProviderKind
from pollypm.plugin_host import extension_host_for_root
from pollypm.providers import get_provider
from pollypm.provider_sdk import TranscriptSource
from pollypm.projects import project_transcripts_dir, session_scoped_dir


POLL_INTERVAL_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 30.0
_INGESTORS: dict[Path, "TranscriptIngestor"] = {}
_INGESTORS_LOCK = threading.Lock()
logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TranscriptFileCursor:
    offset: int = 0
    session_id: str | None = None
    cwd: str | None = None
    model_name: str | None = None


@dataclass(slots=True)
class TranscriptCursorState:
    files: dict[str, TranscriptFileCursor] = field(default_factory=dict)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _parse_iso_timestamp(raw: str | None, *, fallback: Path | None = None) -> str:
    if raw:
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)
            return value.isoformat().replace("+00:00", "Z")
        except ValueError:
            pass
    if fallback is not None and fallback.exists():
        return datetime.fromtimestamp(fallback.stat().st_mtime, tz=UTC).isoformat().replace("+00:00", "Z")
    return _utc_now()


def _extract_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
            elif isinstance(item, str) and item:
                parts.append(item)
        return "\n".join(parts).strip()
    if isinstance(value, dict):
        text = value.get("text")
        if isinstance(text, str):
            return text
    return ""


def _project_key_for_cwd(config, cwd: str | None) -> str:
    if not cwd:
        return config.project.name
    current = Path(cwd).resolve()
    best_key = config.project.name
    best_len = -1
    for key, project in config.projects.items():
        project_path = project.path.resolve()
        try:
            if current == project_path or current.is_relative_to(project_path):
                length = len(str(project_path))
                if length > best_len:
                    best_key = key
                    best_len = length
        except ValueError:
            continue
    return best_key


def _project_root_for_key(config, project_key: str) -> Path:
    project = config.projects.get(project_key)
    if project is not None:
        return project.path
    return config.project.root_dir


def _transcript_root(project_root: Path) -> Path:
    return project_transcripts_dir(project_root)


def _cursor_state_path(config) -> Path:
    path = _transcript_root(config.project.root_dir) / ".ingestion-state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_cursor_state(config) -> TranscriptCursorState:
    path = _cursor_state_path(config)
    if not path.exists():
        return TranscriptCursorState()
    try:
        raw = json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        return TranscriptCursorState()
    files_raw = raw.get("files", {}) if isinstance(raw, dict) else {}
    files: dict[str, TranscriptFileCursor] = {}
    if isinstance(files_raw, dict):
        for file_path, payload in files_raw.items():
            if not isinstance(payload, dict):
                continue
            files[file_path] = TranscriptFileCursor(
                offset=int(payload.get("offset", 0) or 0),
                session_id=payload.get("session_id"),
                cwd=payload.get("cwd"),
                model_name=payload.get("model_name"),
            )
    return TranscriptCursorState(files=files)


def _save_cursor_state(config, state: TranscriptCursorState) -> None:
    path = _cursor_state_path(config)
    payload = {
        "files": {
            file_path: {
                "offset": cursor.offset,
                "session_id": cursor.session_id,
                "cwd": cursor.cwd,
                "model_name": cursor.model_name,
            }
            for file_path, cursor in state.files.items()
        }
    }
    from pollypm.atomic_io import atomic_write_text
    atomic_write_text(path, json.dumps(payload, indent=2) + "\n")


def _append_event(config, event: dict[str, Any]) -> None:
    session_id = str(event["session_id"])
    project_root = _project_root_for_key(config, str(event["project_key"]))
    output_root = session_scoped_dir(_transcript_root(project_root), session_id)
    # Skip session lock — the ingestor runs in a single dedicated thread so
    # there is no concurrent-write risk, and locking here crashes when another
    # live session already holds the lock on this directory.
    output_path = output_root / "events.jsonl"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, separators=(",", ":")) + "\n")


def _event_base(
    *,
    event_type: str,
    session_id: str,
    account_name: str,
    provider: str,
    project_key: str,
    timestamp: str,
    source_path: Path,
    source_offset: int,
    cwd: str | None,
    model_name: str | None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "timestamp": timestamp,
        "event_type": event_type,
        "session_id": session_id,
        "account_name": account_name,
        "provider": provider,
        "project_key": project_key,
        "source_path": str(source_path),
        "source_offset": source_offset,
    }
    if cwd:
        event["cwd"] = cwd
    if model_name:
        event["model_name"] = model_name
    if payload:
        event["payload"] = payload
    return event


def _normalize_claude_line(
    obj: dict[str, Any],
    *,
    account_name: str,
    account: AccountConfig,
    config,
    source_path: Path,
    source_offset: int,
    cursor: TranscriptFileCursor,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    session_id = str(obj.get("sessionId") or cursor.session_id or source_path.stem)
    cursor.session_id = session_id
    cwd = str(obj.get("cwd") or cursor.cwd or "")
    cursor.cwd = cwd
    timestamp = _parse_iso_timestamp(obj.get("timestamp"), fallback=source_path)
    project_key = _project_key_for_cwd(config, cwd)
    message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
    content = message.get("content")
    model_name = message.get("model") if isinstance(message.get("model"), str) else cursor.model_name
    cursor.model_name = model_name
    line_type = obj.get("type")

    if line_type == "user":
        text = _extract_text(content or message)
        if text:
            events.append(
                _event_base(
                    event_type="user_turn",
                    session_id=session_id,
                    account_name=account_name,
                    provider=account.provider.value,
                    project_key=project_key,
                    timestamp=timestamp,
                    source_path=source_path,
                    source_offset=source_offset,
                    cwd=cwd,
                    model_name=model_name,
                    payload={"text": text},
                )
            )
    elif line_type == "assistant":
        text = _extract_text(content or message)
        if text:
            events.append(
                _event_base(
                    event_type="assistant_turn",
                    session_id=session_id,
                    account_name=account_name,
                    provider=account.provider.value,
                    project_key=project_key,
                    timestamp=timestamp,
                    source_path=source_path,
                    source_offset=source_offset,
                    cwd=cwd,
                    model_name=model_name,
                    payload={"text": text},
                )
            )
        usage = message.get("usage")
        if isinstance(usage, dict):
            total_tokens = usage.get("total_tokens")
            if total_tokens is None:
                total_tokens = sum(
                    int(usage.get(key, 0) or 0)
                    for key in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
                )
            if int(total_tokens or 0) > 0:
                events.append(
                    _event_base(
                        event_type="token_usage",
                        session_id=session_id,
                        account_name=account_name,
                        provider=account.provider.value,
                        project_key=project_key,
                        timestamp=timestamp,
                        source_path=source_path,
                        source_offset=source_offset,
                        cwd=cwd,
                        model_name=model_name,
                        payload={"usage": usage, "total_tokens": int(total_tokens)},
                    )
                )
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "tool_use":
                    events.append(
                        _event_base(
                            event_type="tool_call",
                            session_id=session_id,
                            account_name=account_name,
                            provider=account.provider.value,
                            project_key=project_key,
                            timestamp=timestamp,
                            source_path=source_path,
                            source_offset=source_offset,
                            cwd=cwd,
                            model_name=model_name,
                            payload=item,
                        )
                    )
                elif item.get("type") == "tool_result":
                    events.append(
                        _event_base(
                            event_type="tool_result",
                            session_id=session_id,
                            account_name=account_name,
                            provider=account.provider.value,
                            project_key=project_key,
                            timestamp=timestamp,
                            source_path=source_path,
                            source_offset=source_offset,
                            cwd=cwd,
                            model_name=model_name,
                            payload=item,
                        )
                    )
    elif line_type == "error":
        events.append(
            _event_base(
                event_type="error",
                session_id=session_id,
                account_name=account_name,
                provider=account.provider.value,
                project_key=project_key,
                timestamp=timestamp,
                source_path=source_path,
                source_offset=source_offset,
                cwd=cwd,
                model_name=model_name,
                payload={"error": obj.get("error") or message or obj},
            )
        )
    return events


def _normalize_codex_line(
    obj: dict[str, Any],
    *,
    account_name: str,
    account: AccountConfig,
    config,
    source_path: Path,
    source_offset: int,
    cursor: TranscriptFileCursor,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    timestamp = _parse_iso_timestamp(obj.get("timestamp"), fallback=source_path)
    entry_type = obj.get("type")
    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}

    if entry_type == "session_meta":
        cursor.session_id = str(payload.get("id") or cursor.session_id or source_path.stem)
        cursor.cwd = str(payload.get("cwd") or cursor.cwd or "")
        events.append(
            _event_base(
                event_type="session_state",
                session_id=cursor.session_id,
                account_name=account_name,
                provider=account.provider.value,
                project_key=_project_key_for_cwd(config, cursor.cwd),
                timestamp=timestamp,
                source_path=source_path,
                source_offset=source_offset,
                cwd=cursor.cwd,
                model_name=cursor.model_name,
                payload={"state": "started"},
            )
        )
        return events

    if entry_type == "turn_context":
        cursor.cwd = str(payload.get("cwd") or cursor.cwd or "")
        model_name = payload.get("model")
        if isinstance(model_name, str) and model_name:
            cursor.model_name = model_name
        return events

    if entry_type != "event_msg":
        return events

    session_id = cursor.session_id or source_path.stem
    cwd = cursor.cwd
    project_key = _project_key_for_cwd(config, cwd)
    model_name = cursor.model_name
    payload_type = payload.get("type")

    if payload_type == "token_count":
        info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
        total_usage = info.get("total_token_usage") if isinstance(info.get("total_token_usage"), dict) else {}
        last_usage = info.get("last_token_usage") if isinstance(info.get("last_token_usage"), dict) else total_usage
        total_tokens = last_usage.get("total_tokens")
        if total_tokens is None:
            total_tokens = sum(
                int(last_usage.get(key, 0) or 0)
                for key in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
            )
        if int(total_tokens or 0) > 0:
            events.append(
                _event_base(
                    event_type="token_usage",
                    session_id=session_id,
                    account_name=account_name,
                    provider=account.provider.value,
                    project_key=project_key,
                    timestamp=timestamp,
                    source_path=source_path,
                    source_offset=source_offset,
                    cwd=cwd,
                    model_name=model_name,
                    payload={"info": info, "total_tokens": int(total_tokens)},
                )
            )
    elif payload_type == "user_message":
        text = _extract_text(payload.get("text") or payload.get("message") or payload)
        if text:
            events.append(
                _event_base(
                    event_type="user_turn",
                    session_id=session_id,
                    account_name=account_name,
                    provider=account.provider.value,
                    project_key=project_key,
                    timestamp=timestamp,
                    source_path=source_path,
                    source_offset=source_offset,
                    cwd=cwd,
                    model_name=model_name,
                    payload={"text": text},
                )
            )
    elif payload_type == "assistant_message":
        text = _extract_text(payload.get("text") or payload.get("message") or payload)
        if text:
            events.append(
                _event_base(
                    event_type="assistant_turn",
                    session_id=session_id,
                    account_name=account_name,
                    provider=account.provider.value,
                    project_key=project_key,
                    timestamp=timestamp,
                    source_path=source_path,
                    source_offset=source_offset,
                    cwd=cwd,
                    model_name=model_name,
                    payload={"text": text},
                )
            )
    elif payload_type == "tool_call":
        events.append(
            _event_base(
                event_type="tool_call",
                session_id=session_id,
                account_name=account_name,
                provider=account.provider.value,
                project_key=project_key,
                timestamp=timestamp,
                source_path=source_path,
                source_offset=source_offset,
                cwd=cwd,
                model_name=model_name,
                payload=payload,
            )
        )
    elif payload_type == "tool_result":
        events.append(
            _event_base(
                event_type="tool_result",
                session_id=session_id,
                account_name=account_name,
                provider=account.provider.value,
                project_key=project_key,
                timestamp=timestamp,
                source_path=source_path,
                source_offset=source_offset,
                cwd=cwd,
                model_name=model_name,
                payload=payload,
            )
        )
    elif payload_type == "turn_end":
        events.append(
            _event_base(
                event_type="turn_end",
                session_id=session_id,
                account_name=account_name,
                provider=account.provider.value,
                project_key=project_key,
                timestamp=timestamp,
                source_path=source_path,
                source_offset=source_offset,
                cwd=cwd,
                model_name=model_name,
                payload=payload,
            )
        )
    elif payload_type == "error":
        events.append(
            _event_base(
                event_type="error",
                session_id=session_id,
                account_name=account_name,
                provider=account.provider.value,
                project_key=project_key,
                timestamp=timestamp,
                source_path=source_path,
                source_offset=source_offset,
                cwd=cwd,
                model_name=model_name,
                payload=payload,
            )
        )
    return events


def _scan_source(config, account_name: str, account: AccountConfig, source: TranscriptSource, state: TranscriptCursorState) -> None:
    if not source.root.exists():
        return
    for path in sorted(source.root.rglob(source.pattern)):
        file_key = str(path.resolve())
        cursor = state.files.setdefault(file_key, TranscriptFileCursor())
        size = path.stat().st_size
        if size < cursor.offset:
            cursor.offset = 0
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            handle.seek(cursor.offset)
            while True:
                start_offset = handle.tell()
                line = handle.readline()
                if not line:
                    break
                cursor.offset = handle.tell()
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if account.provider is ProviderKind.CLAUDE:
                    events = _normalize_claude_line(
                        obj,
                        account_name=account_name,
                        account=account,
                        config=config,
                        source_path=path,
                        source_offset=start_offset,
                        cursor=cursor,
                    )
                elif account.provider is ProviderKind.CODEX:
                    events = _normalize_codex_line(
                        obj,
                        account_name=account_name,
                        account=account,
                        config=config,
                        source_path=path,
                        source_offset=start_offset,
                        cursor=cursor,
                    )
                else:
                    events = []
                for event in events:
                    _append_event(config, event)


def _iter_sources_for_account(config, account_name: str, account: AccountConfig):
    """Yield (source, account_name, account) for every transcript source that
    applies to this account, combining provider-supplied sources (legacy path)
    with plugin-registered producers (plugin_host path)."""

    # Provider-native sources (existing behavior)
    provider = get_provider(account.provider, root_dir=config.project.root_dir)
    for source in provider.transcript_sources(account, None):
        yield source

    # Plugin-registered transcript sources — producers receive the account
    # and must return an iterable of TranscriptSource. They may also filter
    # by provider themselves. Failures are logged, not fatal.
    host = extension_host_for_root(str(config.project.root_dir.resolve()))
    try:
        producers = host.iter_transcript_sources(account=account, config=config)
    except Exception as exc:  # noqa: BLE001
        logger.debug("transcript_source plugin enumeration failed: %s", exc)
        producers = []
    for _name, produced in producers:
        if produced is None:
            continue
        if isinstance(produced, TranscriptSource):
            yield produced
            continue
        try:
            for item in produced:
                if isinstance(item, TranscriptSource):
                    yield item
        except TypeError:
            # Not iterable — ignore silently; factory returned something else.
            continue


def sync_transcripts_once(config) -> None:
    # File lock prevents concurrent heartbeat cron processes from racing
    lock_path = _cursor_state_path(config).with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    import fcntl
    try:
        with open(lock_path, "w") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                state = _load_cursor_state(config)
                for account_name, account in config.accounts.items():
                    for source in _iter_sources_for_account(config, account_name, account):
                        _scan_source(config, account_name, account, source, state)
                _save_cursor_state(config, state)
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    except BlockingIOError:
        pass  # Another process holds the lock — skip this cycle


class TranscriptIngestor:
    def __init__(self, config) -> None:
        self.config = config
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="pollypm-transcript-ingest", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        backoff_seconds = POLL_INTERVAL_SECONDS
        while not self._stop.wait(POLL_INTERVAL_SECONDS):
            try:
                sync_transcripts_once(self.config)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Transcript ingestion sync failed: %s", exc)
                if self._stop.wait(backoff_seconds):
                    break
                backoff_seconds = min(backoff_seconds * 2, MAX_BACKOFF_SECONDS)
                continue
            backoff_seconds = POLL_INTERVAL_SECONDS

    def stop(self) -> None:
        self._stop.set()


def start_transcript_ingestion(config):
    key = config.project.base_dir.resolve()
    with _INGESTORS_LOCK:
        # Evict stopped ingestors so the dict doesn't grow unbounded
        stale = [k for k, v in _INGESTORS.items() if v._stop.is_set()]
        for k in stale:
            del _INGESTORS[k]
        ingestor = _INGESTORS.get(key)
        if ingestor is None:
            ingestor = TranscriptIngestor(config)
            _INGESTORS[key] = ingestor
        ingestor.start()
        return ingestor
