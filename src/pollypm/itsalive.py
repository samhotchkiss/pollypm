from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
import json
import mimetypes
import re
from pathlib import Path
from typing import Any
from urllib import error, parse, request

from pollypm.atomic_io import atomic_write_json
from pollypm.messaging import (
    notify_deploy_complete,
    notify_deploy_expired,
    notify_deploy_verification_required,
)


ITSALIVE_API = "https://api.itsalive.co"
CONFIG_FILE = ".itsalive"
GLOBAL_CONFIG_FILE = Path.home() / ".itsalive"
# Workspace-level per-email owner token store (#954). itsalive's API mints an
# account-level ownerToken on first successful email verification and accepts
# it on /deploy/init to skip email verification for subsequent fresh
# subdomains under the same email. Keyed by email so a single operator can
# manage multiple verified accounts and a fresh project never re-prompts for
# verification on a previously-verified email.
OWNER_TOKENS_FILE = Path.home() / ".pollypm" / "itsalive_owner_tokens.json"
PENDING_DIR = ".pollypm/itsalive/pending"
_IGNORE_NAMES = {".DS_Store", ".itsalive", "ITSALIVE.md", "CLAUDE.md"}
_IGNORE_PARTS = {".git", "node_modules"}
_VERIFY_TIMEOUT_SECONDS = 15.0
_TITLE_RE = re.compile(r"<title[^>]*>([^<]+)</title>", re.IGNORECASE)
_SCRIPT_SRC_RE = re.compile(
    r"""<script[^>]*\bsrc\s*=\s*["']([^"']+)["'][^>]*>""", re.IGNORECASE
)
# itsalive's serve worker returns this exact placeholder when a subdomain has
# no published files (see /Users/sam/dev/itsalive/workers/serve/src/index.js
# `comingSoonPage`). Detecting the signature explicitly stops the
# hostname-fallback marker from rubber-stamping a not-yet-deployed site
# (#948). The signature is intentionally narrow: title suffix + the literal
# "Coming Soon" headline + the powered-by footer, all of which appear in the
# upstream template and would only collide with a real app that copied them.
_COMING_SOON_TITLE_SUFFIX = ".itsalive.co - Coming Soon"
_COMING_SOON_HEADING = "<h1>Coming Soon</h1>"
_COMING_SOON_FOOTER = "Powered by itsalive.co"
# Content-Type prefixes that browsers actually treat as JavaScript. itsalive's
# serve worker emits ``application/javascript`` for ``.js``; we also accept
# the spec-aligned aliases. ``application/octet-stream`` and ``text/html`` are
# the failure modes we're catching: a missing/asset-misrouted bundle that
# 200s with the SPA index instead of the script (#948).
_JS_CONTENT_TYPE_PREFIXES = (
    "application/javascript",
    "text/javascript",
    "application/ecmascript",
    "text/ecmascript",
    "application/x-javascript",
    "module",  # rare but valid for ES module servers
)
_JS_EXTENSIONS = (".js", ".mjs", ".cjs")


@dataclass(slots=True)
class DeployRequest:
    subdomain: str
    email: str
    publish_dir: str
    files: list[str]
    api_url: str = ITSALIVE_API


@dataclass(slots=True)
class PendingDeploy:
    deploy_id: str
    subdomain: str
    email: str
    publish_dir: str
    files: list[str]
    project_root: str
    api_url: str
    created_at: str
    expires_at: str


@dataclass(slots=True)
class DeployOutcome:
    status: str
    message: str
    subdomain: str
    domain: str | None = None
    url: str | None = None
    pending_path: str | None = None
    expires_at: str | None = None
    email: str | None = None


@dataclass(slots=True)
class VerifyResult:
    """Outcome of a post-deploy URL health check.

    A successful itsalive deploy returns HTTP 200 even when the served
    HTML cannot bootstrap the JS bundle — a white-screen ship. Workers
    and Polly call ``verify_deployment`` after every deploy so a 200
    plus an expected marker is required to claim done. Per #937.
    """

    url: str
    status_code: int
    ok: bool
    reason: str
    marker: str | None = None
    title: str | None = None
    body_excerpt: str = ""


def deploy_site(
    project_root: Path,
    *,
    subdomain: str | None = None,
    email: str | None = None,
    publish_dir: str = ".",
    api_url: str = ITSALIVE_API,
) -> DeployOutcome:
    root = project_root.resolve()
    config = read_project_config(root)
    if config.get("deployToken"):
        return push_site(root, publish_dir=publish_dir, api_url=api_url)

    chosen_subdomain = subdomain or str(config.get("subdomain") or "").strip()
    if not chosen_subdomain:
        raise ValueError("Subdomain is required for the first itsalive deployment")
    request_data = prepare_deploy_request(
        root,
        subdomain=chosen_subdomain,
        email=email,
        publish_dir=publish_dir,
        api_url=api_url,
    )
    # Look up an owner_token for the resolved email first so a fresh
    # subdomain under a previously-verified account skips email
    # verification entirely (#954). Fall back to the legacy single-email
    # ~/.itsalive store for back-compat with installs from before the
    # per-email map landed — but ONLY when the legacy config's email
    # matches the requested email (or has no email field at all). An
    # unconditional fallback would leak a token minted for one account
    # into a deploy for another (reopening of #954).
    owner_token = owner_token_for_email(
        request_data.email
    ) or legacy_owner_token_for_email(request_data.email)
    payload: dict[str, Any] = {
        "subdomain": request_data.subdomain,
        "email": request_data.email,
        "files": request_data.files,
    }
    if owner_token:
        payload["owner_token"] = owner_token
    init_data = api_json("POST", f"{api_url}/deploy/init", payload=payload)
    deploy_id = str(init_data["deploy_id"])
    if bool(init_data.get("pre_verified")):
        # The upstream accepted the saved owner_token and skipped sending
        # a verification email. Surface that in the heartbeat log so a
        # human reading "no email arrived" knows the deploy is healthy
        # (#954).
        if owner_token:
            print(
                f"[itsalive] Skipping email verification — using saved owner token for {request_data.email}",
                flush=True,
            )
        return _complete_pending(
            PendingDeploy(
                deploy_id=deploy_id,
                subdomain=request_data.subdomain,
                email=request_data.email,
                publish_dir=request_data.publish_dir,
                files=request_data.files,
                project_root=str(root),
                api_url=api_url,
                created_at=_now(),
                expires_at=(datetime.now(UTC) + timedelta(hours=24)).isoformat(),
            ),
            notify=False,
        )

    pending = PendingDeploy(
        deploy_id=deploy_id,
        subdomain=request_data.subdomain,
        email=request_data.email,
        publish_dir=request_data.publish_dir,
        files=request_data.files,
        project_root=str(root),
        api_url=api_url,
        created_at=_now(),
        expires_at=(datetime.now(UTC) + timedelta(hours=24)).isoformat(),
    )
    path = write_pending_deploy(root, pending)
    notify_deploy_verification_required(
        root,
        subdomain=pending.subdomain,
        email=pending.email,
        expires_at=pending.expires_at,
    )
    return DeployOutcome(
        status="pending_verification",
        message="Verification email sent; deployment will resume automatically after verification",
        subdomain=pending.subdomain,
        pending_path=str(path),
        expires_at=pending.expires_at,
        email=pending.email,
    )


def push_site(project_root: Path, *, publish_dir: str = ".", api_url: str = ITSALIVE_API) -> DeployOutcome:
    root = project_root.resolve()
    config = read_project_config(root)
    deploy_token = str(config.get("deployToken") or config.get("deploy_token") or "").strip()
    if not deploy_token:
        raise ValueError("No deploy token found in .itsalive; run a first deploy instead")
    files = list_publish_files(root, publish_dir)
    payload = api_json("POST", f"{api_url}/push", payload={"deployToken": deploy_token, "files": files})
    upload_base = f"{api_url}/push/upload"
    for relative_path in files:
        _upload_file(root / publish_dir / relative_path if publish_dir != "." else root / relative_path, upload_base, relative_path, deploy_token)
    final_config = {
        "subdomain": payload["subdomain"],
        "domain": payload["domain"],
        "email": config.get("email"),
        "deployToken": deploy_token,
        "publishDir": publish_dir,
    }
    write_project_config(root, final_config)
    write_itsalive_docs(root, payload["domain"], publish_dir=publish_dir)
    return DeployOutcome(
        status="deployed",
        message="itsalive deployment pushed successfully",
        subdomain=str(payload["subdomain"]),
        domain=str(payload["domain"]),
        url=f"https://{payload['domain']}",
        email=str(config.get("email") or ""),
    )


def pending_deploys(project_root: Path) -> list[PendingDeploy]:
    root = project_root.resolve()
    directory = pending_dir(root)
    if not directory.exists():
        return []
    items: list[PendingDeploy] = []
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text())
            items.append(PendingDeploy(**payload))
        except Exception:
            continue
    return items


def sweep_pending_deploys(project_root: Path) -> list[DeployOutcome]:
    outcomes: list[DeployOutcome] = []
    for pending in pending_deploys(project_root):
        root = Path(pending.project_root)
        expires_at = _parse_timestamp(pending.expires_at)
        if expires_at is not None and expires_at <= datetime.now(UTC):
            delete_pending_deploy(root, pending.deploy_id)
            notify_deploy_expired(
                root,
                subdomain=pending.subdomain,
                expires_at=pending.expires_at,
            )
            outcomes.append(
                DeployOutcome(
                    status="expired",
                    message="Verification window expired",
                    subdomain=pending.subdomain,
                    expires_at=pending.expires_at,
                    email=pending.email,
                )
            )
            continue
        status = api_json("GET", f"{pending.api_url}/deploy/{pending.deploy_id}/status")
        if bool(status.get("verified")):
            outcomes.append(_complete_pending(pending, notify=True))
    return outcomes


def prepare_deploy_request(
    project_root: Path,
    *,
    subdomain: str,
    email: str | None,
    publish_dir: str,
    api_url: str,
) -> DeployRequest:
    root = project_root.resolve()
    global_config = read_global_config()
    resolved_email = email or str(global_config.get("email") or "").strip()
    if not resolved_email:
        raise ValueError("Email is required for the first itsalive deployment")
    return DeployRequest(
        subdomain=subdomain,
        email=resolved_email,
        publish_dir=publish_dir,
        files=list_publish_files(root, publish_dir),
        api_url=api_url,
    )


def list_publish_files(project_root: Path, publish_dir: str = ".") -> list[str]:
    root = project_root.resolve()
    source_root = (root / publish_dir).resolve() if publish_dir != "." else root
    if not source_root.exists():
        raise FileNotFoundError(f"Publish directory does not exist: {publish_dir}")
    if not source_root.is_dir():
        raise NotADirectoryError(f"Publish directory is not a directory: {publish_dir}")
    files: list[str] = []
    for path in sorted(source_root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(source_root)
        if any(part in _IGNORE_PARTS for part in rel.parts):
            continue
        if path.name in _IGNORE_NAMES or path.name.startswith(".env"):
            continue
        files.append(rel.as_posix())
    if not files:
        raise ValueError(f"No deployable files found under {publish_dir}")
    return files


def read_owner_token() -> str | None:
    """Return the legacy single-email owner token from ``~/.itsalive``.

    Kept for back-compat with callers that don't know the requesting
    email (``build_deploy_instructions``, the magic plugin's preflight).
    Per-email lookups should use :func:`owner_token_for_email`. Deploy
    paths that DO know the requesting email must use
    :func:`legacy_owner_token_for_email` instead so a token minted for
    one account never leaks into a deploy for another (#954).
    """
    data = read_global_config()
    token = data.get("ownerToken") or data.get("owner_token")
    if isinstance(token, str) and token.strip():
        return token.strip()
    return None


def legacy_owner_token_for_email(email: str | None) -> str | None:
    """Return the legacy ``~/.itsalive`` token only when its email matches.

    The legacy single-email config at :data:`GLOBAL_CONFIG_FILE` predates
    the per-email map; it stores at most one ``{email, ownerToken}``
    pair. ``deploy_site`` may consult it as a fallback for installs that
    haven't migrated to the per-email store yet, but ONLY when the
    legacy config's email matches the requested email (or has no email
    field at all — back-compat with even older single-account configs
    that omitted the email key). Otherwise the caller must send no
    ``owner_token`` and let the cold verification path run. Email
    comparison is case-insensitive to match
    :func:`owner_token_for_email` (#954).
    """
    if not email:
        return None
    data = read_global_config()
    token = data.get("ownerToken") or data.get("owner_token")
    if not (isinstance(token, str) and token.strip()):
        return None
    legacy_email = data.get("email")
    if isinstance(legacy_email, str) and legacy_email.strip():
        if legacy_email.strip().lower() != email.strip().lower():
            return None
    # Either the legacy config has no email field (very old single-
    # account installs) or it matches the requested email.
    return token.strip()


def read_owner_tokens() -> dict[str, str]:
    """Return the per-email owner-token map from the workspace store.

    The store at :data:`OWNER_TOKENS_FILE` is a flat ``{email: token}``
    dict. Missing file, malformed JSON, and non-dict payloads all
    degrade silently to ``{}`` so a corrupt file never breaks the
    deploy path — at worst the user re-verifies via email (#954).
    """
    if not OWNER_TOKENS_FILE.exists():
        return {}
    try:
        payload = json.loads(OWNER_TOKENS_FILE.read_text())
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        str(email).strip().lower(): str(token).strip()
        for email, token in payload.items()
        if isinstance(email, str)
        and isinstance(token, str)
        and email.strip()
        and token.strip()
    }


def owner_token_for_email(email: str | None) -> str | None:
    """Return the saved owner token for ``email``, if any (#954).

    Email lookup is case-insensitive — itsalive normalizes emails
    server-side and the operator may type ``Sam@Example.com`` once and
    ``sam@example.com`` next time. The legacy ``~/.itsalive`` is only
    consulted by :func:`read_owner_token`; this function strictly reads
    the per-email map so a stale single-email token can't masquerade as
    a different user's verification.
    """
    if not email:
        return None
    tokens = read_owner_tokens()
    token = tokens.get(email.strip().lower())
    return token or None


def write_owner_token_for_email(email: str, owner_token: str) -> None:
    """Persist ``owner_token`` under ``email`` in the workspace store (#954)."""
    if not email or not owner_token:
        return
    tokens = read_owner_tokens()
    tokens[email.strip().lower()] = owner_token.strip()
    OWNER_TOKENS_FILE.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(OWNER_TOKENS_FILE, tokens)


def read_global_config() -> dict[str, Any]:
    if not GLOBAL_CONFIG_FILE.exists():
        return {}
    try:
        payload = json.loads(GLOBAL_CONFIG_FILE.read_text())
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def write_global_config(email: str, owner_token: str) -> None:
    GLOBAL_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(GLOBAL_CONFIG_FILE, {"email": email, "ownerToken": owner_token})


def read_project_config(project_root: Path) -> dict[str, Any]:
    path = project_root / CONFIG_FILE
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text())
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def read_deploy_token(project_root: Path | None = None) -> str | None:
    """Read the deploy token from the project's .itsalive config."""
    root = Path.cwd() if project_root is None else Path(project_root)
    config = read_project_config(root)
    token = config.get("deployToken") or config.get("deploy_token")
    if isinstance(token, str) and token.strip():
        return token.strip()
    return None


def build_deploy_instructions() -> str:
    """Build the full itsalive deployment instruction text for agent prompts."""
    owner_token = read_owner_token()
    verification = (
        "Verified owners can skip the first-deploy email check entirely because PollyPM will include "
        "the saved `owner_token` from `~/.itsalive` automatically."
        if owner_token
        else "If `~/.itsalive` already contains an `ownerToken`, PollyPM will include it automatically "
        "and skip first-deploy email verification."
    )
    return f"""\
## itsalive Deployment Workflow

Use PollyPM's built-in wrapper instead of a raw `npx itsalive` polling loop when you want unattended deployment:

- First deploy: `pm itsalive deploy --project <project_key> --subdomain <slug> --email <user@example.com> --dir <publish_dir>`
- Re-deploy existing site: `pm itsalive deploy --project <project_key> --dir <publish_dir>`
- Check pending verification: `pm itsalive status --project <project_key>`
- Force a sweep now: `pm itsalive sweep --project <project_key>`

Important behavior:
- PollyPM writes first-deploy state under `.pollypm/itsalive/pending/`.
- Verification links remain valid for 24 hours.
- Heartbeat checks pending deploys and completes them automatically after the user clicks the email link.
- Existing `.itsalive` deploy tokens trigger the fast push flow with no verification prompt.
- {verification}

Deploy API endpoints:
- `POST {ITSALIVE_API}/check-subdomain`
- `POST {ITSALIVE_API}/deploy/init`
- `GET  {ITSALIVE_API}/deploy/<deploy_id>/status`
- `PUT  {ITSALIVE_API}/deploy/<deploy_id>/upload?file=<path>`
- `POST {ITSALIVE_API}/deploy/<deploy_id>/finalize`
- `POST {ITSALIVE_API}/push`
- `PUT  {ITSALIVE_API}/push/upload?file=<path>&token=<deployToken>`

## itsalive App Capabilities

Always use relative paths inside deployed apps and include `credentials: 'include'`.

Authentication:
- `POST /_auth/login`
- `GET /_auth/me`
- `POST /_auth/logout`

Shared database:
- `PUT /_db/:collection/:id`
- `GET /_db/:collection/:id`
- `GET /_db/:collection?status=published&sort=-created_at&limit=10&offset=0`
- `DELETE /_db/:collection/:id`
- `POST /_db/:collection/_bulk`
- `PUT /_db/:collection/_settings`

User-private data:
- `GET /_me/:key`
- `PUT /_me/:key`

AI:
- `POST /_ai/chat`
- Providers include Claude, GPT, and Gemini.
- Supports `response_format: 'json'` and vision inputs.

Email and subscribers:
- `POST /_email/send`
- `POST /_email/send-bulk`
- `PUT /settings/branding`
- `POST /_subscribers`
- `GET /_subscribers`
- `PUT /_subscribers/:id`
- `DELETE /_subscribers/:id`

Automation:
- `GET/POST /cron`
- `PUT/DELETE /cron/:id`
- `POST /jobs`
- `GET /jobs`
- `GET /jobs/:id`
- `DELETE /jobs/:id`

Growth and metadata:
- `GET/PUT/DELETE /_og/routes`
- Custom domains and email-domain verification exist in the itsalive platform.
- Free-tier sites must include a visible `Powered by itsalive.co` footer with `?ref=SUBDOMAIN`.

Deploy-token owner operations:
- The `.itsalive` file stores `deployToken` for automation.
- Deploy-token writes can configure collection settings and OG routes without interactive login.
"""


def write_project_config(project_root: Path, payload: dict[str, Any]) -> Path:
    path = project_root / CONFIG_FILE
    atomic_write_json(path, payload)
    return path


def write_pending_deploy(project_root: Path, pending: PendingDeploy) -> Path:
    path = pending_dir(project_root) / f"{pending.deploy_id}.json"
    atomic_write_json(path, asdict(pending))
    return path


def delete_pending_deploy(project_root: Path, deploy_id: str) -> None:
    path = pending_dir(project_root) / f"{deploy_id}.json"
    if path.exists():
        path.unlink()


def pending_dir(project_root: Path) -> Path:
    directory = project_root.resolve() / PENDING_DIR
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def write_itsalive_docs(project_root: Path, domain: str, *, publish_dir: str) -> None:
    publish_note = (
        "The entire project directory is deployed."
        if publish_dir == "."
        else f"Only the `{publish_dir}/` directory is deployed."
    )
    itsalive_path = project_root / "ITSALIVE.md"
    claude_path = project_root / "CLAUDE.md"
    reference_line = "See ITSALIVE.md for itsalive.co deployment and API documentation."
    content = "\n".join(
        [
            "<!--",
            "  DO NOT EDIT THIS FILE",
            "  Generated by PollyPM's itsalive integration.",
            "-->",
            "",
            "# itsalive.co Integration",
            "",
            f"This app is deployed to https://{domain}",
            "",
            publish_note,
            "",
            "## Deploying Updates",
            "",
            "Run `pm itsalive deploy` to deploy changes. If `.itsalive` already contains a deploy token,",
            "PollyPM will push the update without email verification.",
            "",
            "## Authentication and API",
            "",
            "Use relative paths like `/_auth/*`, `/_db/*`, `/_me/*`, `/_ai/chat`, `/_email/*`,",
            "`/_subscribers`, `/cron`, `/jobs`, and `/_og/routes` in your deployed app.",
            "Always include `credentials: 'include'` for cookie-backed user auth.",
            "",
        ]
    )
    itsalive_path.write_text(content + "\n", encoding="utf-8")
    if claude_path.exists():
        current = claude_path.read_text(encoding="utf-8")
        if "ITSALIVE.md" not in current:
            claude_path.write_text(
                reference_line + "\n\n" + current, encoding="utf-8",
            )
    else:
        claude_path.write_text(reference_line + "\n", encoding="utf-8")


def api_json(method: str, url: str, *, payload: Any | None = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
    body: bytes | None = None
    request_headers = dict(headers or {})
    request_headers.setdefault("User-Agent", "PollyPM/0.1")
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    req = request.Request(url, data=body, method=method, headers=request_headers)
    try:
        with request.urlopen(req) as response:
            raw = response.read().decode("utf-8")
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw)
        except Exception as err:  # noqa: BLE001
            raise RuntimeError(f"itsalive API request failed: {exc.code} {raw}") from err
        _raise_api_error(data)
        raise RuntimeError(f"itsalive API request failed: {exc.code}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"itsalive API request failed: {exc.reason}") from exc
    try:
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"itsalive API returned non-JSON response from {url}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"itsalive API returned unexpected payload from {url}")
    _raise_api_error(data)
    return data


def _complete_pending(pending: PendingDeploy, *, notify: bool) -> DeployOutcome:
    root = Path(pending.project_root)
    upload_base = f"{pending.api_url}/deploy/{pending.deploy_id}/upload"
    source_root = (root / pending.publish_dir).resolve() if pending.publish_dir != "." else root
    for relative_path in pending.files:
        _upload_file(source_root / relative_path, upload_base, relative_path, None)
    finalize = api_json("POST", f"{pending.api_url}/deploy/{pending.deploy_id}/finalize")
    domain = f"{finalize['subdomain']}.itsalive.co"
    write_project_config(
        root,
        {
            "subdomain": finalize["subdomain"],
            "domain": domain,
            "email": finalize["email"],
            "deployToken": finalize["deployToken"],
            "publishDir": pending.publish_dir,
        },
    )
    owner_token = _extract_owner_token(finalize)
    if owner_token:
        finalized_email = str(finalize["email"])
        # Workspace per-email map is the canonical store from #954 onwards
        # — persists even if the operator deploys with multiple emails.
        write_owner_token_for_email(finalized_email, owner_token)
        # Legacy ~/.itsalive write retained for back-compat with
        # build_deploy_instructions and the magic plugin preflight which
        # still call read_owner_token() without an email.
        write_global_config(finalized_email, owner_token)
    write_itsalive_docs(root, domain, publish_dir=pending.publish_dir)
    delete_pending_deploy(root, pending.deploy_id)
    if notify:
        notify_deploy_complete(
            root, subdomain=str(finalize["subdomain"]), domain=domain,
        )
    return DeployOutcome(
        status="deployed",
        message="itsalive deployment completed",
        subdomain=str(finalize["subdomain"]),
        domain=domain,
        url=f"https://{domain}",
        email=str(finalize["email"]),
    )


def _upload_file(path: Path, base_url: str, relative_path: str, deploy_token: str | None) -> None:
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    url = f"{base_url}?file={relative_path}"
    if deploy_token:
        url = f"{url}&token={deploy_token}"
    req = request.Request(
        url,
        data=path.read_bytes(),
        method="PUT",
        headers={
            "Content-Type": content_type,
            "User-Agent": "PollyPM/0.1",
        },
    )
    try:
        with request.urlopen(req) as response:
            raw = response.read().decode("utf-8")
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"itsalive upload failed for {relative_path}: {exc.code} {raw}") from exc
    data = json.loads(raw)
    if isinstance(data, dict):
        _raise_api_error(data)


def _raise_api_error(data: dict[str, Any]) -> None:
    message = data.get("error")
    if isinstance(message, str) and message.strip():
        raise RuntimeError(message.strip())


def _parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _extract_owner_token(payload: dict[str, Any]) -> str:
    """Extract the owner token from finalize payloads with tolerant key handling."""
    direct = payload.get("ownerToken") or payload.get("owner_token")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    owner = payload.get("owner")
    if isinstance(owner, dict):
        nested = owner.get("token") or owner.get("ownerToken") or owner.get("owner_token")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    return ""


def verify_marker(project_root: Path | None) -> str | None:
    """Read the project's expected post-deploy marker, if any.

    Workers and the operator pin a known string from the build (a title,
    a `data-app=` attribute, an element id) so a 200-but-empty-body
    deploy fails verification. The marker is stored under ``verifyMarker``
    in ``.itsalive`` so it survives ``write_project_config`` round-trips.
    """
    if project_root is None:
        return None
    config = read_project_config(Path(project_root))
    raw = config.get("verifyMarker") or config.get("verify_marker")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def write_verify_marker(project_root: Path, marker: str) -> Path:
    """Persist the expected post-deploy marker into the project's ``.itsalive``."""
    root = Path(project_root).resolve()
    config = read_project_config(root)
    config["verifyMarker"] = marker.strip()
    return write_project_config(root, config)


def verify_deployment(
    url: str,
    *,
    marker: str | None = None,
    project_root: Path | None = None,
    timeout: float = _VERIFY_TIMEOUT_SECONDS,
) -> VerifyResult:
    """Fetch ``url`` and assert HTTP 200 + an expected marker is present.

    A successful itsalive deploy can still serve a white screen: the
    HTML loads with status 200 but the JS bundle 404s, the asset paths
    resolve under the wrong base href, or an import error blanks the
    page. ``verify_deployment`` is the discriminator — workers MUST
    call it before signaling ``pm task done`` and Polly MUST call it
    before notifying the user that a task is shipped (#937).

    Marker resolution order:
      1. The explicit ``marker`` argument.
      2. The project's persisted marker (``.itsalive`` ``verifyMarker``).
      3. The fetched body's ``<title>`` content.

    A non-200 response, a missing body, or a marker mismatch all return
    ``VerifyResult(ok=False)`` with a human-readable ``reason``.
    """
    fetch_url = url if "://" in url else f"https://{url}"
    req = request.Request(
        fetch_url,
        method="GET",
        headers={
            "User-Agent": "PollyPM-itsalive-verify/0.1",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    try:
        with request.urlopen(req, timeout=timeout) as response:
            status = int(getattr(response, "status", 0) or response.getcode())
            body_bytes = response.read()
    except error.HTTPError as exc:
        body_bytes = b""
        try:
            body_bytes = exc.read()
        except Exception:  # noqa: BLE001
            body_bytes = b""
        return VerifyResult(
            url=fetch_url,
            status_code=int(exc.code),
            ok=False,
            reason=f"HTTP {exc.code} from {fetch_url}",
            body_excerpt=body_bytes.decode("utf-8", errors="replace")[:200],
        )
    except error.URLError as exc:
        return VerifyResult(
            url=fetch_url,
            status_code=0,
            ok=False,
            reason=f"network error: {exc.reason}",
        )
    except TimeoutError as exc:  # pragma: no cover - depends on socket
        return VerifyResult(
            url=fetch_url,
            status_code=0,
            ok=False,
            reason=f"timeout after {timeout:.1f}s: {exc}",
        )

    body_text = body_bytes.decode("utf-8", errors="replace")
    title_match = _TITLE_RE.search(body_text)
    title = title_match.group(1).strip() if title_match else None

    if status != 200:
        return VerifyResult(
            url=fetch_url,
            status_code=status,
            ok=False,
            reason=f"HTTP {status} from {fetch_url}",
            title=title,
            body_excerpt=body_text[:200],
        )

    if not body_text.strip():
        return VerifyResult(
            url=fetch_url,
            status_code=status,
            ok=False,
            reason="empty body from 200 response",
            title=title,
            body_excerpt="",
        )

    # Explicit placeholder detection: itsalive's serve worker returns a
    # "Coming Soon" page with status 200 when no files are published for a
    # subdomain. Its <title> contains the hostname, which would otherwise
    # match the hostname-fallback marker and rubber-stamp a non-deploy
    # (#948). Fail closed before any marker resolution.
    if _is_coming_soon_placeholder(body_text):
        return VerifyResult(
            url=fetch_url,
            status_code=status,
            ok=False,
            reason=(
                "itsalive 'Coming Soon' placeholder served — no files "
                "published for this subdomain yet"
            ),
            title=title,
            body_excerpt=body_text[:200],
        )

    expected = marker if marker is not None else verify_marker(project_root)
    if expected is None or not expected.strip():
        expected = title

    if not expected:
        return VerifyResult(
            url=fetch_url,
            status_code=status,
            ok=False,
            reason=(
                "no marker available — pass --marker, set verifyMarker in "
                ".itsalive, or include a <title> in the deployed HTML"
            ),
            title=title,
            body_excerpt=body_text[:200],
        )

    if expected not in body_text:
        return VerifyResult(
            url=fetch_url,
            status_code=status,
            ok=False,
            reason=f"marker not found in body: {expected!r}",
            marker=expected,
            title=title,
            body_excerpt=body_text[:200],
        )

    # Cheap heuristic: if a SPA's only `<script src>` is a relative path
    # that resolves above the served root, the browser renders blank
    # even though curl is happy. Catch the obvious case where the bundle
    # path looks unreachable. We do not fetch each script — that's the
    # operator's call — but we surface the suspicion in ``reason`` if
    # the body has no script at all (a static landing page is fine; a
    # built SPA without any script tag is the white-screen smell).
    has_script = bool(_SCRIPT_SRC_RE.search(body_text)) or "<script" in body_text.lower()
    looks_like_spa_root = "<div id=\"root\"" in body_text or '<div id="app"' in body_text
    if looks_like_spa_root and not has_script:
        return VerifyResult(
            url=fetch_url,
            status_code=status,
            ok=False,
            reason=(
                "SPA mount point present but no <script> tag — likely "
                "a build output that did not inject the bundle"
            ),
            marker=expected,
            title=title,
            body_excerpt=body_text[:200],
        )

    # Linked JS bundle check: a 200 on the SPA index with the right marker
    # still ships a white screen if `<script src="/assets/index-XYZ.js">`
    # 404s or returns the SPA index HTML (the classic "asset router falls
    # through to index.html" misroute). Fetch every same-origin script the
    # page references and assert HTTP 200 + a JavaScript content-type. A
    # bundle that returns text/html or octet-stream means the browser will
    # refuse to execute it (or run garbage), so we fail verification (#948).
    bundle_failure = _check_linked_js_bundles(
        fetch_url, body_text, timeout=timeout,
    )
    if bundle_failure is not None:
        return VerifyResult(
            url=fetch_url,
            status_code=status,
            ok=False,
            reason=bundle_failure,
            marker=expected,
            title=title,
            body_excerpt=body_text[:200],
        )

    return VerifyResult(
        url=fetch_url,
        status_code=status,
        ok=True,
        reason="ok",
        marker=expected,
        title=title,
        body_excerpt=body_text[:200],
    )


def _is_coming_soon_placeholder(body_text: str) -> bool:
    """Return True if ``body_text`` is the itsalive 'Coming Soon' placeholder.

    The signature is the title suffix + headline + powered-by footer. All
    three appearing together is unique enough to rule out a real app that
    happens to use the phrase "Coming Soon" in its content.
    """
    if _COMING_SOON_TITLE_SUFFIX not in body_text:
        return False
    if _COMING_SOON_HEADING not in body_text:
        return False
    return _COMING_SOON_FOOTER in body_text


def _check_linked_js_bundles(
    page_url: str, body_text: str, *, timeout: float
) -> str | None:
    """Fetch every same-origin JS script linked from ``body_text``.

    Returns ``None`` if every linked bundle is reachable with a JS-ish
    content-type, or a human-readable failure reason otherwise. External
    scripts (different origin) are skipped — itsalive deploys are
    same-origin by construction, and we don't want to fail on third-party
    analytics outages we can't control.
    """
    parsed_page = parse.urlparse(page_url)
    page_origin = (parsed_page.scheme, parsed_page.netloc)
    seen: set[str] = set()
    for raw_src in _SCRIPT_SRC_RE.findall(body_text):
        src = raw_src.strip()
        if not src:
            continue
        # Inline data URIs, blob URLs, and the like aren't network-fetchable.
        if src.startswith(("data:", "blob:", "javascript:")):
            continue
        absolute = parse.urljoin(page_url, src)
        parsed = parse.urlparse(absolute)
        if (parsed.scheme, parsed.netloc) != page_origin:
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        # Only care about .js / .mjs / .cjs (or unknown-extension same-origin
        # scripts — those usually mean a built bundle without an extension).
        path_lower = parsed.path.lower()
        is_js_ext = path_lower.endswith(_JS_EXTENSIONS)
        # Skip clearly-not-JS extensions (e.g. some sites embed .json via
        # <script type="application/json">). We only flag <script src=...>
        # that look like executable code paths.
        if not is_js_ext and "." in path_lower.rsplit("/", 1)[-1]:
            continue
        failure = _fetch_and_validate_js_asset(absolute, timeout=timeout)
        if failure is not None:
            return failure
    return None


def _fetch_and_validate_js_asset(url: str, *, timeout: float) -> str | None:
    """Fetch ``url`` and assert it returns 200 + a JavaScript content-type.

    Returns ``None`` on success or a human-readable reason on failure.
    """
    req = request.Request(
        url,
        method="GET",
        headers={
            "User-Agent": "PollyPM-itsalive-verify/0.1",
            "Accept": "application/javascript,text/javascript,*/*;q=0.1",
        },
    )
    try:
        with request.urlopen(req, timeout=timeout) as response:
            status = int(getattr(response, "status", 0) or response.getcode())
            content_type = response.headers.get("Content-Type", "") or ""
    except error.HTTPError as exc:
        return f"linked JS asset {url} returned HTTP {exc.code}"
    except error.URLError as exc:
        return f"linked JS asset {url} unreachable: {exc.reason}"
    except TimeoutError as exc:  # pragma: no cover - depends on socket
        return f"linked JS asset {url} timeout: {exc}"

    if status != 200:
        return f"linked JS asset {url} returned HTTP {status}"
    ct_lower = content_type.split(";", 1)[0].strip().lower()
    if not any(ct_lower.startswith(prefix) for prefix in _JS_CONTENT_TYPE_PREFIXES):
        return (
            f"linked JS asset {url} served with Content-Type "
            f"{content_type!r} (expected JavaScript) — likely an asset "
            f"router fallthrough returning HTML"
        )
    return None
