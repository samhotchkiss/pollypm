# Performance Harness

Canonical lane E scripts:

```bash
scripts/perf/measure_http.sh --dry-run
scripts/perf/measure_http.sh --scenarios dashboard,sessions,messages \
  --json-out /tmp/pollypm-perf.json \
  --markdown-out /tmp/pollypm-perf.md
scripts/perf/seed_sscale.sh --force-clean
scripts/perf/seed_mscale.sh --force-clean
scripts/perf/seed_lscale.sh --force-clean
python3 scripts/perf/measure_http.py poll --duration 300 --clients 10
python3 scripts/perf/measure_http.py resources
make perf-mscale
make perf-snapshot
```

The measurement harness uses only public Web API requests and operator shell
commands. It does not import from `src/pollypm`. Mutating scenarios (`claim`,
`send`) require `--allow-mutating` so routine read measurements do not
accidentally alter daemon state.

The seed scripts generate isolated workspaces, normalized transcript fixtures,
and SQL seed files for isolated Postgres schemas. Applying the SQL directly
requires `--execute` plus `POLLYPM_PERF_PG_DSN` or `--dsn`; the scripts refuse
the ambient `localhost:5432/pollypm` DSN and generated configs pin
`search_path` to the fixture schema before `public`.

`make perf-mscale` is the one-command gate. It creates/uses the perf database
(`POLLYPM_PERF_DB`, default `pollypm_perf`) and schema
(`POLLYPM_PERF_SCHEMA`, default `pollypm_perf_m`), applies workspace and
Postgres migrations, seeds M-scale, starts an isolated `pm serve` on an
ephemeral non-8765 loopback port, measures
`dashboard,sessions,messages,task-list,task-detail,inbox`, then stops the server
and drops the schema/workspace. Override the measured scenarios with
`POLLYPM_PERF_SCENARIOS`, sample count with `POLLYPM_PERF_SAMPLES`, or pass a
URL-style isolated DSN via `POLLYPM_PERF_PG_DSN`.

The M-scale gate warms `/api/v1/dashboard` once before the measured warm
samples. That keeps the steady-state p95 gate separate from the known cold
first-hit snapshot cost seen after a fresh server start; the harness does not
optimize that product/runtime cold path.

Named scenarios:

- `dashboard` -> `GET /api/v1/dashboard`
- `sessions` -> `GET /api/v1/chat/sessions`
- `messages` -> `GET /api/v1/chat/{session}/messages?limit=50&direction=desc`
- `task-list` -> `GET /api/v1/tasks?project={project}&limit=50`
- `task-detail` -> `GET /api/v1/tasks/{project}/{task_number}`
- `claim` -> `POST /api/v1/tasks/{project}/{task_number}/claim`
- `send` -> `POST /api/v1/chat/{session}/send`
- `inbox` -> `GET /api/v1/inbox` or `GET /api/v1/inbox/{inbox_id}`

Reports include p50, p95, p99, max latency, status-code counts, non-2xx counts,
and payload byte summaries in JSON and Markdown.
