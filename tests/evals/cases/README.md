# Evals Case Schema

Each `*.yaml` file in this directory is a single agent-behavior eval case.

Required fields:

- `id`: stable case identifier used in reports.
- `role`: `architect`, `advisor`, `worker`, or `operator`.
- `session_template`: chat session name. Use `<project>` or `{project}` for the CLI `--project` value.
- `prompt`: prompt sent through `POST /api/v1/chat/{session_name}/send`.
- `assertions`: assertion mapping.

Optional fields:

- `timeout_seconds`: live daemon polling timeout. Defaults to `90`.
- `canned_response`: per-case dry-run response override.

Supported assertions:

- `must_contain_at_least`: structural counts such as `candidates_count`, `paragraphs_count`, and `bullets_count`.
- `must_match_regex`: regexes that must match the response.
- `must_not_match_regex`: regexes that must not match the response.
- `must_contain_keywords`: case-insensitive literal keywords that must appear.
- `must_not_contain_keywords`: case-insensitive literal keywords that must not appear.
- `response_category`: heuristic category: `planning`, `question`, `action`, `refusal`, or `critique`.
