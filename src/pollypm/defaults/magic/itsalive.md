Description: Deploy and extend sites with itsalive.co
Trigger: when launching, updating, or wiring backend features for an itsalive site

Use PollyPM's built-in itsalive flow instead of a raw blocking deploy loop.

Commands:
- `pm itsalive deploy --project <project_key> --subdomain <slug> --email <email> --dir <publish_dir>` for a first deploy
- `pm itsalive deploy --project <project_key> --dir <publish_dir>` for subsequent deploys
- `pm itsalive status --project <project_key>` to inspect pending verification
- `pm itsalive sweep --project <project_key>` to force a verification/completion check now

Important behavior:
- First deploys persist pending state in `.pollypm-state/itsalive/pending/`
- Verification links remain valid for 24 hours
- Heartbeat resumes and completes verified deploys automatically
- Existing `~/.itsalive` owner tokens skip first-deploy verification
- Existing project `.itsalive` deploy tokens skip the first-deploy flow and use push deploys

Capabilities available after deployment:
- Auth via `/_auth/*`
- Shared DB via `/_db/*`
- User-private storage via `/_me/*`
- AI chat via `/_ai/chat`
- Transactional email and branding
- Subscribers/newsletters
- Cron jobs and async job queue
- Dynamic OG tags for SPA routes

Read `.pollypm/docs/reference/itsalive.md` for the API surface and deployment rules before implementing non-trivial itsalive features.
