"""Tests for ``pollypm.providers.codex.CodexProvider`` (Phase C of #397).

Run with::

    HOME=/tmp/pytest-providers-codex uv run --with pytest \\
        pytest tests/providers/test_codex_provider.py -x

Exercises the adapter surface: Protocol satisfaction, delegation to
the sibling helpers, the entry-point registration, and the three-
question-rule error messages on the surfaces that are not yet wired.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from pollypm.acct import ProviderAdapter, get_provider
from pollypm.acct.model import AccountConfig
from pollypm.models import ProviderKind
from pollypm.providers.codex import CodexProvider


@pytest.fixture(autouse=True)
def _reset_registry_cache():
    """Clear the ``lru_cache`` so each test sees a fresh registry."""
    get_provider.cache_clear()
    yield
    get_provider.cache_clear()


def _fake_id_token(claims: dict[str, object]) -> str:
    def _b64(payload: dict[str, object] | str) -> str:
        data = (
            json.dumps(payload).encode("utf-8")
            if isinstance(payload, dict)
            else payload.encode("utf-8")
        )
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    return ".".join([_b64({"alg": "none"}), _b64(claims), _b64("sig")])


def _account(name: str, home: Path | None) -> AccountConfig:
    return AccountConfig(
        name=name,
        provider=ProviderKind.CODEX,
        email=None,
        home=home,
    )


def test_codex_provider_name_matches_entry_point() -> None:
    assert CodexProvider().name == "codex"


def test_codex_provider_satisfies_protocol() -> None:
    assert isinstance(CodexProvider(), ProviderAdapter)


def test_entry_point_resolves_to_codex_provider() -> None:
    """``get_provider("codex")`` returns a ``CodexProvider`` instance.

    This guards against the pyproject.toml entry point regressing back
    to the Phase A legacy adapter during a merge.
    """
    adapter = get_provider("codex")
    assert isinstance(adapter, CodexProvider)


def test_detect_logged_in_delegates_to_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "auth.json").write_text(
        json.dumps({"tokens": {"id_token": _fake_id_token({"email": "a@b.com"})}})
    )
    provider = CodexProvider()

    assert provider.detect_logged_in(_account("codex_1", home)) is True


def test_detect_logged_in_returns_false_for_unauthenticated_home(
    tmp_path: Path,
) -> None:
    provider = CodexProvider()

    assert provider.detect_logged_in(_account("codex_1", tmp_path / "empty")) is False


def test_detect_logged_in_returns_false_when_home_is_none() -> None:
    """Guards the null-home path — no filesystem work happens."""
    provider = CodexProvider()

    assert provider.detect_logged_in(_account("codex_1", None)) is False


def test_detect_email_returns_authenticated_email(tmp_path: Path) -> None:
    home = tmp_path / "home"
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "auth.json").write_text(
        json.dumps({"tokens": {"id_token": _fake_id_token({"email": "User@X.com"})}})
    )
    provider = CodexProvider()

    assert provider.detect_email(_account("codex_1", home)) == "user@x.com"


def test_detect_email_returns_none_when_home_is_none() -> None:
    provider = CodexProvider()

    assert provider.detect_email(_account("codex_1", None)) is None


def test_isolated_env_sets_codex_home_additively(tmp_path: Path) -> None:
    provider = CodexProvider()
    env = provider.isolated_env(tmp_path / "home")

    # Protocol contract: purely additive — no ambient env leaks in.
    assert env == {"CODEX_HOME": str(tmp_path / "home" / ".codex")}


def test_worker_launch_cmd_prepends_codex_binary() -> None:
    provider = CodexProvider()
    argv = provider.worker_launch_cmd(
        _account("codex_1", None),
        ["--resume", "foo"],
    )

    assert argv == ["codex", "--resume", "foo"]


def test_run_login_flow_rejects_none_home_with_three_question_error() -> None:
    """#240 three-question rule: what / why / how-to-fix in the message."""
    provider = CodexProvider()

    with pytest.raises(ValueError) as exc_info:
        provider.run_login_flow(_account("codex_missing_home", None))

    msg = str(exc_info.value)
    # What happened
    assert "'codex_missing_home'" in msg
    # Why it matters
    assert "Why:" in msg
    # How to fix
    assert "Fix:" in msg


def test_probe_usage_surfaces_not_implemented_with_fix_line() -> None:
    """Phase C leaves probe_usage stubbed — the error must point callers
    at the legacy API that still works."""
    provider = CodexProvider()

    with pytest.raises(NotImplementedError) as exc_info:
        provider.probe_usage(_account("codex_1", None))

    msg = str(exc_info.value)
    assert "Why:" in msg
    assert "Fix:" in msg
    assert "probe_account_usage" in msg


def test_legacy_codex_adapter_symbol_still_imports() -> None:
    """The old ``CodexAdapter`` launch-command class must still be
    importable at its historical path so the ``core_codex`` built-in
    plugin and the ``tests/test_runtime.py`` suite keep working."""
    from pollypm.providers.codex import CodexAdapter

    assert CodexAdapter().name == "codex"
    assert CodexAdapter().binary == "codex"
