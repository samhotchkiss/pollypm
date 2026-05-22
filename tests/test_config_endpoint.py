"""Tests for the Phase 2 read-only config endpoints (surface §13).

Covers ``GET /api/v1/config`` and ``GET /api/v1/config/projects/{key}``:

- happy path returns the full redacted config
- credential-shaped fields (``auth_token``, ``api_key``, account
  ``env`` secrets) are replaced with ``"***"``
- per-project filter returns the right block; unknown key → 404
- auth required on both endpoints
- minimal config (no projects, no sessions) doesn't crash

The test is self-contained: it builds a :class:`PollyPMConfig`
in-memory and constructs the FastAPI app via
:func:`pollypm.web_api.create_app`. Run with
``pytest --noconftest tests/test_config_endpoint.py -v --timeout=120``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pollypm.config import (
    AccountConfig,
    MemorySettings,
    PollyPMConfig,
    PollyPMSettings,
    ProjectSettings,
)
from pollypm.models import (
    KnownProject,
    PgStorageSettings,
    ProjectKind,
    ProviderKind,
    RuntimeKind,
    SessionConfig,
    StorageSettings,
)
from pollypm.web_api import create_app, ensure_token
from pollypm.web_api.routes.config import REDACTED


# ---------------------------------------------------------------------------
# Local fixtures (no conftest — runner uses --noconftest)
# ---------------------------------------------------------------------------


def _build_config(
    *,
    workspace_root: Path,
    project_root: Path,
    include_session_with_token: bool = True,
    include_account_env_secret: bool = True,
    projects: dict[str, KnownProject] | None = None,
) -> PollyPMConfig:
    base_dir = workspace_root / ".pollypm"
    base_dir.mkdir(parents=True, exist_ok=True)
    state_db = base_dir / "state.db"

    accounts: dict[str, AccountConfig] = {
        "codex_primary": AccountConfig(
            name="codex_primary",
            provider=ProviderKind.CODEX,
            email="codex@example.com",
            runtime=RuntimeKind.LOCAL,
            home=base_dir / "homes" / "codex_primary",
            env=(
                {
                    "OPENAI_API_KEY": "sk-super-secret-12345",
                    "PUBLIC_FLAG": "ok",
                }
                if include_account_env_secret
                else {}
            ),
        ),
    }

    sessions: dict[str, SessionConfig] = {}
    if include_session_with_token:
        sessions["operator"] = SessionConfig(
            name="operator",
            role="operator",
            provider=ProviderKind.CODEX,
            account="codex_primary",
            cwd=workspace_root,
            project="myproj",
            auth_token="deadbeef" * 8,  # 64-char hex; should be redacted
        )

    if projects is None:
        projects = {
            "myproj": KnownProject(
                key="myproj",
                path=project_root,
                name="My Project",
                tracked=True,
                kind=ProjectKind.GIT,
            ),
        }

    return PollyPMConfig(
        project=ProjectSettings(
            name="PollyPM",
            root_dir=workspace_root,
            tmux_session="pollypm-test",
            workspace_root=workspace_root,
            base_dir=base_dir,
            logs_dir=base_dir / "logs",
            snapshots_dir=base_dir / "snapshots",
            state_db=state_db,
        ),
        pollypm=PollyPMSettings(
            controller_account="codex_primary",
            open_permissions_by_default=False,
            failover_enabled=False,
            failover_accounts=[],
            heartbeat_backend="local",
            scheduler_backend="inline",
            lease_timeout_minutes=30,
        ),
        accounts=accounts,
        sessions=sessions,
        projects=projects,
        memory=MemorySettings(backend="file"),
    )


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "myproj"
    root.mkdir()
    (root / ".pollypm").mkdir()
    return root


@pytest.fixture
def token_path(tmp_path: Path) -> Path:
    return tmp_path / "api-token"


@pytest.fixture
def token(token_path: Path) -> str:
    value, _ = ensure_token(token_path)
    return value


@pytest.fixture
def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def api_config(workspace_root: Path, project_root: Path) -> PollyPMConfig:
    return _build_config(workspace_root=workspace_root, project_root=project_root)


@pytest.fixture
def client(api_config: PollyPMConfig, token_path: Path, token: str) -> TestClient:
    _ = token  # ensure the token file exists before the app reads it
    app = create_app(config=api_config, token_path=token_path)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_get_config_happy_path_returns_full_config(client: TestClient, auth_headers: dict[str, str]) -> None:
    """``GET /api/v1/config`` returns the loaded config envelope."""
    response = client.get("/api/v1/config", headers=auth_headers)
    assert response.status_code == 200

    body = response.json()
    assert "config" in body, body
    cfg = body["config"]

    # Sanity: the obvious top-level fields are present.
    assert cfg["project"]["name"] == "PollyPM"
    assert cfg["project"]["tmux_session"] == "pollypm-test"
    assert "myproj" in cfg["projects"]
    assert cfg["projects"]["myproj"]["name"] == "My Project"
    # Enum (StrEnum) → ``.value``; not the repr.
    assert cfg["projects"]["myproj"]["kind"] == "git"


def test_get_config_paths_coerced_to_strings(client: TestClient, auth_headers: dict[str, str], workspace_root: Path) -> None:
    """``pathlib.Path`` values flatten to strings so the body is JSON-safe."""
    body = client.get("/api/v1/config", headers=auth_headers).json()
    root_dir = body["config"]["project"]["root_dir"]
    assert isinstance(root_dir, str)
    assert root_dir == str(workspace_root)


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def test_session_auth_token_is_redacted(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Session ``auth_token`` must never round-trip through the API."""
    body = client.get("/api/v1/config", headers=auth_headers).json()
    sessions = body["config"]["sessions"]
    assert "operator" in sessions
    assert sessions["operator"]["auth_token"] == REDACTED
    # The literal token value must not appear anywhere in the response.
    assert "deadbeef" not in response_text(body)


def test_account_env_api_key_is_redacted(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Account ``env`` entries whose keys look secret are redacted.

    The redaction walks every nested dict; an account's ``env`` map
    holds the OS-level credential exports, so ``OPENAI_API_KEY`` must
    flatten to ``"***"`` even though it's two levels deep.
    """
    body = client.get("/api/v1/config", headers=auth_headers).json()
    env = body["config"]["accounts"]["codex_primary"]["env"]
    assert env["OPENAI_API_KEY"] == REDACTED
    # Non-secret env keys pass through unchanged.
    assert env["PUBLIC_FLAG"] == "ok"
    assert "sk-super-secret-12345" not in response_text(body)


def test_empty_auth_token_not_overwritten(
    workspace_root: Path,
    project_root: Path,
    token_path: Path,
    token: str,
    auth_headers: dict[str, str],
) -> None:
    """Legacy ``auth_token=""`` must stay empty (not become ``"***"``).

    Sessions written before Lever 2 (#2012) hold an empty auth_token;
    redacting that to ``"***"`` would misrepresent the on-disk state
    (the operator can no longer tell "field unset" from "field hidden").
    """
    _ = token
    config = _build_config(
        workspace_root=workspace_root,
        project_root=project_root,
        include_session_with_token=False,
    )
    # Add a session with an explicitly-empty auth_token.
    config.sessions["legacy"] = SessionConfig(
        name="legacy",
        role="operator",
        provider=ProviderKind.CODEX,
        account="codex_primary",
        cwd=workspace_root,
        project="myproj",
        auth_token="",
    )
    app = create_app(config=config, token_path=token_path)
    with TestClient(app) as client:
        body = client.get("/api/v1/config", headers=auth_headers).json()
    assert body["config"]["sessions"]["legacy"]["auth_token"] == ""


# ---------------------------------------------------------------------------
# Per-project filter
# ---------------------------------------------------------------------------


def test_project_filter_returns_single_block(client: TestClient, auth_headers: dict[str, str]) -> None:
    """``GET /config/projects/{key}`` returns just that project's block."""
    response = client.get("/api/v1/config/projects/myproj", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["key"] == "myproj"
    assert body["project"]["name"] == "My Project"
    assert body["project"]["kind"] == "git"
    assert body["project"]["tracked"] is True


def test_project_filter_unknown_key_returns_404(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Unknown project key → 404 ``not_found`` with the spec envelope."""
    response = client.get("/api/v1/config/projects/does-not-exist", headers=auth_headers)
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "not_found"
    assert "does-not-exist" in body["error"]["message"]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_get_config_requires_auth(client: TestClient) -> None:
    """No bearer → 401 (matches the rest of the API surface)."""
    response = client.get("/api/v1/config")
    assert response.status_code == 401


def test_get_project_config_requires_auth(client: TestClient) -> None:
    response = client.get("/api/v1/config/projects/myproj")
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_minimal_config_does_not_crash(
    workspace_root: Path,
    token_path: Path,
    token: str,
    auth_headers: dict[str, str],
) -> None:
    """Empty projects + empty sessions still serialises cleanly.

    Fresh installs (or test fixtures that skip account setup) shouldn't
    crash the endpoint just because dict-valued fields are empty.
    """
    _ = token
    base_dir = workspace_root / ".pollypm"
    base_dir.mkdir(parents=True, exist_ok=True)
    config = PollyPMConfig(
        project=ProjectSettings(
            name="PollyPM",
            root_dir=workspace_root,
            tmux_session="pollypm-min",
            workspace_root=workspace_root,
            base_dir=base_dir,
            logs_dir=base_dir / "logs",
            snapshots_dir=base_dir / "snapshots",
            state_db=base_dir / "state.db",
        ),
        pollypm=PollyPMSettings(controller_account="codex_primary"),
        accounts={},
        sessions={},
        projects={},
        memory=MemorySettings(backend="file"),
    )
    app = create_app(config=config, token_path=token_path)
    with TestClient(app) as client:
        response = client.get("/api/v1/config", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["config"]["projects"] == {}
    assert body["config"]["sessions"] == {}
    assert body["config"]["accounts"] == {}


def test_known_project_credentials_redacted_in_per_project_view(
    workspace_root: Path,
    project_root: Path,
    token_path: Path,
    token: str,
    auth_headers: dict[str, str],
) -> None:
    """Per-project view also redacts credential-shaped fields.

    ``KnownProject`` doesn't carry secrets today, but the per-project
    serialiser shares the redaction walker with the full-config one —
    this test pins the contract so a future credential-shaped field on
    ``KnownProject`` can't accidentally leak.
    """
    _ = token
    config = _build_config(
        workspace_root=workspace_root,
        project_root=project_root,
        projects={
            "myproj": KnownProject(
                key="myproj",
                path=project_root,
                name="My Project",
                tracked=True,
                kind=ProjectKind.GIT,
            ),
        },
    )
    app = create_app(config=config, token_path=token_path)
    with TestClient(app) as client:
        body = client.get(
            "/api/v1/config/projects/myproj", headers=auth_headers,
        ).json()
    # Every key that looks credential-shaped must be ``"***"``; nothing
    # in the canonical KnownProject shape should match today, so the
    # block stays unredacted — but the walker still runs.
    for key, value in body["project"].items():
        if any(needle in key.lower() for needle in ("token", "secret", "api_key")):
            assert value == REDACTED, f"unredacted credential-shaped field: {key}"


# ---------------------------------------------------------------------------
# DSN / URL value-shape redaction (Codex P0 on PR #2056)
# ---------------------------------------------------------------------------


def test_storage_url_with_userinfo_is_redacted(
    workspace_root: Path,
    project_root: Path,
    token_path: Path,
    token: str,
    auth_headers: dict[str, str],
) -> None:
    """``[storage].url`` carrying userinfo must be redacted.

    The key ``url`` doesn't match the secret-name heuristic, so this
    pins the value-shape check: anything that parses as
    ``scheme://user:password@host/...`` is treated as a live
    credential regardless of the key it sits under.
    """
    _ = token
    config = _build_config(workspace_root=workspace_root, project_root=project_root)
    config.storage = StorageSettings(
        backend="postgres",
        url="postgresql://user:urlpass@example.com/db",
    )
    app = create_app(config=config, token_path=token_path)
    with TestClient(app) as client:
        body = client.get("/api/v1/config", headers=auth_headers).json()
    assert body["config"]["storage"]["url"] == REDACTED
    assert "urlpass" not in response_text(body)


def test_storage_pg_dsn_with_userinfo_is_redacted(
    workspace_root: Path,
    project_root: Path,
    token_path: Path,
    token: str,
    auth_headers: dict[str, str],
) -> None:
    """``[storage.pg].dsn`` carrying userinfo must be redacted."""
    _ = token
    config = _build_config(workspace_root=workspace_root, project_root=project_root)
    config.storage = StorageSettings(
        backend="postgres",
        url="",
        pg=PgStorageSettings(
            dsn="postgresql://user:dsnpass@example.com/db",
        ),
    )
    app = create_app(config=config, token_path=token_path)
    with TestClient(app) as client:
        body = client.get("/api/v1/config", headers=auth_headers).json()
    assert body["config"]["storage"]["pg"]["dsn"] == REDACTED
    assert "dsnpass" not in response_text(body)


def test_account_env_dsn_carriers_are_redacted(
    workspace_root: Path,
    project_root: Path,
    token_path: Path,
    token: str,
    auth_headers: dict[str, str],
) -> None:
    """Env entries with DSN/URL-shaped values must be redacted.

    Codex's P0 called out ``DATABASE_URL``, ``POSTGRES_DSN``, and
    ``SENTRY_DSN`` specifically — none of those keys match the
    secret-name heuristic, so the value-shape check (URL with
    userinfo) is what protects them.
    """
    _ = token
    config = _build_config(
        workspace_root=workspace_root,
        project_root=project_root,
        include_account_env_secret=False,
    )
    config.accounts["codex_primary"].env = {
        "DATABASE_URL": "postgresql://user:dbpass@example.com/db",
        "POSTGRES_DSN": "postgresql://user:pgdsnpass@example.com/db",
        "SENTRY_DSN": "https://user:sentrypass@sentry.example.com/123",
        "PUBLIC_FLAG": "ok",
    }
    app = create_app(config=config, token_path=token_path)
    with TestClient(app) as client:
        body = client.get("/api/v1/config", headers=auth_headers).json()
    env = body["config"]["accounts"]["codex_primary"]["env"]
    assert env["DATABASE_URL"] == REDACTED
    assert env["POSTGRES_DSN"] == REDACTED
    assert env["SENTRY_DSN"] == REDACTED
    assert env["PUBLIC_FLAG"] == "ok"  # non-URL string untouched
    raw = response_text(body)
    assert "dbpass" not in raw
    assert "pgdsnpass" not in raw
    assert "sentrypass" not in raw


def test_bare_url_without_userinfo_is_not_redacted(
    workspace_root: Path,
    project_root: Path,
    token_path: Path,
    token: str,
    auth_headers: dict[str, str],
) -> None:
    """Public URLs (no userinfo) pass through unchanged.

    Over-redaction would hurt observability — a webhook callback URL
    or a public docs link has no userinfo and should not be replaced
    with ``"***"``.
    """
    _ = token
    config = _build_config(
        workspace_root=workspace_root,
        project_root=project_root,
        include_account_env_secret=False,
    )
    config.accounts["codex_primary"].env = {
        "DOCS_URL": "https://example.com/api",
        "API_BASE": "https://api.example.com/v1",
    }
    app = create_app(config=config, token_path=token_path)
    with TestClient(app) as client:
        body = client.get("/api/v1/config", headers=auth_headers).json()
    env = body["config"]["accounts"]["codex_primary"]["env"]
    assert env["DOCS_URL"] == "https://example.com/api"
    assert env["API_BASE"] == "https://api.example.com/v1"


def test_non_url_string_in_safe_field_is_not_redacted(
    client: TestClient, auth_headers: dict[str, str],
) -> None:
    """Plain string values in non-secret fields remain visible.

    The value-shape check only fires for URL-with-userinfo. Ordinary
    string fields (project name, tmux session name, …) must round-trip
    untouched so the operator can read their config back from the API.
    """
    body = client.get("/api/v1/config", headers=auth_headers).json()
    assert body["config"]["project"]["tmux_session"] == "pollypm-test"
    assert body["config"]["project"]["name"] == "PollyPM"


# ---------------------------------------------------------------------------
# Env-credential value-shape redaction (Codex round-2 P0 on PR #2056)
# ---------------------------------------------------------------------------


def test_aws_access_key_id_is_redacted(
    workspace_root: Path,
    project_root: Path,
    token_path: Path,
    token: str,
    auth_headers: dict[str, str],
) -> None:
    """``AWS_ACCESS_KEY_ID`` env entries must be redacted.

    The key name lacks ``token``/``secret``/``api_key`` so the
    original keyword heuristic missed it. Both the expanded keyword
    set (``access_key``) AND the value-shape regex (``^AKIA…``)
    cover this — either alone suffices, both together is defence in
    depth.
    """
    _ = token
    config = _build_config(
        workspace_root=workspace_root,
        project_root=project_root,
        include_account_env_secret=False,
    )
    config.accounts["codex_primary"].env = {
        "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
        "PUBLIC_FLAG": "ok",
    }
    app = create_app(config=config, token_path=token_path)
    with TestClient(app) as client:
        body = client.get("/api/v1/config", headers=auth_headers).json()
    env = body["config"]["accounts"]["codex_primary"]["env"]
    assert env["AWS_ACCESS_KEY_ID"] == REDACTED
    assert env["PUBLIC_FLAG"] == "ok"
    assert "AKIAIOSFODNN7EXAMPLE" not in response_text(body)


def test_github_pat_is_redacted(
    workspace_root: Path,
    project_root: Path,
    token_path: Path,
    token: str,
    auth_headers: dict[str, str],
) -> None:
    """``GITHUB_PAT`` env entries must be redacted.

    Key name lacks the original credential keywords; matched by the
    new ``pat`` keyword AND by the ``^ghp_…`` value-shape regex.
    """
    _ = token
    config = _build_config(
        workspace_root=workspace_root,
        project_root=project_root,
        include_account_env_secret=False,
    )
    config.accounts["codex_primary"].env = {
        "GITHUB_PAT": "ghp_" + "a" * 36,
        "PUBLIC_FLAG": "ok",
    }
    app = create_app(config=config, token_path=token_path)
    with TestClient(app) as client:
        body = client.get("/api/v1/config", headers=auth_headers).json()
    env = body["config"]["accounts"]["codex_primary"]["env"]
    assert env["GITHUB_PAT"] == REDACTED
    assert "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" not in response_text(body)


def test_aws_secret_access_key_is_redacted(
    workspace_root: Path,
    project_root: Path,
    token_path: Path,
    token: str,
    auth_headers: dict[str, str],
) -> None:
    """``AWS_SECRET_ACCESS_KEY`` must be redacted by the secret heuristic."""
    _ = token
    config = _build_config(
        workspace_root=workspace_root,
        project_root=project_root,
        include_account_env_secret=False,
    )
    config.accounts["codex_primary"].env = {
        "AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "PUBLIC_FLAG": "ok",
    }
    app = create_app(config=config, token_path=token_path)
    with TestClient(app) as client:
        body = client.get("/api/v1/config", headers=auth_headers).json()
    env = body["config"]["accounts"]["codex_primary"]["env"]
    assert env["AWS_SECRET_ACCESS_KEY"] == REDACTED
    assert "wJalrXUtnFEMI" not in response_text(body)


def test_value_shape_redaction_for_bare_credential_value(
    workspace_root: Path,
    project_root: Path,
    token_path: Path,
    token: str,
    auth_headers: dict[str, str],
) -> None:
    """Credential value-shape catches even fully-innocuous env key names.

    If an operator names their env entry ``MY_KEY`` (no credential
    keyword anywhere) but the value matches the AWS / GitHub /
    Slack format, the value-shape backstop still redacts it.
    """
    _ = token
    config = _build_config(
        workspace_root=workspace_root,
        project_root=project_root,
        include_account_env_secret=False,
    )
    # AWS-shaped value (16 caps after AKIA) and a GitHub PAT shape —
    # the env key name has no credential keyword, so only the
    # value-shape backstop fires. Avoid Slack ``xox*`` literals here so
    # GitHub push-protection's secret scanner doesn't reject the test
    # for matching its Slack-token rule.
    config.accounts["codex_primary"].env = {
        "MY_ID": "AKIA" + "B" * 16,
        "WEBHOOK": "ghp_" + "z" * 36,
    }
    app = create_app(config=config, token_path=token_path)
    with TestClient(app) as client:
        body = client.get("/api/v1/config", headers=auth_headers).json()
    env = body["config"]["accounts"]["codex_primary"]["env"]
    assert env["MY_ID"] == REDACTED
    assert env["WEBHOOK"] == REDACTED


# ---------------------------------------------------------------------------
# PAT detection boundary (Codex round-4 P0 on PR #2056)
# ---------------------------------------------------------------------------


def test_path_keys_are_not_redacted(
    workspace_root: Path,
    project_root: Path,
    token_path: Path,
    token: str,
    auth_headers: dict[str, str],
) -> None:
    """Ordinary path-shaped keys must NOT be redacted by PAT detection.

    Codex round-4 P0 on PR #2056 caught the prior raw ``pat`` substring
    over-redacting ``path`` / ``project_path`` / ``config_path``. These
    keys are core operator-visible config (project location, etc.) and
    leaking PAT detection onto them broke the read-only config contract.
    """
    _ = token
    config = _build_config(
        workspace_root=workspace_root,
        project_root=project_root,
        include_account_env_secret=False,
    )
    # Stash a few innocuous path-shaped entries on the env map; the
    # walker will see them at the same nesting depth as a real PAT.
    config.accounts["codex_primary"].env = {
        "path": "/tmp/project",
        "project_path": "/tmp/project",
        "config_path": "/tmp/pollypm.toml",
        "PUBLIC_FLAG": "ok",
    }
    app = create_app(config=config, token_path=token_path)
    with TestClient(app) as client:
        body = client.get("/api/v1/config", headers=auth_headers).json()
    env = body["config"]["accounts"]["codex_primary"]["env"]
    assert env["path"] == "/tmp/project"
    assert env["project_path"] == "/tmp/project"
    assert env["config_path"] == "/tmp/pollypm.toml"
    assert env["PUBLIC_FLAG"] == "ok"


def test_github_pat_still_redacted_by_name(
    workspace_root: Path,
    project_root: Path,
    token_path: Path,
    token: str,
    auth_headers: dict[str, str],
) -> None:
    """``GITHUB_PAT`` must still redact via exact name match.

    Pins that the round-4 boundary fix didn't regress the round-2 PAT
    coverage. Uses a value that doesn't match the ``^ghp_…`` regex so
    only the name-side path can carry the redaction.
    """
    _ = token
    config = _build_config(
        workspace_root=workspace_root,
        project_root=project_root,
        include_account_env_secret=False,
    )
    # Value-shape regex deliberately won't match (no ``ghp_`` prefix);
    # only the exact-name check protects this entry.
    config.accounts["codex_primary"].env = {
        "GITHUB_PAT": "not-shaped-like-a-real-pat-but-still-a-secret",
    }
    app = create_app(config=config, token_path=token_path)
    with TestClient(app) as client:
        body = client.get("/api/v1/config", headers=auth_headers).json()
    env = body["config"]["accounts"]["codex_primary"]["env"]
    assert env["GITHUB_PAT"] == REDACTED
    assert "not-shaped-like-a-real-pat-but-still-a-secret" not in response_text(body)


def test_provider_pat_suffixes_redacted(
    workspace_root: Path,
    project_root: Path,
    token_path: Path,
    token: str,
    auth_headers: dict[str, str],
) -> None:
    """Generic ``*_PAT`` keys redact via suffix match.

    The round-4 fix replaced the raw ``pat`` substring with a
    boundary-aware ``_PAT`` suffix check. Any provider-prefixed PAT
    name (``GITLAB_PAT``, ``BITBUCKET_PAT``, … or a custom
    ``WHATEVER_PAT``) must still redact even though it's not in the
    exact-name allowlist.
    """
    _ = token
    config = _build_config(
        workspace_root=workspace_root,
        project_root=project_root,
        include_account_env_secret=False,
    )
    config.accounts["codex_primary"].env = {
        "WHATEVER_PAT": "opaque-token-value-12345",
        "GITLAB_PAT": "glpat-deadbeefcafebabe",
    }
    app = create_app(config=config, token_path=token_path)
    with TestClient(app) as client:
        body = client.get("/api/v1/config", headers=auth_headers).json()
    env = body["config"]["accounts"]["codex_primary"]["env"]
    assert env["WHATEVER_PAT"] == REDACTED
    assert env["GITLAB_PAT"] == REDACTED
    raw = response_text(body)
    assert "opaque-token-value-12345" not in raw
    assert "glpat-deadbeefcafebabe" not in raw


# ---------------------------------------------------------------------------
# DSN-with-query-string-password redaction (Codex round-2 P0 on PR #2056)
# ---------------------------------------------------------------------------


def test_storage_pg_dsn_with_query_password_is_redacted(
    workspace_root: Path,
    project_root: Path,
    token_path: Path,
    token: str,
    auth_headers: dict[str, str],
) -> None:
    """DSN with password in query string (not userinfo) must be redacted.

    Codex round-2 P0 example:
    ``postgresql://db.example/pollypm?user=alice&password=secretpw``.
    The previous userinfo-only check missed this shape entirely.
    Belt-and-suspenders: the ``dsn`` keyword now also triggers
    ``_looks_secret`` so the key-name match also catches it.
    """
    _ = token
    config = _build_config(workspace_root=workspace_root, project_root=project_root)
    config.storage = StorageSettings(
        backend="postgres",
        url="",
        pg=PgStorageSettings(
            dsn="postgresql://db.example/pollypm?user=alice&password=secretpw",
        ),
    )
    app = create_app(config=config, token_path=token_path)
    with TestClient(app) as client:
        body = client.get("/api/v1/config", headers=auth_headers).json()
    assert body["config"]["storage"]["pg"]["dsn"] == REDACTED
    assert "secretpw" not in response_text(body)


def test_env_dsn_with_query_password_is_redacted(
    workspace_root: Path,
    project_root: Path,
    token_path: Path,
    token: str,
    auth_headers: dict[str, str],
) -> None:
    """An env DSN whose key isn't ``dsn`` must redact via value-shape."""
    _ = token
    config = _build_config(
        workspace_root=workspace_root,
        project_root=project_root,
        include_account_env_secret=False,
    )
    config.accounts["codex_primary"].env = {
        # No credential keyword in the key, password only in the query.
        "PG_CONN": "postgresql://db.example/pollypm?user=alice&password=secretpw",
        "PUBLIC_FLAG": "ok",
    }
    app = create_app(config=config, token_path=token_path)
    with TestClient(app) as client:
        body = client.get("/api/v1/config", headers=auth_headers).json()
    env = body["config"]["accounts"]["codex_primary"]["env"]
    assert env["PG_CONN"] == REDACTED
    assert env["PUBLIC_FLAG"] == "ok"
    assert "secretpw" not in response_text(body)


# ---------------------------------------------------------------------------
# libpq keyword DSN redaction (Codex round-3 P0 on PR #2056)
# ---------------------------------------------------------------------------


def test_libpq_dsn_with_password_token_is_redacted(
    workspace_root: Path,
    project_root: Path,
    token_path: Path,
    token: str,
    auth_headers: dict[str, str],
) -> None:
    """libpq keyword DSN with ``password=`` token must be redacted.

    Codex round-3 P0 exact probe: ``PG_CONN = "host=db.example
    dbname=pollypm user=alice password=secretpw"``. Carrier key
    ``PG_CONN`` lacks every credential keyword, value isn't URL-shape,
    and no vendor prefix matches — only the libpq value-shape catches
    this.
    """
    _ = token
    config = _build_config(
        workspace_root=workspace_root,
        project_root=project_root,
        include_account_env_secret=False,
    )
    config.accounts["codex_primary"].env = {
        "PG_CONN": "host=db.example dbname=pollypm user=alice password=secretpw",
        "PUBLIC_FLAG": "ok",
    }
    app = create_app(config=config, token_path=token_path)
    with TestClient(app) as client:
        body = client.get("/api/v1/config", headers=auth_headers).json()
    env = body["config"]["accounts"]["codex_primary"]["env"]
    assert env["PG_CONN"] == REDACTED
    assert env["PUBLIC_FLAG"] == "ok"
    assert "secretpw" not in response_text(body)


def test_libpq_dsn_without_password_token_not_redacted(
    workspace_root: Path,
    project_root: Path,
    token_path: Path,
    token: str,
    auth_headers: dict[str, str],
) -> None:
    """libpq DSN with no credential token passes through unchanged.

    Over-redaction would hurt observability — a DSN like
    ``host=db dbname=x user=alice sslmode=require`` carries no
    password and should remain visible so operators can read their
    connection config back.
    """
    _ = token
    config = _build_config(
        workspace_root=workspace_root,
        project_root=project_root,
        include_account_env_secret=False,
    )
    safe_dsn = "host=db dbname=x user=alice sslmode=require"
    config.accounts["codex_primary"].env = {
        "PG_HOST_CONFIG": safe_dsn,
    }
    app = create_app(config=config, token_path=token_path)
    with TestClient(app) as client:
        body = client.get("/api/v1/config", headers=auth_headers).json()
    env = body["config"]["accounts"]["codex_primary"]["env"]
    assert env["PG_HOST_CONFIG"] == safe_dsn


def test_single_key_value_not_treated_as_dsn(
    workspace_root: Path,
    project_root: Path,
    token_path: Path,
    token: str,
    auth_headers: dict[str, str],
) -> None:
    """A single ``key=value`` pair isn't a DSN — must not be redacted.

    The 2-token minimum in ``_value_looks_libpq_dsn`` exists to keep
    trivial config like ``mode=production`` out of scope. Even though
    a hypothetical ``password=foo`` *would* match the secret keyword,
    one token alone shouldn't be treated as a DSN value-shape.
    """
    _ = token
    config = _build_config(
        workspace_root=workspace_root,
        project_root=project_root,
        include_account_env_secret=False,
    )
    config.accounts["codex_primary"].env = {
        "MODE": "mode=production",
    }
    app = create_app(config=config, token_path=token_path)
    with TestClient(app) as client:
        body = client.get("/api/v1/config", headers=auth_headers).json()
    env = body["config"]["accounts"]["codex_primary"]["env"]
    assert env["MODE"] == "mode=production"


def test_libpq_dsn_with_quoted_password(
    workspace_root: Path,
    project_root: Path,
    token_path: Path,
    token: str,
    auth_headers: dict[str, str],
) -> None:
    """Quoted-value form (whitespace inside ``password='...'``) redacts.

    libpq allows single-quoted values with embedded whitespace; the
    tokenizer must still see ``password`` as one of the keyword tokens
    even when its value contains spaces.
    """
    _ = token
    config = _build_config(
        workspace_root=workspace_root,
        project_root=project_root,
        include_account_env_secret=False,
    )
    config.accounts["codex_primary"].env = {
        "PG_CONN": "host=db password='se cret pw'",
    }
    app = create_app(config=config, token_path=token_path)
    with TestClient(app) as client:
        body = client.get("/api/v1/config", headers=auth_headers).json()
    env = body["config"]["accounts"]["codex_primary"]["env"]
    assert env["PG_CONN"] == REDACTED
    assert "se cret pw" not in response_text(body)


# ---------------------------------------------------------------------------
# OpenAPI documents the value-shape redaction contract
# ---------------------------------------------------------------------------


def test_openapi_describes_value_shape_redaction() -> None:
    """``docs/api/openapi.yaml`` /config description mentions
    value-shape redaction (libpq DSN, URL userinfo) not just the
    keyword heuristic.

    The redaction contract is security-critical and operator-visible;
    clients shouldn't have to read the route source to know which
    shapes get masked.
    """
    import yaml

    contract_path = (
        Path(__file__).resolve().parent.parent
        / "docs"
        / "api"
        / "openapi.yaml"
    )
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    description = contract["paths"]["/config"]["get"]["description"].lower()
    assert "libpq" in description, description
    assert "userinfo" in description, description


# ---------------------------------------------------------------------------
# Reload-on-edit (Codex round-2 P0 on PR #2056)
# ---------------------------------------------------------------------------


def test_get_config_reloads_on_disk_edit(
    tmp_path: Path,
    token_path: Path,
    token: str,
    auth_headers: dict[str, str],
) -> None:
    """A TOML edit between two GETs must surface on the next call.

    The previous wiring (``lambda: config``) returned the startup
    snapshot, so the endpoint was stale until restart. The fix
    re-invokes ``load_config`` per request; ``load_config`` is
    mtime-cached so unchanged files are a single ``stat`` call.
    """
    _ = token
    from pollypm.config import load_config

    workspace_root = tmp_path / "ws"
    workspace_root.mkdir()
    project_root = tmp_path / "proj"
    project_root.mkdir()
    (project_root / ".pollypm").mkdir()

    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        f"""
[project]
name = "PollyPM"
root_dir = "{workspace_root.as_posix()}"
tmux_session = "pollypm-reload"
workspace_root = "{workspace_root.as_posix()}"

[pollypm]
controller_account = "codex_primary"

[accounts.codex_primary]
provider = "codex"
email = "codex@example.com"

[projects.alpha]
path = "{project_root.as_posix()}"
name = "Alpha"
kind = "git"
tracked = true
""".lstrip(),
        encoding="utf-8",
    )

    config = load_config(config_path)
    app = create_app(config=config, token_path=token_path)
    with TestClient(app) as client:
        body = client.get("/api/v1/config", headers=auth_headers).json()
        assert set(body["config"]["projects"].keys()) == {"alpha"}

        # Edit the TOML on disk: add a second project. The
        # mtime-keyed cache in ``load_config`` invalidates because the
        # mtime changes; the dependency provider re-invokes
        # ``load_config`` on the next request.
        project_root_b = tmp_path / "proj_b"
        project_root_b.mkdir()
        (project_root_b / ".pollypm").mkdir()
        # Bump mtime explicitly — some filesystems have 1s resolution
        # and the test writes both files in the same second.
        import os
        import time

        new_text = config_path.read_text(encoding="utf-8") + (
            f"\n[projects.beta]\n"
            f'path = "{project_root_b.as_posix()}"\n'
            f'name = "Beta"\n'
            f'kind = "git"\n'
            f"tracked = true\n"
        )
        config_path.write_text(new_text, encoding="utf-8")
        future = time.time() + 5
        os.utime(config_path, (future, future))

        body2 = client.get("/api/v1/config", headers=auth_headers).json()
        assert set(body2["config"]["projects"].keys()) == {"alpha", "beta"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def response_text(body: object) -> str:
    """Stringify the response for substring leak-checks."""
    import json

    return json.dumps(body, default=str)
