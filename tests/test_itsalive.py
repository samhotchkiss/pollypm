from __future__ import annotations

import json
from pathlib import Path

from pollypm import itsalive


def test_list_publish_files_filters_control_files(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<h1>ok</h1>")
    (tmp_path / ".itsalive").write_text("{}")
    (tmp_path / "ITSALIVE.md").write_text("ignore")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("ignore")
    assert itsalive.list_publish_files(tmp_path) == ["index.html"]


def test_first_deploy_persists_pending_verification(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "index.html").write_text("<h1>ok</h1>")
    monkeypatch.setattr(itsalive, "GLOBAL_CONFIG_FILE", tmp_path / "global.json")

    def fake_api(method: str, url: str, *, payload=None, headers=None):
        assert method == "POST"
        assert url.endswith("/deploy/init")
        assert payload["subdomain"] == "demo"
        return {"deploy_id": "dep_123", "pre_verified": False}

    monkeypatch.setattr(itsalive, "api_json", fake_api)
    outcome = itsalive.deploy_site(tmp_path, subdomain="demo", email="user@example.com", publish_dir="dist")
    assert outcome.status == "pending_verification"
    pending = itsalive.pending_deploys(tmp_path)
    assert len(pending) == 1
    assert pending[0].deploy_id == "dep_123"
    inbox_msgs = tmp_path / ".pollypm" / "inbox" / "messages"
    assert any(
        "verification required" in path.read_text()
        for msg_dir in inbox_msgs.iterdir() if msg_dir.is_dir()
        for path in msg_dir.glob("0*.md")
    )


def test_verified_owner_skips_verification_and_completes(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "site").mkdir()
    (tmp_path / "site" / "index.html").write_text("<h1>ok</h1>")
    global_config = tmp_path / "global.json"
    global_config.write_text(json.dumps({"email": "owner@example.com", "ownerToken": "owner_tok"}))
    monkeypatch.setattr(itsalive, "GLOBAL_CONFIG_FILE", global_config)
    uploads: list[tuple[Path, str]] = []

    def fake_api(method: str, url: str, *, payload=None, headers=None):
        if url.endswith("/deploy/init"):
            assert payload["owner_token"] == "owner_tok"
            return {"deploy_id": "dep_456", "pre_verified": True}
        if url.endswith("/deploy/dep_456/finalize"):
            return {
                "subdomain": "demo",
                "email": "owner@example.com",
                "deployToken": "deploy_tok",
                "ownerToken": "owner_tok_2",
            }
        raise AssertionError(url)

    monkeypatch.setattr(itsalive, "api_json", fake_api)
    monkeypatch.setattr(
        itsalive,
        "_upload_file",
        lambda path, base_url, relative_path, deploy_token: uploads.append((path, relative_path)),
    )
    outcome = itsalive.deploy_site(tmp_path, subdomain="demo", publish_dir="site")
    assert outcome.status == "deployed"
    assert outcome.url == "https://demo.itsalive.co"
    assert uploads == [(tmp_path / "site" / "index.html", "index.html")]
    config = json.loads((tmp_path / ".itsalive").read_text())
    assert config["deployToken"] == "deploy_tok"
    saved_global = json.loads(global_config.read_text())
    assert saved_global["ownerToken"] == "owner_tok_2"
    assert itsalive.pending_deploys(tmp_path) == []


def test_sweep_completes_verified_pending_deploy(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "index.html").write_text("<h1>ok</h1>")
    monkeypatch.setattr(itsalive, "GLOBAL_CONFIG_FILE", tmp_path / "global.json")
    pending = itsalive.PendingDeploy(
        deploy_id="dep_789",
        subdomain="demo",
        email="user@example.com",
        publish_dir="dist",
        files=["index.html"],
        project_root=str(tmp_path),
        api_url=itsalive.ITSALIVE_API,
        created_at="2026-04-12T00:00:00+00:00",
        expires_at="2099-04-13T00:00:00+00:00",
    )
    itsalive.write_pending_deploy(tmp_path, pending)
    uploads: list[str] = []

    def fake_api(method: str, url: str, *, payload=None, headers=None):
        if url.endswith("/deploy/dep_789/status"):
            return {"verified": True, "subdomain": "demo"}
        if url.endswith("/deploy/dep_789/finalize"):
            return {
                "subdomain": "demo",
                "email": "user@example.com",
                "deployToken": "deploy_tok",
                "ownerToken": "owner_tok",
            }
        raise AssertionError(url)

    monkeypatch.setattr(itsalive, "api_json", fake_api)
    monkeypatch.setattr(
        itsalive,
        "_upload_file",
        lambda path, base_url, relative_path, deploy_token: uploads.append(relative_path),
    )
    outcomes = itsalive.sweep_pending_deploys(tmp_path)
    assert len(outcomes) == 1
    assert outcomes[0].status == "deployed"
    assert uploads == ["index.html"]
    assert not list((tmp_path / ".pollypm-state" / "itsalive" / "pending").glob("*.json"))
    inbox_msgs = tmp_path / ".pollypm" / "inbox" / "messages"
    assert any(
        "deploy completed" in path.read_text()
        for msg_dir in inbox_msgs.iterdir() if msg_dir.is_dir()
        for path in msg_dir.glob("0*.md")
    )


def test_push_deploy_uses_existing_project_token(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "index.html").write_text("<h1>ok</h1>")
    (tmp_path / ".itsalive").write_text(json.dumps({"deployToken": "deploy_tok", "email": "user@example.com"}))
    uploads: list[str] = []

    def fake_api(method: str, url: str, *, payload=None, headers=None):
        assert url.endswith("/push")
        assert payload["deployToken"] == "deploy_tok"
        return {"subdomain": "demo", "domain": "demo.itsalive.co"}

    monkeypatch.setattr(itsalive, "api_json", fake_api)
    monkeypatch.setattr(
        itsalive,
        "_upload_file",
        lambda path, base_url, relative_path, deploy_token: uploads.append(f"{relative_path}:{deploy_token}"),
    )
    outcome = itsalive.deploy_site(tmp_path)
    assert outcome.status == "deployed"
    assert uploads == ["index.html:deploy_tok"]
