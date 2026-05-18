# [CANCELLED] itsalive/48 build v6+ → review (CLI refactor + staging cleanup)

**Two reviewer findings addressed in one handoff.**

**v3 reviewer finding (CLI not actually testable):**
- `cli/src/index.js` now exports `main(depsOverride)` with injectable prompts/fetch/readFile/writeFile/existsSync/mkdir/glob/sleep/openUrl/exit
- Auto-run guarded by `import.meta.url === pathToFileURL(process.argv[1]).href` — importing the module no longer executes the CLI
- The duplicate `firstDeploy()` body now delegates to `runFirstDeploy` from `./deploy.js`
- `tests/route-smoke.mjs` drives `main()` end-to-end with mocked prompts (subdomain + email + open), mocked fetch (check-subdomain → init → status × 2 → upload × 2 → finalize → docs/itsalive-md), in-memory fs, mocked glob, instant sleep
- Real CLI still works: `node cli/src/index.js --help` prints usage

**v6 reviewer note (staging zero-id placeholders):**
- Removed `[[env.staging.d1_databases]]` and `[[env.staging.kv_namespaces]]` blocks from both `workers/api/wrangler.toml` and `workers/serve/wrangler.toml` — without them, `wrangler deploy --env staging` fails loudly at startup (desired)
- `docs/runbooks/deploy-hygiene.md` has a new section "Adding the staging D1/KV bindings (one-time setup)" with the wrangler create commands and the exact binding blocks to drop into a gitignored local override
- New smoke check `no placeholder zero-id binding` per wrangler.toml — regression-proof

**State:** branch `task/itsalive-48` clean, 4 commits ahead of master, `npm run smoke` = **26/26 PASS**, `workers/api/src/index.js` and `workers/serve/src/index.js` byte-identical to master.

**Verify:**
```
cd /Users/sam/dev/itsalive/.pollypm/worktrees/itsalive-48
git log --oneline master..HEAD
npm run smoke
git diff --stat master..HEAD -- workers/api/src/index.js workers/serve/src/index.js  # should be empty
```
