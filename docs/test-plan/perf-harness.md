# Performance Harness

Canonical lane E scripts:

```bash
scripts/perf/measure_http.sh --dry-run
scripts/perf/measure_http.sh --scenarios dashboard,sessions,messages \
  --json-out /tmp/pollypm-perf.json \
  --markdown-out /tmp/pollypm-perf.md
python3 scripts/perf/measure_http.py poll --duration 300 --clients 10
python3 scripts/perf/measure_http.py resources
make perf-snapshot
```

The harness uses only public Web API requests and operator shell commands. It
does not import from `src/pollypm`. Mutating scenarios (`claim`, `send`) require
`--allow-mutating` so routine read measurements do not accidentally alter daemon
state.

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
