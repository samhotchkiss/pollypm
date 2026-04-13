from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
import json
import mimetypes
from pathlib import Path
from typing import Any
from urllib import error, request

from pollypm.atomic_io import atomic_write_json
from pollypm.messaging import create_message


ITSALIVE_API = "https://api.itsalive.co"
CONFIG_FILE = ".itsalive"
GLOBAL_CONFIG_FILE = Path.home() / ".itsalive"
PENDING_DIR = ".pollypm-state/itsalive/pending"
_IGNORE_NAMES = {".DS_Store", ".itsalive", "ITSALIVE.md", "CLAUDE.md"}
_IGNORE_PARTS = {".git", "node_modules"}


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
    owner_token = read_owner_token()
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
    create_message(
        root,
        sender="itsalive",
        subject=f"itsalive verification required for {pending.subdomain}",
        body=(
            f"PollyPM started an itsalive deployment for `{pending.subdomain}.itsalive.co`.\n\n"
            f"A verification email was sent to `{pending.email}`. The link remains valid until "
            f"`{pending.expires_at}`.\n\n"
            "No one needs to sit in the terminal waiting. Heartbeat will detect verification and "
            "complete the deployment automatically."
        ),
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
            create_message(
                root,
                sender="itsalive",
                subject=f"itsalive verification expired for {pending.subdomain}",
                body=(
                    f"The pending deployment for `{pending.subdomain}.itsalive.co` expired at "
                    f"`{pending.expires_at}` before the verification link was clicked.\n\n"
                    "Run `pm itsalive deploy` again to send a fresh email."
                ),
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
    data = read_global_config()
    token = data.get("ownerToken") or data.get("owner_token")
    if isinstance(token, str) and token.strip():
        return token.strip()
    return None


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
    itsalive_path.write_text(content + "\n")
    if claude_path.exists():
        current = claude_path.read_text()
        if "ITSALIVE.md" not in current:
            claude_path.write_text(reference_line + "\n\n" + current)
    else:
        claude_path.write_text(reference_line + "\n")


def api_json(method: str, url: str, *, payload: Any | None = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
    body: bytes | None = None
    request_headers = dict(headers or {})
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
    owner_token = str(finalize.get("ownerToken") or "").strip()
    if owner_token:
        write_global_config(str(finalize["email"]), owner_token)
    write_itsalive_docs(root, domain, publish_dir=pending.publish_dir)
    delete_pending_deploy(root, pending.deploy_id)
    if notify:
        create_message(
            root,
            sender="itsalive",
            subject=f"itsalive deploy completed for {finalize['subdomain']}",
            body=(
                f"`https://{domain}` is live.\n\n"
                "PollyPM detected that verification completed and finished the deploy automatically."
            ),
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
        headers={"Content-Type": content_type},
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
