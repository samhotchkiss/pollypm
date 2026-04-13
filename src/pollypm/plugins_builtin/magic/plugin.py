"""itsalive.co deployment plugin for PollyPM.

Provides an agent profile that teaches agents about itsalive capabilities
and utility helpers for the full deployment flow.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from pollypm.agent_profiles.base import AgentProfile, AgentProfileContext
from pollypm.plugin_api.v1 import HookContext, PollyPMPlugin

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ITSALIVE_API = "https://api.itsalive.co"
ITSALIVE_CONFIG_FILE = ".itsalive"
OWNER_TOKEN_PATH = Path.home() / ".itsalive"

# ---------------------------------------------------------------------------
# Utility helpers (referenced by agents via the prompt)
# ---------------------------------------------------------------------------


def read_owner_token() -> str | None:
    """Return the owner_token from ~/.itsalive if it exists."""
    if not OWNER_TOKEN_PATH.exists():
        return None
    try:
        data = json.loads(OWNER_TOKEN_PATH.read_text())
        return data.get("ownerToken") or data.get("owner_token")
    except (json.JSONDecodeError, OSError):
        return None


def read_deploy_token(project_root: Path | None = None) -> str | None:
    """Return the deploy_token from .itsalive in the project root."""
    if project_root is None:
        project_root = Path.cwd()
    config_path = project_root / ITSALIVE_CONFIG_FILE
    if not config_path.exists():
        return None
    try:
        data = json.loads(config_path.read_text())
        return data.get("deployToken") or data.get("deploy_token")
    except (json.JSONDecodeError, OSError):
        return None


def build_deploy_instructions() -> str:
    """Return the full deployment instruction text for agents."""
    owner_token = read_owner_token()
    skip_note = ""
    if owner_token:
        skip_note = (
            "\n\nNOTE: An owner_token was found at ~/.itsalive. Include it in "
            "the deploy/init request to skip email verification entirely."
        )

    return f"""\
## Deploying a Site with itsalive.co

Base URL: {ITSALIVE_API}

### Pre-flight: Check subdomain availability

```
POST {ITSALIVE_API}/check-subdomain
Content-Type: application/json
{{"subdomain": "my-app"}}
```
Returns `{{"available": true}}` or `{{"available": false}}`.

### Step 1 - Initialise deployment

```
POST {ITSALIVE_API}/deploy/init
Content-Type: application/json
{{
  "subdomain": "my-app",
  "email": "user@example.com",
  "files": {{"index.html": {{"size": 1234, "hash": "..."}}}},
  "owner_token": "<optional, from ~/.itsalive>"
}}
```
Returns `{{"deploy_id": "...", "pre_verified": true/false}}`.

If `owner_token` is provided and valid the deploy is pre-verified and you can
skip straight to step 3. Otherwise the user will receive a verification email
(valid for 24 hours).{skip_note}

### Step 2 - Poll for email verification

```
GET {ITSALIVE_API}/deploy/<deploy_id>/status
```
Returns `{{"verified": true/false}}`. Poll every few seconds until verified.

### Step 3 - Get presigned upload URLs

```
POST {ITSALIVE_API}/deploy/<deploy_id>/upload-urls
Content-Type: application/json
{{"files": ["index.html", "style.css"]}}
```

### Step 4 - Upload files

PUT each file to its presigned URL.

### Step 5 - Finalise

```
POST {ITSALIVE_API}/deploy/<deploy_id>/finalize
```
Returns `{{"url": "https://my-app.itsalive.co", "deployToken": "...", "ownerToken": "..."}}`.

Save the `deployToken` to `.itsalive` in the project root and the `ownerToken`
to `~/.itsalive` for future deploys without re-verification.

### Updating an existing site

```
POST {ITSALIVE_API}/push
Content-Type: application/json
{{
  "deploy_token": "<from .itsalive>",
  "files": {{"index.html": {{"content": "<base64>", "size": 1234}}}}
}}
```

## itsalive.co Platform Capabilities

Once deployed, the site has access to a rich backend via relative API paths
(`/_auth/*`, `/_db/*`, `/_me/*`, etc.). All calls should include
`credentials: 'include'` for cookie-based auth.

### Authentication (magic-link, no passwords)
- `POST /_auth/login` — send magic link to email
- `GET  /_auth/me`    — check current session
- `POST /_auth/logout` — end session

### Database (shared app data, per-collection permissions)
- `PUT    /_db/:collection/:id`        — create / update document
- `GET    /_db/:collection/:id`        — get document
- `GET    /_db/:collection`            — list with filters, sort, pagination
- `DELETE /_db/:collection/:id`        — delete (owner only)
- `POST   /_db/:collection/_bulk`      — bulk write (up to 100)
- `PUT    /_db/:collection/_settings`  — configure public_read, public_write, schema
Query params: `?status=published`, `?mine=true`, `?sort=-created_at`, `?limit=10&offset=0`

### User-private data
- `GET/PUT /_me/:key` — per-user private key-value store

### AI Chat (Claude, GPT, Gemini)
- `POST /_ai/chat` — send messages, choose provider/tier, vision support

### Email
- `POST /_email/send`      — send transactional email
- `POST /_email/send-bulk`  — send to multiple recipients
- `PUT  /_email/settings`   — configure reply-to, from name

### Subscribers / Newsletter
- `POST /_subscribers`       — public subscribe endpoint
- `GET  /_subscribers`       — list (owner)
- `PUT  /_subscribers/:id`   — update
- `DELETE /_subscribers/:id` — remove

### Cron Jobs
- `POST/GET/PUT/DELETE /cron` — schedule recurring URL calls

### Job Queue
- `POST /jobs`       — queue async background work
- `GET  /jobs/:id`   — check status
- `DELETE /jobs/:id`  — cancel

### Dynamic OG Tags (social sharing for SPAs)
- `PUT /_og/routes` — map URL patterns to DB collections for og:title, og:description, og:image

### Email Branding
- `PUT /settings/branding` — customise login-email appearance

### File Uploads
- Files are deployed as static assets to R2 and served from the subdomain.

### Custom Domains
- Supported via the itsalive dashboard / API.

### Analytics & Billing
- Available through the owner dashboard at itsalive.co.

### Required Attribution (free tier)
All free-tier apps must include a visible footer:
```html
<footer style="text-align:center;padding:2rem;font-size:0.85rem;">
  <a href="https://itsalive.co?ref=SUBDOMAIN">Powered by itsalive.co</a>
</footer>
```
"""


# ---------------------------------------------------------------------------
# Agent profile
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MagicProfile(AgentProfile):
    """Agent profile that injects itsalive deployment knowledge."""

    name: str = "magic"

    def build_prompt(self, context: AgentProfileContext) -> str | None:
        return build_deploy_instructions()


# ---------------------------------------------------------------------------
# Observers
# ---------------------------------------------------------------------------


def _on_session_after_launch(ctx: HookContext) -> None:
    """Log when a session launches with the magic profile."""
    logger.info("magic plugin: session launched — itsalive integration active")


# ---------------------------------------------------------------------------
# Plugin object
# ---------------------------------------------------------------------------

plugin = PollyPMPlugin(
    name="magic",
    version="0.1.0",
    description="itsalive.co deployment integration for polly agent sessions.",
    capabilities=("agent_profile", "hook"),
    agent_profiles={
        "magic": lambda: MagicProfile(),
    },
    observers={
        "session.after_launch": [_on_session_after_launch],
    },
)
