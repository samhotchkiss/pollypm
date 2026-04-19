"""Control-home/profile synchronization for control-plane sessions.

Contract:
- Inputs: account/session models plus the owning state store for runtime
  metadata writes.
- Outputs: synchronized control-home paths and effective account records.
- Side effects: creates directories, mirrors auth/config files, primes
  Claude homes, and updates account runtime metadata.
- Invariants: readonly supervisors never mutate homes; Claude keeps its
  real home for keychain-backed auth while Codex control sessions use a
  mirrored control-home.
- Allowed dependencies: onboarding helpers, config models, and the legacy
  state store. No tmux or launch-planner ownership.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pollypm.models import AccountConfig, ProjectSettings, ProviderKind, SessionConfig
from pollypm.onboarding import _prime_claude_home

if TYPE_CHECKING:
    from pollypm.storage.state import StateStore


@dataclass(slots=True)
class ControlHomeManager:
    """Own control-home sync and account runtime metadata refresh."""

    project: ProjectSettings
    readonly_state: bool
    control_roles: frozenset[str]
    control_homes_dir_name: str = "control-homes"

    def refresh_account_runtime_metadata(
        self,
        store: "StateStore",
        account_name: str,
        account: AccountConfig,
    ) -> None:
        access_expires_at: str | None = None
        refresh_available = False
        if account.provider is ProviderKind.CLAUDE and account.home is not None:
            credentials_path = account.home / ".claude" / ".credentials.json"
            if credentials_path.exists():
                try:
                    data = json.loads(credentials_path.read_text())
                    oauth = data.get("claudeAiOauth", {})
                    expires_at = oauth.get("expiresAt")
                    if isinstance(expires_at, (int, float)):
                        access_expires_at = datetime.fromtimestamp(
                            expires_at / 1000,
                            UTC,
                        ).isoformat()
                    refresh_available = bool(oauth.get("refreshToken"))
                except Exception:  # noqa: BLE001
                    access_expires_at = None
        store.upsert_account_runtime(
            account_name=account_name,
            provider=account.provider.value,
            status="healthy",
            reason="local auth metadata loaded",
            access_expires_at=access_expires_at,
            refresh_available=refresh_available,
        )

    def control_home(self, session_name: str) -> Path:
        return self.project.base_dir / self.control_homes_dir_name / session_name

    def effective_account(
        self,
        session: SessionConfig,
        account: AccountConfig,
    ) -> AccountConfig:
        if session.role not in self.control_roles or account.home is None:
            return account
        if self.readonly_state:
            return account
        if account.provider is ProviderKind.CLAUDE:
            _prime_claude_home(account.home)
            return account
        control_home = self.sync_control_home(account, session.name)
        return replace(account, home=control_home)

    def sync_control_home(self, account: AccountConfig, session_name: str) -> Path:
        if account.home is None:
            raise RuntimeError(f"Account {account.name} has no home configured")
        source_home = account.home
        target_home = self.control_home(session_name)
        target_home.mkdir(parents=True, exist_ok=True, mode=0o700)

        if account.provider is ProviderKind.CLAUDE:
            self._sync_file(source_home / ".claude.json", target_home / ".claude.json")
            self._sync_file(
                source_home / ".claude" / ".credentials.json",
                target_home / ".claude" / ".credentials.json",
            )
            self._sync_file(
                source_home / ".claude" / "settings.json",
                target_home / ".claude" / "settings.json",
            )
            _prime_claude_home(target_home)
        elif account.provider is ProviderKind.CODEX:
            self._sync_file(
                source_home / ".codex" / ".codex-global-state.json",
                target_home / ".codex" / ".codex-global-state.json",
            )
            self._sync_file(
                source_home / ".codex" / "auth.json",
                target_home / ".codex" / "auth.json",
            )
            self._sync_file(
                source_home / ".codex" / "config.toml",
                target_home / ".codex" / "config.toml",
            )

        return target_home

    def _sync_file(self, source: Path, target: Path) -> None:
        if source.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        elif target.exists() and not source.exists():
            source.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, source)
