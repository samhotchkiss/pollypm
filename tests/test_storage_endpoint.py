"""Integration tests for ``GET /api/v1/storage`` (Phase 2 §12).

The router is a thin adapter over
:func:`pollypm.cli_features.storage.scan_pollypm_home`. We exercise it
end-to-end through ``TestClient`` against a real in-process app, using a
synthetic ``~/.pollypm/`` rooted at ``tmp_path``. Cap-hit behavior is
tested by monkey-patching the cap constant on the CLI module so the
suite never has to materialize 50k files on disk.

Run with::

    pytest --noconftest tests/test_storage_endpoint.py -v --timeout=120

to bypass the per-project conftest that requires a Postgres harness;
these tests touch only the filesystem + the FastAPI app.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pollypm import storage_report as storage_scanner
from pollypm.cli_features import storage as storage_cli
from pollypm.config import (
    AccountConfig,
    MemorySettings,
    PollyPMConfig,
    PollyPMSettings,
    ProjectSettings,
)
from pollypm.models import KnownProject, ProjectKind, ProviderKind, RuntimeKind
from pollypm.web_api import create_app, ensure_token


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_home(tmp_path: Path) -> Path:
    """A synthetic ``~/.pollypm/`` rooted at ``tmp_path``."""
    home = tmp_path / "pollypm-home"
    home.mkdir()
    return home


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "myproj"
    root.mkdir()
    (root / ".pollypm").mkdir()
    return root


@pytest.fixture
def config(fake_home: Path, project_root: Path) -> PollyPMConfig:
    """Config whose ``base_dir`` points at ``fake_home``.

    The route uses ``config.project.base_dir`` as the scan root when
    set, so every test in this module is isolated from the user's real
    ``~/.pollypm/``.
    """
    return PollyPMConfig(
        project=ProjectSettings(
            name="PollyPM",
            root_dir=fake_home.parent,
            tmux_session="pollypm-test",
            workspace_root=fake_home.parent,
            base_dir=fake_home,
            logs_dir=fake_home / "logs",
            snapshots_dir=fake_home / "snapshots",
            state_db=fake_home / "state.db",
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
        accounts={
            "codex_primary": AccountConfig(
                name="codex_primary",
                provider=ProviderKind.CODEX,
                email="codex@example.com",
                runtime=RuntimeKind.LOCAL,
                home=fake_home / "homes" / "codex_primary",
            ),
        },
        sessions={},
        projects={
            "myproj": KnownProject(
                key="myproj",
                path=project_root,
                name="My Project",
                tracked=True,
                kind=ProjectKind.GIT,
            ),
        },
        memory=MemorySettings(backend="file"),
    )


@pytest.fixture
def token(tmp_path: Path) -> tuple[Path, str]:
    token_path = tmp_path / "api-token"
    value, _generated = ensure_token(token_path)
    return token_path, value


@pytest.fixture
def auth_headers(token: tuple[Path, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {token[1]}"}


@pytest.fixture
def client(config: PollyPMConfig, token: tuple[Path, str]) -> TestClient:
    token_path, _ = token
    app = create_app(config=config, token_path=token_path)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(path: Path, *, size: int = 64) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def _seed_home(home: Path) -> None:
    """Populate every canonical subdir with a small file each."""
    for subdir in storage_cli._HOME_SUBDIRS:
        _write(home / subdir / "a.bin", size=128)
    # Plus a top-level config file so the config_files row is non-empty.
    _write(home / "pollypm.toml", size=42)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_storage_report_happy_path(
    client: TestClient, auth_headers: dict[str, str], fake_home: Path
) -> None:
    """Synthetic ``~/.pollypm/`` with one file per subdir returns
    populated totals + every subdir row + config-files row."""
    _seed_home(fake_home)

    resp = client.get("/api/v1/storage", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["home"] == str(fake_home)
    assert body["generated_at"]  # ISO-8601 string
    # Every canonical subdir should be present, regardless of size.
    names = [row["name"] for row in body["subdirs"]]
    for expected in storage_cli._HOME_SUBDIRS:
        assert expected in names, names
    # Totals reflect the seeded files: 10 subdir files + 1 config file.
    assert body["total_files"] == len(storage_cli._HOME_SUBDIRS) + 1
    assert body["total_bytes"] > 0
    assert body["config_files"]["files"] == 1
    assert body["config_files"]["bytes"] == 42


def test_storage_report_each_subdir_surfaces(
    client: TestClient, auth_headers: dict[str, str], fake_home: Path
) -> None:
    """Files written into each canonical subdir surface with the right
    file count + byte total on its row."""
    for subdir in storage_cli._HOME_SUBDIRS:
        _write(fake_home / subdir / "one.dat", size=100)
        _write(fake_home / subdir / "two.dat", size=100)

    resp = client.get("/api/v1/storage", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    by_name = {row["name"]: row for row in body["subdirs"]}
    for subdir in storage_cli._HOME_SUBDIRS:
        row = by_name[subdir]
        assert row["files"] == 2, f"{subdir} files: {row}"
        assert row["bytes"] == 200, f"{subdir} bytes: {row}"
        assert row["cap_hit"] is False
        # mtime fields should be populated ISO strings.
        assert row["oldest_mtime"] is not None
        assert row["newest_mtime"] is not None


def test_storage_report_cap_hit_annotation(
    client: TestClient,
    auth_headers: dict[str, str],
    fake_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``snapshots/`` exceeds ``SCAN_FILE_CAP``, the row sets
    ``cap_hit=true`` and emits the unbounded-growth NOTES badge."""
    # Lower the cap so the test materializes only a handful of files.
    # Patch the canonical module (``storage_report``) — the cap is
    # read by ``scan_pollypm_home`` from there, not from the CLI
    # re-export.
    monkeypatch.setattr(storage_scanner, "SCAN_FILE_CAP", 4)

    snapshots = fake_home / "snapshots"
    snapshots.mkdir()
    # Write 8 files: 2x the lowered cap so the walker definitely trips
    # the early-exit branch.
    for i in range(8):
        _write(snapshots / f"snap-{i}.bin", size=10)

    resp = client.get("/api/v1/storage", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    snapshots_row = next(
        row for row in body["subdirs"] if row["name"] == "snapshots"
    )
    assert snapshots_row["cap_hit"] is True
    # NOTES text comes from scan_pollypm_home; we only assert it's
    # non-empty so a future copy edit doesn't break the test.
    assert snapshots_row["note"]


def test_storage_auth_required(client: TestClient, fake_home: Path) -> None:
    """No bearer header → 401 unauthorized."""
    _seed_home(fake_home)
    resp = client.get("/api/v1/storage")
    assert resp.status_code == 401, resp.text
    body = resp.json()
    assert body["error"]["code"] in ("unauthorized", "invalid_token")


def test_storage_missing_home_returns_empty_200(
    client: TestClient, auth_headers: dict[str, str], fake_home: Path
) -> None:
    """A non-existent ``~/.pollypm/`` is a normal mode (fresh install)
    — the report returns 200 with empty subdirs + zero totals, not 500."""
    # Wipe the seeded fake home so the scan target is missing.
    import shutil

    shutil.rmtree(fake_home)
    assert not fake_home.exists()

    resp = client.get("/api/v1/storage", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_files"] == 0
    assert body["total_bytes"] == 0
    assert body["subdirs"] == []
    assert body["config_files"]["files"] == 0


def test_storage_subdir_detail(
    client: TestClient, auth_headers: dict[str, str], fake_home: Path
) -> None:
    """``GET /api/v1/storage/{subdir}`` returns one row of the report."""
    _write(fake_home / "transcripts" / "a.jsonl", size=500)
    _write(fake_home / "transcripts" / "b.jsonl", size=500)

    resp = client.get("/api/v1/storage/transcripts", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "transcripts"
    assert body["files"] == 2
    assert body["bytes"] == 1000
    assert body["cap_hit"] is False


def test_storage_subdir_unknown_returns_404(
    client: TestClient, auth_headers: dict[str, str], fake_home: Path
) -> None:
    """Unknown subdir name → 404 not_found with a hint listing the
    canonical names; we never walk arbitrary user paths."""
    _seed_home(fake_home)
    resp = client.get("/api/v1/storage/does-not-exist", headers=auth_headers)
    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["error"]["code"] == "not_found"
    # The hint should mention at least one canonical subdir so the
    # caller can self-correct without reading source.
    assert "snapshots" in body["error"].get("hint", "")


def test_storage_subdir_returns_empty_when_home_missing(
    client: TestClient, auth_headers: dict[str, str], fake_home: Path
) -> None:
    """Codex P0 (PR #2054 round 1): a canonical subdir + missing
    home directory must return ``200`` with an empty
    :class:`StorageEntry` — mirroring the top-level endpoint's
    fresh-install semantics — not a misleading 404 that claims a
    bug in ``scan_pollypm_home``.

    Run for every canonical subdir so a future addition to
    ``HOME_SUBDIRS`` is automatically covered.
    """
    import shutil

    shutil.rmtree(fake_home)
    assert not fake_home.exists()

    for subdir in storage_cli._HOME_SUBDIRS:
        resp = client.get(f"/api/v1/storage/{subdir}", headers=auth_headers)
        assert resp.status_code == 200, f"{subdir}: {resp.text}"
        body = resp.json()
        assert body["name"] == subdir
        assert body["files"] == 0, f"{subdir}: {body}"
        assert body["bytes"] == 0, f"{subdir}: {body}"
        assert body["cap_hit"] is False
        assert body["note"] == ""
        assert body["oldest_mtime"] is None
        assert body["newest_mtime"] is None


def test_storage_route_does_not_import_cli_features() -> None:
    """Module-boundary regression (Codex P1 on PR #2054 round 1).

    ``web_api/routes/storage.py`` must NOT import from
    ``pollypm.cli_features.*`` — pulling the Typer / bootstrap-pg /
    migrate-to-pg / prune CLI surface into ``pm serve`` couples the
    web API to presentation-layer code and inflates the route's
    import graph. Canonical scanner + types live in the neutral
    :mod:`pollypm.storage_report` module; both surfaces depend on it.
    """
    from pathlib import Path as _Path

    import pollypm.web_api.routes.storage as storage_route

    source = _Path(storage_route.__file__).read_text(encoding="utf-8")
    # Grep both the ``from`` and ``import`` forms so a future caller
    # can't sneak the boundary violation back in either way.
    assert "from pollypm.cli_features" not in source, (
        "web_api/routes/storage.py imports from pollypm.cli_features — "
        "use pollypm.storage_report (the neutral helper) instead."
    )
    assert "import pollypm.cli_features" not in source, (
        "web_api/routes/storage.py imports pollypm.cli_features — "
        "use pollypm.storage_report (the neutral helper) instead."
    )
