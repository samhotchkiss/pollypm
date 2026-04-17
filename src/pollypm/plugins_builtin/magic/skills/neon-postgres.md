---
name: neon-postgres
description: Neon serverless Postgres — branching, scale-to-zero, connection pooling, schema migrations.
when_to_trigger:
  - neon
  - serverless postgres
  - database branching
  - scale to zero
kind: magic_skill
attribution: https://github.com/neondatabase/neon
---

# Neon / Postgres

## When to use

Use when the project is on Neon or when the workload has bursty traffic with long idle periods — Neon's scale-to-zero is a killer feature for dev environments and low-volume production. For always-hot services with predictable load, Supabase or a managed RDS is a better default. Neon is still Postgres — most Postgres knowledge carries.

## Process

1. **Use the pooled connection string, not direct.** Neon's compute can hibernate and cold-start on a connection; the pooler (`-pooler` in the host) handles that gracefully. Direct connections are for admin only.
2. **Pooler mode: transaction.** `?pgbouncer=true&connect_timeout=10` in the connection string. Transaction-mode pooling invalidates prepared statements between transactions — keep your driver using simple query protocol or set `prepareThreshold: 0`.
3. **Branch the database for every PR.** Neon branches are copy-on-write from any point in time; a PR gets its own database for free. Wire your CI to create a branch on PR open and drop it on close.
4. **Bound connection count per instance.** Serverless functions multiply connections fast; always go through the pooler, and if you are on Vercel, use `@neondatabase/serverless` with HTTP or WebSocket driver rather than TCP.
5. **Schema migrations via Prisma / Drizzle / raw SQL.** Neon is plain Postgres; pick the migration tool you already use. Preview branches run the same migrations — do not let branches diverge schema.
6. **Scale-to-zero knobs:** `autosuspend_delay_seconds` (default 300) controls how long idle compute stays up. For dev, lower to 60; for prod with bursty traffic, raise to 900 to avoid thrashing.
7. **Point-in-time restore on the main branch only.** For experimental destructive changes, branch first — PITR on the main branch is the last-resort backup.
8. **Monitor compute autosuspend events.** A pattern of wake-sleep-wake every few seconds means your workload should not scale to zero; configure `autosuspend_delay_seconds` up or use the "always-on" compute.

## Example invocation

```ts
// db.ts — HTTP driver for Vercel / serverless
import { neon } from '@neondatabase/serverless';

export const sql = neon(process.env.DATABASE_URL!);

// queries.ts
export async function getTasks(userId: string) {
  return sql`SELECT id, title, status FROM tasks WHERE user_id = ${userId}`;
}
```

```yaml
# .github/workflows/pr-branch.yml
name: Neon branch per PR
on:
  pull_request:
    types: [opened, closed]

jobs:
  branch:
    runs-on: ubuntu-latest
    steps:
      - uses: neondatabase/create-branch-action@v5
        if: github.event.action == 'opened'
        with:
          project_id: ${{ secrets.NEON_PROJECT_ID }}
          branch_name: pr-${{ github.event.number }}
          api_key: ${{ secrets.NEON_API_KEY }}
      - uses: neondatabase/delete-branch-action@v3
        if: github.event.action == 'closed'
        with:
          project_id: ${{ secrets.NEON_PROJECT_ID }}
          branch: pr-${{ github.event.number }}
          api_key: ${{ secrets.NEON_API_KEY }}
```

## Outputs

- Pooled connection string wired into the app.
- CI workflow creating a branch per PR, deleting on close.
- Migrations that run on both main and preview branches.
- `autosuspend_delay_seconds` tuned to the traffic shape.

## Common failure modes

- Direct (non-pooled) connection string in serverless; exhausts connections within minutes.
- Assuming prepared statements work through the pooler; transaction-mode breaks them.
- Letting preview branches drift schema; PRs pass CI but break on merge.
- Scale-to-zero with a wake-every-30-seconds pattern; compute thrashes, latency suffers.
