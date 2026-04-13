# itsalive

PollyPM can deploy static sites to itsalive.co and teach agents how to use the platform after deploy.

## Deploy Workflow

Use PollyPM's wrapper, not a manual blocking poll loop:

- `pm itsalive deploy --project <project_key> --subdomain <slug> --email <email> --dir <publish_dir>` for first deploys
- `pm itsalive deploy --project <project_key> --dir <publish_dir>` for push deploys after `.itsalive` exists
- `pm itsalive status --project <project_key>` to inspect pending first deploys
- `pm itsalive sweep --project <project_key>` to force the resume logic immediately

Behavior:
- First deploy: PollyPM calls `POST /deploy/init`
- If the user is not already verified, PollyPM stores the pending deploy locally and returns immediately
- The verification email remains valid for 24 hours
- Heartbeat polls `GET /deploy/<id>/status`; after verification it uploads files and calls `/finalize`
- `.itsalive` stores the `deployToken` for later pushes
- `~/.itsalive` stores the `ownerToken` so already-verified users skip verification on new sites

## Auth and Data APIs

Inside an itsalive app, use relative URLs and include `credentials: 'include'`.

Authentication:
- `POST /_auth/login`
- `GET /_auth/me`
- `POST /_auth/logout`

Shared database:
- `PUT /_db/:collection/:id`
- `GET /_db/:collection/:id`
- `GET /_db/:collection`
- `DELETE /_db/:collection/:id`
- `POST /_db/:collection/_bulk`
- `PUT /_db/:collection/_settings`

Collection queries:
- `?field=value`
- `?mine=true`
- `?sort=field`
- `?sort=-field`
- `?limit=N`
- `?offset=N`

Private per-user storage:
- `GET /_me/:key`
- `PUT /_me/:key`

## Higher-Level Features

AI:
- `POST /_ai/chat`
- Providers: Claude, GPT, Gemini
- Supports structured JSON responses and vision inputs

Email:
- `POST /_email/send`
- `POST /_email/send-bulk`
- `PUT /settings/branding`

Subscribers:
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

Metadata:
- `GET/PUT/DELETE /_og/routes`
- Custom domains and email-domain verification are available through the itsalive platform

## Deployment Rules

- Free-tier apps must include a visible footer linking to `https://itsalive.co?ref=SUBDOMAIN`
- Deploy tokens can configure collection settings and OG routes without interactive browser auth
- Re-read project `ITSALIVE.md` after deploy if the site repo contains one, because the platform can add new capabilities over time
