from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pollypm.config import load_config
from pollypm.models import AccountConfig, ProviderKind
from pollypm.providers import get_provider
from pollypm.storage.state import StateStore, TokenUsageHourlyRecord


@dataclass(slots=True)
class TranscriptTokenSample:
    session_name: str
    account_name: str
    provider: str
    model_name: str
    project_key: str
    cumulative_tokens: int
    observed_at: datetime
    source_path: Path


@dataclass(slots=True)
class TranscriptUsageEvent:
    observed_at: datetime
    model_name: str
    project_key: str
    tokens_used: int


@dataclass(slots=True)
class TranscriptScanResult:
    final_sample: TranscriptTokenSample
    usage_events: list[TranscriptUsageEvent]


def _parse_iso_timestamp(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _mtime_timestamp(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)


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
    if best_len >= 0:
        return best_key
    try:
        if current == config.project.root_dir.resolve() or current.is_relative_to(config.project.root_dir.resolve()):
            return config.project.name
    except ValueError:
        pass
    return config.project.name


def _scan_claude_transcript(path: Path, account_name: str, account: AccountConfig, config) -> TranscriptScanResult | None:
    session_name = path.stem
    observed_at: datetime | None = None
    model_name = "unknown"
    cwd: str | None = None
    cumulative = 0
    usage_events: list[TranscriptUsageEvent] = []

    try:
        for line in path.open():
            obj = json.loads(line)
            observed_at = _parse_iso_timestamp(obj.get("timestamp")) or observed_at
            if obj.get("type") != "assistant":
                continue
            message = obj.get("message") or {}
            if not isinstance(message, dict):
                continue
            session_name = str(obj.get("sessionId") or session_name)
            cwd = str(obj.get("cwd") or cwd or "")
            model = message.get("model")
            if isinstance(model, str) and model and model != "<synthetic>":
                model_name = model
            usage = message.get("usage") or {}
            if not isinstance(usage, dict):
                continue
            total = usage.get("total_tokens")
            if total is None:
                # Count actual tokens used, NOT cache reads (which are free/cheap)
                total = sum(
                    int(usage.get(key, 0) or 0)
                    for key in (
                        "input_tokens",
                        "output_tokens",
                        "cache_creation_input_tokens",
                        # cache_read_input_tokens excluded — cached reads are
                        # nearly free and inflate the count by 10-100x
                    )
                )
            tokens_used = int(total or 0)
            if tokens_used <= 0:
                continue
            cumulative += tokens_used
            point_time = observed_at or _mtime_timestamp(path)
            usage_events.append(
                TranscriptUsageEvent(
                    observed_at=point_time,
                    model_name=model_name,
                    project_key=_project_key_for_cwd(config, cwd),
                    tokens_used=tokens_used,
                )
            )
    except Exception:  # noqa: BLE001
        return None

    if not usage_events:
        return None
    final_event = usage_events[-1]
    return TranscriptScanResult(
        final_sample=TranscriptTokenSample(
            session_name=session_name,
            account_name=account_name,
            provider=account.provider.value,
            model_name=final_event.model_name,
            project_key=final_event.project_key,
            cumulative_tokens=cumulative,
            observed_at=final_event.observed_at,
            source_path=path,
        ),
        usage_events=usage_events,
    )


def _scan_codex_transcript(path: Path, account_name: str, account: AccountConfig, config) -> TranscriptScanResult | None:
    session_name = path.stem
    observed_at: datetime | None = None
    model_name = "unknown"
    cwd: str | None = None
    cumulative = 0
    usage_events: list[TranscriptUsageEvent] = []

    try:
        for line in path.open():
            obj = json.loads(line)
            observed_at = _parse_iso_timestamp(obj.get("timestamp")) or observed_at
            entry_type = obj.get("type")
            if entry_type == "session_meta":
                payload = obj.get("payload") or {}
                if isinstance(payload, dict):
                    session_name = str(payload.get("id") or session_name)
                    cwd = str(payload.get("cwd") or cwd or "")
                continue
            if entry_type == "turn_context":
                payload = obj.get("payload") or {}
                if isinstance(payload, dict):
                    cwd = str(payload.get("cwd") or cwd or "")
                    model = payload.get("model")
                    if isinstance(model, str) and model:
                        model_name = model
                continue
            if entry_type != "event_msg":
                continue
            payload = obj.get("payload") or {}
            if not isinstance(payload, dict) or payload.get("type") != "token_count":
                continue
            info = payload.get("info") or {}
            if not isinstance(info, dict):
                continue
            total_usage = info.get("total_token_usage") or {}
            last_usage = info.get("last_token_usage") or total_usage
            if not isinstance(total_usage, dict):
                total_usage = {}
            if not isinstance(last_usage, dict):
                last_usage = {}
            total_cumulative = total_usage.get("total_tokens")
            if total_cumulative is None:
                total_cumulative = sum(
                    int(total_usage.get(key, 0) or 0)
                    for key in (
                        "input_tokens",
                        "cached_input_tokens",
                        "output_tokens",
                        "reasoning_output_tokens",
                    )
                )
            tokens_used = last_usage.get("total_tokens")
            if tokens_used is None:
                tokens_used = sum(
                    int(last_usage.get(key, 0) or 0)
                    for key in (
                        "input_tokens",
                        "cached_input_tokens",
                        "output_tokens",
                        "reasoning_output_tokens",
                    )
                )
            tokens_used = int(tokens_used or 0)
            total_cumulative = int(total_cumulative or 0)
            if total_cumulative <= 0 or tokens_used <= 0:
                continue
            cumulative = max(cumulative, total_cumulative)
            point_time = observed_at or _mtime_timestamp(path)
            usage_events.append(
                TranscriptUsageEvent(
                    observed_at=point_time,
                    model_name=model_name,
                    project_key=_project_key_for_cwd(config, cwd),
                    tokens_used=tokens_used,
                )
            )
    except Exception:  # noqa: BLE001
        return None

    if not usage_events:
        return None
    final_event = usage_events[-1]
    return TranscriptScanResult(
        final_sample=TranscriptTokenSample(
            session_name=session_name,
            account_name=account_name,
            provider=account.provider.value,
            model_name=final_event.model_name,
            project_key=final_event.project_key,
            cumulative_tokens=cumulative,
            observed_at=final_event.observed_at,
            source_path=path,
        ),
        usage_events=usage_events,
    )


def _scan_account_transcripts(config, account_name: str) -> list[TranscriptScanResult]:
    account = config.accounts[account_name]
    provider = get_provider(account.provider, root_dir=config.project.root_dir)
    sources = provider.transcript_sources(account, None)
    results: list[TranscriptScanResult] = []
    for source in sources:
        if not source.root.exists():
            continue
        for path in sorted(source.root.rglob(source.pattern)):
            result = None
            if account.provider is ProviderKind.CLAUDE:
                result = _scan_claude_transcript(path, account_name, account, config)
            elif account.provider is ProviderKind.CODEX:
                result = _scan_codex_transcript(path, account_name, account, config)
            if result is not None:
                results.append(result)
    return results


def sync_token_ledger_for_config(config, *, account: str | None = None) -> list[TranscriptTokenSample]:
    store = StateStore(config.project.state_db)
    account_names = [account] if account else list(config.accounts)
    ingested: list[TranscriptTokenSample] = []
    rollups: dict[tuple[str, str, str, str, str], int] = {}
    updated_at = datetime.now(UTC).isoformat()

    for account_name in account_names:
        for result in _scan_account_transcripts(config, account_name):
            sample = result.final_sample
            store.upsert_token_sample(
                session_name=sample.session_name,
                account_name=sample.account_name,
                provider=sample.provider,
                model_name=sample.model_name,
                project_key=sample.project_key,
                cumulative_tokens=sample.cumulative_tokens,
                observed_at=sample.observed_at.isoformat(),
            )
            ingested.append(sample)
            for event in result.usage_events:
                hour_bucket = event.observed_at.astimezone(UTC).replace(minute=0, second=0, microsecond=0).isoformat()
                key = (
                    hour_bucket,
                    sample.account_name,
                    sample.provider,
                    event.model_name,
                    event.project_key,
                )
                rollups[key] = rollups.get(key, 0) + event.tokens_used

    store.replace_token_usage_hourly(
        [
            TokenUsageHourlyRecord(
                hour_bucket=hour_bucket,
                account_name=account_name,
                provider=provider,
                model_name=model_name,
                project_key=project_key,
                tokens_used=tokens_used,
                updated_at=updated_at,
            )
            for (hour_bucket, account_name, provider, model_name, project_key), tokens_used in sorted(rollups.items())
        ],
        account_names=account_names,
    )
    return ingested


def sync_token_ledger(config_path: Path, *, account: str | None = None) -> list[TranscriptTokenSample]:
    config = load_config(config_path)
    return sync_token_ledger_for_config(config, account=account)


def recent_token_usage(config_path: Path, *, limit: int = 24) -> list[TokenUsageHourlyRecord]:
    config = load_config(config_path)
    store = StateStore(config.project.state_db)
    return store.recent_token_usage(limit=limit)
