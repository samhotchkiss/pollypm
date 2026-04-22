# Account Modes

PollyPM supports three account layouts:

## `default-profile`

The account uses the host machine's normal CLI profile:

- Claude reads the user's default `~/.claude/`
- Codex reads the user's default `~/.codex/`
- `home` is `null` in `pollypm.toml`

This is the lowest-friction mode when the CLI is already logged in on the host.

## `isolated-home`

The account uses a PollyPM-managed home directory:

- Claude gets `CLAUDE_CONFIG_DIR=<home>/.claude`
- Codex gets `CODEX_HOME=<home>/.codex`
- `home` points at a dedicated path under `.pollypm/homes/`

Use this when you want separate credentials per account.

## `docker`

The account runs inside an isolated container runtime.

- The workspace is mounted into the container
- PollyPM keeps the account's CLI state separate from the host
- This is the strongest isolation mode

## Rule of thumb

- Use `default-profile` for the zero-input happy path.
- Use `isolated-home` when the same machine needs distinct credentials.
- Use `docker` when you want hard runtime separation.
