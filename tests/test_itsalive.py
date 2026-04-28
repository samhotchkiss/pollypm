from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import patch
from urllib import error

import pytest

from pollypm import itsalive


@pytest.fixture(autouse=True)
def _redirect_owner_tokens_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Quarantine the workspace-level owner-token store under tmp_path.

    The per-email map at :data:`itsalive.OWNER_TOKENS_FILE` defaults to
    ``~/.pollypm/itsalive_owner_tokens.json`` (#954). Without this
    fixture, any test that exercises ``deploy_site`` or
    ``_complete_pending`` would write the test's fake email/token into
    the developer's real workspace store. Redirecting per-test keeps
    the suite hermetic.
    """
    monkeypatch.setattr(
        itsalive, "OWNER_TOKENS_FILE", tmp_path / "owner_tokens.json"
    )


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

    alerts: list[dict] = []

    def _capture(project_root, *, subdomain, email, expires_at):
        alerts.append({"subdomain": subdomain, "email": email, "expires_at": expires_at})

    monkeypatch.setattr(itsalive, "notify_deploy_verification_required", _capture)

    outcome = itsalive.deploy_site(tmp_path, subdomain="demo", email="user@example.com", publish_dir="dist")
    assert outcome.status == "pending_verification"
    pending = itsalive.pending_deploys(tmp_path)
    assert len(pending) == 1
    assert pending[0].deploy_id == "dep_123"
    assert len(alerts) == 1
    assert alerts[0]["subdomain"] == "demo"
    assert alerts[0]["email"] == "user@example.com"


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
    complete_calls: list[dict] = []
    monkeypatch.setattr(
        itsalive,
        "notify_deploy_complete",
        lambda project_root, *, subdomain, domain: complete_calls.append(
            {"subdomain": subdomain, "domain": domain}
        ),
    )
    outcomes = itsalive.sweep_pending_deploys(tmp_path)
    assert len(outcomes) == 1
    assert outcomes[0].status == "deployed"
    assert uploads == ["index.html"]
    assert not list((tmp_path / ".pollypm" / "itsalive" / "pending").glob("*.json"))
    assert complete_calls == [{"subdomain": "demo", "domain": "demo.itsalive.co"}]


def test_sweep_persists_owner_token_from_snake_case_finalize_response(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "index.html").write_text("<h1>ok</h1>")
    global_config = tmp_path / "global.json"
    monkeypatch.setattr(itsalive, "GLOBAL_CONFIG_FILE", global_config)
    pending = itsalive.PendingDeploy(
        deploy_id="dep_snake",
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

    def fake_api(method: str, url: str, *, payload=None, headers=None):
        if url.endswith("/deploy/dep_snake/status"):
            return {"verified": True, "subdomain": "demo"}
        if url.endswith("/deploy/dep_snake/finalize"):
            return {
                "subdomain": "demo",
                "email": "user@example.com",
                "deployToken": "deploy_tok",
                "owner_token": "owner_tok_snake",
            }
        raise AssertionError(url)

    monkeypatch.setattr(itsalive, "api_json", fake_api)
    monkeypatch.setattr(itsalive, "_upload_file", lambda *args, **kwargs: None)

    outcomes = itsalive.sweep_pending_deploys(tmp_path)

    assert len(outcomes) == 1
    saved_global = json.loads(global_config.read_text())
    assert saved_global["ownerToken"] == "owner_tok_snake"


class _FakeResponse:
    """Minimal urllib response stand-in for ``verify_deployment`` tests."""

    def __init__(
        self,
        body: bytes,
        status: int = 200,
        content_type: str = "text/html; charset=utf-8",
    ) -> None:
        self._body = body
        self.status = status
        self.headers = {"Content-Type": content_type}

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body

    def getcode(self) -> int:
        return self.status


def _patch_urlopen(response):
    """Patch ``urllib.request.urlopen`` for verify tests.

    ``response`` may be:
      * a ``_FakeResponse`` — returned for any URL;
      * an ``Exception`` — raised for any URL;
      * a ``dict`` mapping URL → ``_FakeResponse``/``Exception`` (a default
        can be provided under the empty-string key) for tests that need to
        distinguish the page fetch from a linked JS bundle fetch.
    """

    def _resolve(req):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if isinstance(response, dict):
            picked = response.get(url, response.get("", None))
            if picked is None:
                raise AssertionError(f"unmocked URL in test: {url}")
            return picked
        return response

    def fake_urlopen(req, timeout=None):  # noqa: ARG001 - signature compat
        picked = _resolve(req)
        if isinstance(picked, Exception):
            raise picked
        return picked

    return patch("pollypm.itsalive.request.urlopen", side_effect=fake_urlopen)


def test_verify_deployment_passes_when_marker_present_in_200_body() -> None:
    """A successful itsalive deploy serves HTTP 200 plus the expected
    marker in the body — the canonical pass case for #937."""
    body = (
        b"<!doctype html><html><head><title>Demo Tracker</title></head>"
        b"<body><div id=\"app\" data-app=\"demo\"></div>"
        b"<script src=\"/assets/index.js\"></script></body></html>"
    )
    routes = {
        "https://demo.example.test": _FakeResponse(body, status=200),
        "https://demo.example.test/assets/index.js": _FakeResponse(
            b"console.log('hi');",
            status=200,
            content_type="application/javascript; charset=utf-8",
        ),
    }
    with _patch_urlopen(routes):
        result = itsalive.verify_deployment(
            "https://demo.example.test", marker="data-app=\"demo\"",
        )
    assert result.ok is True
    assert result.status_code == 200
    assert result.marker == "data-app=\"demo\""
    assert result.title == "Demo Tracker"
    assert result.reason == "ok"


def test_verify_deployment_fails_on_200_with_empty_body() -> None:
    """The classic white-screen failure mode: HTTP 200 with no body
    (or a body that doesn't contain the expected marker) must be
    treated as a failure, not a pass. This is the discriminator the
    worker + Polly rely on per #937."""
    with _patch_urlopen(_FakeResponse(b"", status=200)):
        result = itsalive.verify_deployment(
            "https://blank.example.test", marker="data-app=\"x\"",
        )
    assert result.ok is False
    assert result.status_code == 200
    assert "empty body" in result.reason


def test_verify_deployment_fails_when_marker_missing_from_200_body() -> None:
    """itsalive served HTTP 200 but the JS bundle didn't load and the
    pre-committed marker isn't in the body. ``verify_deployment`` must
    flag this as broken even though the status code looks healthy."""
    body = (
        b"<!doctype html><html><head><title>Some App</title></head>"
        b"<body><div id=\"root\"></div></body></html>"
    )
    with _patch_urlopen(_FakeResponse(body, status=200)):
        result = itsalive.verify_deployment(
            "https://broken.example.test", marker="data-app=\"specific\"",
        )
    assert result.ok is False
    assert result.status_code == 200
    assert "marker not found" in result.reason
    # The body excerpt is recorded so Polly's rework task has concrete
    # detail to include in its description.
    assert "Some App" in result.body_excerpt


def test_verify_deployment_falls_back_to_title_marker_when_none_supplied() -> None:
    """If neither ``--marker`` nor the project's persisted ``verifyMarker``
    is set, ``verify_deployment`` derives the marker from the served
    HTML's ``<title>``. A page with no title and no caller-supplied
    marker must fail closed rather than rubber-stamping a 200."""
    body = b"<html><head><title>Hello App</title></head><body><script src=\"x.js\"></script>hi</body></html>"
    routes = {
        "https://hello.example.test": _FakeResponse(body, status=200),
        "https://hello.example.test/x.js": _FakeResponse(
            b"console.log('hi');",
            status=200,
            content_type="application/javascript",
        ),
    }
    with _patch_urlopen(routes):
        result = itsalive.verify_deployment("https://hello.example.test")
    assert result.ok is True
    # The title became the marker.
    assert result.marker == "Hello App"

    # Empty title + no caller marker → fail closed.
    body2 = b"<html><head></head><body>nope</body></html>"
    with _patch_urlopen(_FakeResponse(body2, status=200)):
        result2 = itsalive.verify_deployment("https://no-title.example.test")
    assert result2.ok is False
    assert "no marker" in result2.reason


def test_verify_deployment_uses_project_marker_when_caller_omits_one(
    tmp_path: Path,
) -> None:
    """The persisted ``verifyMarker`` in ``.itsalive`` is the audit
    contract: once Polly saves a marker for a project, every later
    verify reuses it. This keeps audit-on-request stable across
    sessions."""
    itsalive.write_verify_marker(tmp_path, "data-build-id=42")
    body = b"<html><head><title>x</title></head><body data-build-id=42><script>1</script></body></html>"
    with _patch_urlopen(_FakeResponse(body, status=200)):
        result = itsalive.verify_deployment(
            "https://saved.example.test", project_root=tmp_path,
        )
    assert result.ok is True
    assert result.marker == "data-build-id=42"


def test_verify_deployment_fails_on_http_error() -> None:
    """Transport-level failures (the deploy URL is genuinely 5xx or DNS
    is broken) must produce ``ok=False`` so the worker / Polly stop the
    success path. Distinct from the 200-but-broken case so the CLI can
    surface a different exit code."""
    http_error = error.HTTPError(
        "https://err.example.test", 503, "boom", hdrs=None, fp=io.BytesIO(b"down"),
    )
    with _patch_urlopen(http_error):
        result = itsalive.verify_deployment("https://err.example.test")
    assert result.ok is False
    assert result.status_code == 503
    assert "503" in result.reason


def test_itsalive_verify_cli_exits_2_on_200_with_missing_marker() -> None:
    """The ``pm itsalive verify`` CLI must exit with a distinct code
    for the 200-but-blank case (exit 2) vs transport errors (exit 1)
    so workers and Polly can branch on the failure mode. A pass exits
    0. The exit codes are part of the contract documented on the
    command's help — changing them silently regresses #937."""
    from typer.testing import CliRunner

    from pollypm.cli_features.issues import itsalive_app

    runner = CliRunner()

    # Case 1: pass — 200 + marker present.
    body_ok = b"<html><head><title>App</title></head><body><script>1</script>data-app=ok</body></html>"
    with _patch_urlopen(_FakeResponse(body_ok, status=200)):
        result = runner.invoke(
            itsalive_app, ["verify", "demo", "--marker", "data-app=ok"],
        )
    assert result.exit_code == 0, result.output
    assert "ok=True" in result.output

    # Case 2: 200 but missing marker — exit 2 (the white-screen case).
    body_blank = b"<html><head><title>App</title></head><body><script>1</script></body></html>"
    with _patch_urlopen(_FakeResponse(body_blank, status=200)):
        result = runner.invoke(
            itsalive_app, ["verify", "demo", "--marker", "data-app=ok"],
        )
    assert result.exit_code == 2, result.output
    assert "ok=False" in result.output

    # Case 3: HTTP 5xx — exit 1 (transport / server failure).
    http_error = error.HTTPError(
        "https://demo.itsalive.co", 503, "down", hdrs=None, fp=io.BytesIO(b""),
    )
    with _patch_urlopen(http_error):
        result = runner.invoke(
            itsalive_app, ["verify", "demo", "--marker", "anything"],
        )
    assert result.exit_code == 1, result.output


def test_verify_deployment_flags_spa_missing_script_tag() -> None:
    """Heuristic: a SPA mount point (``<div id=\"root\">``) with no
    ``<script>`` tag in the HTML is the canonical white-screen build
    failure (Vite/Webpack didn't inject the bundle). Even if a marker
    matches, the missing script means the page won't bootstrap."""
    body = b"<html><head><title>Marker Here</title></head><body><div id=\"root\"></div></body></html>"
    with _patch_urlopen(_FakeResponse(body, status=200)):
        result = itsalive.verify_deployment(
            "https://spa.example.test", marker="Marker Here",
        )
    assert result.ok is False
    assert "script" in result.reason.lower()


def test_verify_deployment_rejects_itsalive_coming_soon_placeholder() -> None:
    """The itsalive serve worker returns a 'Coming Soon' placeholder with
    HTTP 200 when no files have been published for the subdomain. Its
    ``<title>`` contains the hostname, so the hostname-fallback marker
    used to silently match and report ``ok=True`` (#948). Verify must
    detect the placeholder explicitly and fail closed."""
    body = (
        b"<!DOCTYPE html>\n<html lang=\"en\"><head>"
        b"<meta charset=\"UTF-8\">"
        b"<title>pomodoro.itsalive.co - Coming Soon</title>"
        b"</head><body><div class=\"card\">"
        b"<h1>Coming Soon</h1>"
        b"<p>This site is being built and will be live shortly.</p>"
        b"<div class=\"subdomain\">pomodoro.itsalive.co</div>"
        b"</div>"
        b"<p class=\"footer\">"
        b"<a href=\"https://itsalive.co\">Powered by itsalive.co</a>"
        b"</p></body></html>"
    )
    with _patch_urlopen(_FakeResponse(body, status=200)):
        result = itsalive.verify_deployment("https://pomodoro.itsalive.co")
    assert result.ok is False
    assert result.status_code == 200
    assert "coming soon" in result.reason.lower()
    assert "placeholder" in result.reason.lower()


def test_verify_deployment_coming_soon_placeholder_via_cli_exits_2() -> None:
    """The CLI exit code on a placeholder must match the white-screen
    case (exit 2) — it's a 200-but-not-actually-deployed failure, not a
    transport error. Workers that branch on exit code must see the
    distinct exit=2 so they refuse to mark the task done (#948)."""
    from typer.testing import CliRunner

    from pollypm.cli_features.issues import itsalive_app

    runner = CliRunner()
    body = (
        b"<!DOCTYPE html><html><head>"
        b"<title>demo.itsalive.co - Coming Soon</title>"
        b"</head><body><h1>Coming Soon</h1>"
        b"<a href=\"https://itsalive.co\">Powered by itsalive.co</a>"
        b"</body></html>"
    )
    with _patch_urlopen(_FakeResponse(body, status=200)):
        result = runner.invoke(itsalive_app, ["verify", "demo"])
    assert result.exit_code == 2, result.output
    assert "ok=False" in result.output


def test_verify_deployment_rejects_html_returned_for_js_asset() -> None:
    """The classic Vite/SPA asset-router fallthrough: ``index.html`` is
    served for unknown paths, so a missing ``/assets/index-XYZ.js``
    returns the SPA HTML with status 200 and ``Content-Type: text/html``.
    The browser refuses to execute it and the page white-screens.
    Verify must fetch each linked same-origin script and reject the
    response unless it's actually JavaScript (#948)."""
    body = (
        b"<!doctype html><html><head><title>App</title></head>"
        b"<body><div id=\"root\"></div>"
        b"<script src=\"/assets/index-abc123.js\"></script>"
        b"</body></html>"
    )
    routes = {
        "https://app.example.test": _FakeResponse(body, status=200),
        # The asset router falls through to index.html with text/html.
        "https://app.example.test/assets/index-abc123.js": _FakeResponse(
            body, status=200, content_type="text/html; charset=utf-8",
        ),
    }
    with _patch_urlopen(routes):
        result = itsalive.verify_deployment(
            "https://app.example.test", marker="App",
        )
    assert result.ok is False
    assert result.status_code == 200
    assert "/assets/index-abc123.js" in result.reason
    assert (
        "javascript" in result.reason.lower()
        or "text/html" in result.reason.lower()
    )


def test_verify_deployment_rejects_404_on_linked_js_bundle() -> None:
    """A linked JS bundle that 404s means the SPA cannot bootstrap. The
    HTML may be 200 with the marker present, but the page is broken. The
    verifier must follow the script and reject the deploy (#948)."""
    body = (
        b"<!doctype html><html><head><title>App</title></head>"
        b"<body><div id=\"root\"></div>"
        b"<script src=\"/assets/missing.js\"></script>"
        b"</body></html>"
    )
    http_404 = error.HTTPError(
        "https://app.example.test/assets/missing.js",
        404,
        "Not Found",
        hdrs=None,
        fp=io.BytesIO(b""),
    )
    routes = {
        "https://app.example.test": _FakeResponse(body, status=200),
        "https://app.example.test/assets/missing.js": http_404,
    }
    with _patch_urlopen(routes):
        result = itsalive.verify_deployment(
            "https://app.example.test", marker="App",
        )
    assert result.ok is False
    assert "404" in result.reason
    assert "/assets/missing.js" in result.reason


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


# --- #954: workspace-level per-email owner_token store ---------------------


def test_finalize_persists_owner_token_to_per_email_workspace_store(
    tmp_path: Path, monkeypatch
) -> None:
    """``_complete_pending`` MUST capture the ``ownerToken`` from the
    finalize response and write it to the workspace-level per-email map
    (#954). Without this, every fresh subdomain re-prompts for
    verification even though the email was already verified upstream."""
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "index.html").write_text("<h1>ok</h1>")
    monkeypatch.setattr(itsalive, "GLOBAL_CONFIG_FILE", tmp_path / "global.json")
    pending = itsalive.PendingDeploy(
        deploy_id="dep_per_email",
        subdomain="alpha",
        email="alpha@example.com",
        publish_dir="dist",
        files=["index.html"],
        project_root=str(tmp_path),
        api_url=itsalive.ITSALIVE_API,
        created_at="2026-04-12T00:00:00+00:00",
        expires_at="2099-04-13T00:00:00+00:00",
    )
    itsalive.write_pending_deploy(tmp_path, pending)

    def fake_api(method: str, url: str, *, payload=None, headers=None):
        if url.endswith("/deploy/dep_per_email/status"):
            return {"verified": True, "subdomain": "alpha"}
        if url.endswith("/deploy/dep_per_email/finalize"):
            return {
                "subdomain": "alpha",
                "email": "alpha@example.com",
                "deployToken": "deploy_tok_alpha",
                "ownerToken": "owner_tok_alpha",
            }
        raise AssertionError(url)

    monkeypatch.setattr(itsalive, "api_json", fake_api)
    monkeypatch.setattr(itsalive, "_upload_file", lambda *args, **kwargs: None)

    outcomes = itsalive.sweep_pending_deploys(tmp_path)

    assert len(outcomes) == 1
    assert outcomes[0].status == "deployed"
    assert itsalive.owner_token_for_email("alpha@example.com") == "owner_tok_alpha"
    # Case-insensitive lookup — itsalive normalises emails server-side.
    assert itsalive.owner_token_for_email("Alpha@Example.com") == "owner_tok_alpha"


def test_init_includes_owner_token_when_stored_for_email(
    tmp_path: Path, monkeypatch
) -> None:
    """``deploy_site`` MUST forward the saved per-email owner_token on
    ``/deploy/init`` so a fresh subdomain skips email verification when
    the email is already verified upstream (#954)."""
    (tmp_path / "site").mkdir()
    (tmp_path / "site" / "index.html").write_text("<h1>ok</h1>")
    # No legacy ~/.itsalive — proves the per-email map alone is enough.
    monkeypatch.setattr(itsalive, "GLOBAL_CONFIG_FILE", tmp_path / "missing-global.json")
    itsalive.write_owner_token_for_email("ops@example.com", "owner_tok_ops")

    seen: dict[str, object] = {}

    def fake_api(method: str, url: str, *, payload=None, headers=None):
        if url.endswith("/deploy/init"):
            seen["init_payload"] = payload
            return {"deploy_id": "dep_ops", "pre_verified": True}
        if url.endswith("/deploy/dep_ops/finalize"):
            return {
                "subdomain": "ops-site",
                "email": "ops@example.com",
                "deployToken": "deploy_tok_ops",
                "ownerToken": "owner_tok_ops_rotated",
            }
        raise AssertionError(url)

    monkeypatch.setattr(itsalive, "api_json", fake_api)
    monkeypatch.setattr(itsalive, "_upload_file", lambda *args, **kwargs: None)

    outcome = itsalive.deploy_site(
        tmp_path, subdomain="ops-site", email="ops@example.com", publish_dir="site",
    )
    assert outcome.status == "deployed"
    init_payload = seen["init_payload"]
    assert isinstance(init_payload, dict)
    assert init_payload["owner_token"] == "owner_tok_ops"
    # Rotated token from finalize should overwrite the per-email entry.
    assert itsalive.owner_token_for_email("ops@example.com") == "owner_tok_ops_rotated"


def test_init_omits_owner_token_when_none_stored_for_email(
    tmp_path: Path, monkeypatch
) -> None:
    """Cold first deploy: no token stored anywhere → ``/deploy/init``
    body must NOT include ``owner_token`` and the deploy must walk the
    normal email-verification path (#954 regression guard)."""
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "index.html").write_text("<h1>ok</h1>")
    monkeypatch.setattr(itsalive, "GLOBAL_CONFIG_FILE", tmp_path / "missing-global.json")
    # OWNER_TOKENS_FILE is already redirected by the autouse fixture
    # and is empty — this is the "fresh install" baseline.
    seen: dict[str, object] = {}

    def fake_api(method: str, url: str, *, payload=None, headers=None):
        assert url.endswith("/deploy/init")
        seen["init_payload"] = payload
        return {"deploy_id": "dep_cold", "pre_verified": False}

    monkeypatch.setattr(itsalive, "api_json", fake_api)
    monkeypatch.setattr(
        itsalive, "notify_deploy_verification_required", lambda *a, **k: None,
    )

    outcome = itsalive.deploy_site(
        tmp_path,
        subdomain="cold",
        email="new@example.com",
        publish_dir="dist",
    )
    assert outcome.status == "pending_verification"
    init_payload = seen["init_payload"]
    assert isinstance(init_payload, dict)
    assert "owner_token" not in init_payload
    assert init_payload["email"] == "new@example.com"


def test_owner_token_for_email_returns_none_for_unknown_email(tmp_path: Path) -> None:
    """A different email than what's stored returns None — the legacy
    single-email ``~/.itsalive`` value must not masquerade as some other
    user's verification (#954)."""
    itsalive.write_owner_token_for_email("alice@example.com", "tok_alice")
    assert itsalive.owner_token_for_email("alice@example.com") == "tok_alice"
    assert itsalive.owner_token_for_email("bob@example.com") is None
    assert itsalive.owner_token_for_email("") is None
    assert itsalive.owner_token_for_email(None) is None


def test_read_owner_tokens_tolerates_corrupt_store(
    tmp_path: Path, monkeypatch
) -> None:
    """A malformed JSON file must not crash the deploy path — at worst
    the operator re-verifies via email."""
    monkeypatch.setattr(itsalive, "OWNER_TOKENS_FILE", tmp_path / "bad.json")
    (tmp_path / "bad.json").write_text("not json {")
    assert itsalive.read_owner_tokens() == {}
    assert itsalive.owner_token_for_email("anyone@example.com") is None


# --- #954 reopen: legacy ~/.itsalive must respect email boundary ---------


def _write_legacy_global_config(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload))


def test_deploy_does_not_leak_legacy_token_across_email_boundary(
    tmp_path: Path, monkeypatch
) -> None:
    """Reopening of #954: ``deploy_site`` must NOT fall back to the
    legacy single-email ``~/.itsalive`` token when the legacy config
    belongs to a different email than the one being deployed under.
    Sending the wrong account's token would defeat the per-email
    boundary the workspace store is designed to enforce."""
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "index.html").write_text("<h1>ok</h1>")
    global_path = tmp_path / "global.json"
    _write_legacy_global_config(
        global_path, {"email": "old@example.com", "ownerToken": "old-token"}
    )
    monkeypatch.setattr(itsalive, "GLOBAL_CONFIG_FILE", global_path)
    # OWNER_TOKENS_FILE empty via autouse fixture.

    seen: dict[str, object] = {}

    def fake_api(method: str, url: str, *, payload=None, headers=None):
        assert url.endswith("/deploy/init")
        seen["init_payload"] = payload
        return {"deploy_id": "dep_isolated", "pre_verified": False}

    monkeypatch.setattr(itsalive, "api_json", fake_api)
    monkeypatch.setattr(
        itsalive, "notify_deploy_verification_required", lambda *a, **k: None
    )

    outcome = itsalive.deploy_site(
        tmp_path,
        subdomain="demo",
        email="new@example.com",
        publish_dir="dist",
    )
    assert outcome.status == "pending_verification"
    init_payload = seen["init_payload"]
    assert isinstance(init_payload, dict)
    assert init_payload["email"] == "new@example.com"
    # Critical assertion: the legacy token MUST NOT leak across emails.
    assert "owner_token" not in init_payload or init_payload.get("owner_token") is None


def test_deploy_uses_legacy_token_when_emails_match(
    tmp_path: Path, monkeypatch
) -> None:
    """When the legacy ``~/.itsalive`` config's email matches the
    requested deploy email, the saved owner token IS forwarded — that's
    the back-compat path for single-account installs that predate the
    per-email map (#954)."""
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "index.html").write_text("<h1>ok</h1>")
    global_path = tmp_path / "global.json"
    _write_legacy_global_config(
        global_path, {"email": "old@example.com", "ownerToken": "old-token"}
    )
    monkeypatch.setattr(itsalive, "GLOBAL_CONFIG_FILE", global_path)

    seen: dict[str, object] = {}

    def fake_api(method: str, url: str, *, payload=None, headers=None):
        if url.endswith("/deploy/init"):
            seen["init_payload"] = payload
            return {"deploy_id": "dep_match", "pre_verified": False}
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(itsalive, "api_json", fake_api)
    monkeypatch.setattr(
        itsalive, "notify_deploy_verification_required", lambda *a, **k: None
    )

    itsalive.deploy_site(
        tmp_path,
        subdomain="demo",
        email="old@example.com",
        publish_dir="dist",
    )
    init_payload = seen["init_payload"]
    assert isinstance(init_payload, dict)
    assert init_payload.get("owner_token") == "old-token"


def test_deploy_uses_legacy_token_when_legacy_has_no_email(
    tmp_path: Path, monkeypatch
) -> None:
    """Even older single-account ``~/.itsalive`` files may have an
    ``ownerToken`` but no ``email`` key. Treat that as "applies to any
    email" — the operator only ever had one account on this machine —
    and forward the legacy token (#954 back-compat)."""
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "index.html").write_text("<h1>ok</h1>")
    global_path = tmp_path / "global.json"
    _write_legacy_global_config(global_path, {"ownerToken": "legacy-only-token"})
    monkeypatch.setattr(itsalive, "GLOBAL_CONFIG_FILE", global_path)

    seen: dict[str, object] = {}

    def fake_api(method: str, url: str, *, payload=None, headers=None):
        if url.endswith("/deploy/init"):
            seen["init_payload"] = payload
            return {"deploy_id": "dep_noemail", "pre_verified": False}
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(itsalive, "api_json", fake_api)
    monkeypatch.setattr(
        itsalive, "notify_deploy_verification_required", lambda *a, **k: None
    )

    itsalive.deploy_site(
        tmp_path,
        subdomain="demo",
        email="any@example.com",
        publish_dir="dist",
    )
    init_payload = seen["init_payload"]
    assert isinstance(init_payload, dict)
    assert init_payload.get("owner_token") == "legacy-only-token"


def test_legacy_owner_token_for_email_respects_email_match(
    tmp_path: Path, monkeypatch
) -> None:
    """Direct unit test for the helper: returns the token only when the
    legacy config has no email or has a matching email; otherwise None."""
    global_path = tmp_path / "global.json"
    monkeypatch.setattr(itsalive, "GLOBAL_CONFIG_FILE", global_path)

    # No file at all → None.
    assert itsalive.legacy_owner_token_for_email("anyone@example.com") is None

    # Mismatched email → None.
    _write_legacy_global_config(
        global_path, {"email": "old@example.com", "ownerToken": "tok"}
    )
    assert itsalive.legacy_owner_token_for_email("new@example.com") is None
    # Matching email (case-insensitive) → token.
    assert itsalive.legacy_owner_token_for_email("OLD@example.com") == "tok"

    # No email field → token (back-compat).
    _write_legacy_global_config(global_path, {"ownerToken": "tok-no-email"})
    assert (
        itsalive.legacy_owner_token_for_email("anyone@example.com")
        == "tok-no-email"
    )

    # Empty/None requested email → None (cannot match anything safely).
    assert itsalive.legacy_owner_token_for_email("") is None
    assert itsalive.legacy_owner_token_for_email(None) is None
