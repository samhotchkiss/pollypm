"""itsalive.co deployment plugin for PollyPM."""

from __future__ import annotations

import logging
from pathlib import Path

from pollypm.agent_profiles.builtin import StaticPromptProfile, heartbeat_prompt, polly_prompt, worker_prompt
from pollypm.itsalive import ITSALIVE_API, read_owner_token, read_project_config
from pollypm.plugin_api.v1 import HookContext, PollyPMPlugin

logger = logging.getLogger(__name__)


def read_deploy_token(project_root=None) -> str | None:
    root = Path.cwd() if project_root is None else Path(project_root)
    config = read_project_config(root)
    token = config.get("deployToken") or config.get("deploy_token")
    if isinstance(token, str) and token.strip():
        return token.strip()
    return None


def build_deploy_instructions() -> str:
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

Use PollyPM's built-in wrapper instead of raw `npx itsalive` when you want unattended deployment:

- First deploy: `pm itsalive deploy --project <project_key> --subdomain <slug> --email <user@example.com> --dir <publish_dir>`
- Re-deploy existing site: `pm itsalive deploy --project <project_key> --dir <publish_dir>`
- Check pending verification: `pm itsalive status --project <project_key>`
- Force a sweep now: `pm itsalive sweep --project <project_key>`

Important behavior:
- PollyPM writes first-deploy state under `.pollypm-state/itsalive/pending/`.
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


def _polly_prompt() -> str:
    return (
        f"{polly_prompt()}\n\n"
        "When the user wants to publish a site through itsalive, prefer PollyPM's `pm itsalive ...` "
        "commands over telling a worker to sit in a polling loop. If verification is needed, notify the "
        "user once and keep the lane moving; heartbeat will resume the deploy later.\n\n"
        f"{build_deploy_instructions()}"
    )


def _worker_prompt() -> str:
    return (
        f"{worker_prompt()}\n\n"
        "If a site should ship via itsalive, use the built-in PollyPM itsalive commands. Do not wait "
        "interactively for email verification when the wrapper has already persisted pending state.\n\n"
        f"{build_deploy_instructions()}"
    )


def _heartbeat_prompt() -> str:
    return (
        f"{heartbeat_prompt()}\n\n"
        "Heartbeat is also responsible for sweeping pending itsalive deploys and completing them once "
        "email verification finishes.\n\n"
        f"{build_deploy_instructions()}"
    )


def _on_session_after_launch(ctx: HookContext) -> None:
    logger.info("magic plugin active for %s", ctx.metadata.get("session_name", "session"))


plugin = PollyPMPlugin(
    name="magic",
    version="0.2.0",
    description="itsalive.co deployment integration for PollyPM sessions.",
    capabilities=("agent_profile", "hook"),
    agent_profiles={
        "magic": lambda: StaticPromptProfile(name="magic", prompt=build_deploy_instructions()),
        "polly": lambda: StaticPromptProfile(name="polly", prompt=_polly_prompt()),
        "worker": lambda: StaticPromptProfile(name="worker", prompt=_worker_prompt()),
        "heartbeat": lambda: StaticPromptProfile(name="heartbeat", prompt=_heartbeat_prompt()),
    },
    observers={
        "session.after_launch": [_on_session_after_launch],
    },
)
