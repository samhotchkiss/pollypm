# 0039 Build itsalive plugin magic for automated site deployment

Build a pollypm plugin that automates launching sites via itsalive.co and teaches polly agents about all itsalive capabilities.

## Requirements

1. **Plugin structure**: Create a pollypm plugin (type: magic/provider) that integrates itsalive into the pollypm ecosystem. Follow the plugin system conventions in .pollypm/docs/reference/plugins.md.

2. **Agent awareness**: The plugin must inject itsalive capability knowledge into agent sessions so polly workers know how to use itsalive's full API (auth, database, email, AI chat, cron, jobs, subscribers, OG tags, etc). Reference ITSALIVE.md for the complete API.

3. **Async email verification (24hr window)**: The first site deployment requires email verification. Currently this blocks. Make it async-friendly:
   - When a user triggers their first itsalive deploy, send the verification email
   - The user has 24 hours to click the link (not just the current session)
   - Polly should automatically detect when verification completes and proceed with the deploy
   - This is critical for unattended operation

4. **Skip verification for verified users**: Once a user has verified their email once, subsequent site launches should skip email confirmation entirely.

5. **Build, test, deploy**: The plugin should be functional end-to-end. Write tests. Deploy it so it's usable.

## Key files to study
- /Users/sam/dev/itsalive/ITSALIVE.md - Full API reference
- /Users/sam/dev/itsalive/cli/ - The npx itsalive CLI
- /Users/sam/dev/pollypm/src/pollypm/plugins_builtin/ - Existing plugin examples
- /Users/sam/dev/itsalive/CLAUDE.md - Architecture overview
