---
## Summary

PollyPM v1 is an active system in daily use. All new features must be built incrementally without breaking existing functionality. This document defines the stability principles, incremental delivery model, migration strategies for state and configuration, and the plugin-first extensibility approach that keeps core changes rare and high-confidence. Breaking changes require explicit migration paths and are never silent.

---

# 15. Migration and Stability

## Stability Principles

PollyPM is not a greenfield project waiting for a first release. It runs every day, managing real agent sessions on real projects. Every change must respect this reality.

### Principle 1: Do Not Break What Works

PollyPM's current functionality is the baseline. Any change that causes a regression in existing behavior is unacceptable, regardless of what new capability it enables.

This means:

- Existing `pollypm.toml` configurations must continue to work after updates
- Existing SQLite state stores must continue to be readable after schema changes
- Existing CLI commands must retain their current behavior (new flags and commands are fine; changing existing ones requires a deprecation cycle)
- Existing plugins must continue to load and function after core changes
- Existing tmux session structures must be compatible with updated PollyPM versions

### Principle 2: Clean Interfaces Before Integration

New features are built behind clean interfaces and tested independently before being wired into the core system.

This means:

- New modules have well-defined APIs that can be tested in isolation
- Integration with existing code happens only after the new module is proven to work
- The integration point is as narrow as possible — ideally a single function call or hook registration
- If integration would require modifying many existing files, the design needs rethinking

### Principle 3: Plugins First, Core Second

The plugin system (doc 04) is the primary mechanism for adding new capabilities.

New functionality that fits one of these categories should be a plugin:

- Issue management backends (GitHub, Linear, Jira, etc.)
- Provider adapters (new CLI agents)
- Heartbeat strategies (custom health classification logic)
- Memory backends (custom storage for project context)
- Transcript sources (new transcript formats)

Core changes are reserved for:

- Bug fixes in existing core functionality
- Performance improvements that do not change behavior
- New plugin hooks that enable plugin-based extensibility
- Infrastructure changes that all plugins depend on (state store schema, config format)

### Principle 4: Forward-Compatible Changes Only

Every change to shared state (database, config, file formats) must be forward-compatible:

- New columns in SQLite tables must have defaults
- New tables are fine — they do not affect existing queries
- Existing columns are never dropped or renamed without a migration
- New config keys must have defaults that preserve existing behavior
- Existing config keys are never removed without a deprecation cycle
- File format changes must be backward-readable (new fields are optional)


## Incremental Delivery Model

Large features are decomposed into small, independently valuable pieces. Each piece is merged and verified before the next one starts.

### Decomposition Rules

A feature is ready to implement when it has been broken down such that:

1. **Each piece is independently testable.** It has its own unit and integration tests that pass without the other pieces.

2. **Each piece is independently mergeable.** Merging it does not break existing functionality and does not require the other pieces to be present.

3. **Each piece is independently valuable.** Even if the other pieces are never built, this piece provides some benefit (even if just internal code quality or a building block for future work).

4. **Each piece has a rollback plan.** If it causes problems after merge, it can be reverted without affecting the pieces that came before it.

### Delivery Sequence

For a multi-piece feature, the delivery sequence is:

```
Piece 1: Build → Test → Verify → Merge → Monitor
Piece 2: Build → Test → Verify → Merge → Monitor
Piece 3: Build → Test → Verify → Merge → Monitor
Integration: Test pieces together → Verify → Merge → Monitor
```

The integration step is separate and explicit. It is where the pieces are wired together and tested as a whole. This step often reveals issues that per-piece testing missed, which is why it happens after all pieces are individually stable.

### What "Monitor" Means

After each merge, there is a monitoring period where the change runs in production use:

- Does PollyPM still start correctly?
- Do existing sessions still launch and run?
- Does the heartbeat still function?
- Are there any new errors in the event log?
- Does the TUI still display correctly?

The monitoring period is typically one operational cycle (one day of active use). If problems appear, the change is reverted before proceeding to the next piece.

### Anti-Pattern: Big-Bang Integration

The following pattern is explicitly prohibited:

```
Build all pieces in a long-lived branch
  → Test them together at the end
    → Merge everything at once
      → Hope nothing breaks
```

This fails because:

- Bugs accumulate and interact in ways that are hard to diagnose
- The merge is risky and reverting means losing all work
- Integration issues are discovered late when they are expensive to fix
- The branch diverges from main, creating merge conflicts


## Plugin-First Extensibility

Plugins are the primary mechanism for adding capabilities without modifying core code. This keeps core changes rare, small, and high-confidence.

### What Goes in Plugins

| Capability | Plugin Type | Reference |
|------------|------------|-----------|
| New issue management backends | Issue backend plugin | Doc 06 |
| New provider CLI adapters | Provider adapter plugin | Doc 05 |
| New heartbeat strategies | Heartbeat strategy plugin | Doc 10 |
| New memory/context backends | Memory backend plugin | Doc 04 |
| New transcript source parsers | Transcript source plugin | Doc 04 |
| Custom checkpoint strategies | Checkpoint plugin | Doc 12 |
| Custom alert handlers | Alert handler plugin | Doc 13 |

### What Stays in Core

| Capability | Rationale |
|------------|-----------|
| Session lifecycle management | Fundamental orchestration — all plugins depend on it |
| Tmux layer | Shared infrastructure — not provider-specific |
| State store (SQLite) | Shared data layer — plugins read/write through defined APIs |
| Config loading | Must work before plugins are loaded |
| Plugin loader itself | Bootstrap dependency — cannot be a plugin |
| CLI and TUI framework | Shared UI infrastructure |
| Heartbeat supervisor framework | Core monitoring loop — strategies are plugins but the loop is core |

### Plugin Stability Contract

Plugins depend on core interfaces. Those interfaces have a stability guarantee:

- Plugin hook signatures are versioned
- Existing hooks are not removed or changed without a deprecation cycle
- New hooks can be added freely (they do not affect existing plugins)
- If a core change would break a plugin interface, the change must include a migration path for existing plugins
- Plugin authors can specify which core version they are compatible with


## State Store Migration

The SQLite state store evolves as features are added. Migrations keep existing data accessible while enabling new functionality.

### Schema Versioning

The state store contains a `schema_version` table:

```sql
CREATE TABLE schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL,
    description TEXT
);
```

Each row represents a migration that has been applied. The current schema version is the highest version number in this table.

### Migration Scripts

Migrations are Python scripts in a dedicated directory:

```
pollypm/migrations/
  001_initial_schema.py
  002_add_checkpoints_table.py
  003_add_alert_auto_action.py
  ...
```

Each migration script contains:

- A `version` constant (integer, matching the filename number)
- A `description` string
- An `upgrade()` function that applies the migration
- SQL statements that modify the schema

### Migration Properties

| Property | Requirement |
|----------|-------------|
| Forward-only | Migrations go up, never down. Rollback is done by reverting code and restoring a backup. |
| Idempotent | Running a migration twice has the same effect as running it once. Uses `IF NOT EXISTS` and similar guards. |
| Additive | New columns have defaults. New tables are fine. Existing columns are never dropped. |
| Atomic | Each migration runs in a transaction. If any statement fails, the entire migration is rolled back. |
| Automatic | Migrations run automatically on PollyPM startup. No manual intervention required. |

### Migration Execution Flow

On startup:

1. Open the SQLite database
2. Read the current schema version from `schema_version`
3. Find all migration scripts with version numbers higher than current
4. Apply each migration in order, within a transaction
5. Record each applied migration in `schema_version`
6. If any migration fails, roll back the transaction and abort startup with a clear error

### Safe Schema Changes

| Change Type | Safe? | Notes |
|-------------|-------|-------|
| Add new table | Yes | Does not affect existing queries |
| Add column with default | Yes | Existing rows get the default value |
| Add column without default | No | Existing rows would have NULL; use a default |
| Add index | Yes | Improves performance, does not change data |
| Drop column | No | Breaks existing queries. Use deprecation. |
| Rename column | No | Breaks existing queries. Add new column, migrate data, deprecate old. |
| Drop table | No | Breaks existing queries. Only after all references are removed. |
| Change column type | No | SQLite is flexible here but it can break application assumptions. |

### Backup Before Migration

Before applying any migration, PollyPM creates a backup of the database file:

```
~/.pollypm/pollypm.db → ~/.pollypm/backups/pollypm-pre-v003-20260409.db
```

This ensures that if a migration goes wrong, the original data can be recovered manually.


## Config Migration

The `pollypm.toml` configuration file evolves alongside the system. Config changes must be backward-compatible.

### Config Versioning

The config file may optionally specify a version:

```toml
config_version = 1
```

If omitted, version 1 is assumed. This field enables future format migrations.

### Backward Compatibility Rules

| Rule | Details |
|------|---------|
| New keys get defaults | Adding `[alerts.session_stuck]` with `auto_recover = false` as default preserves existing behavior |
| Removed keys are warned | A deprecated key produces a warning on startup but does not cause an error |
| Renamed keys have aliases | If `heartbeat_interval` is renamed to `heartbeat.interval`, the old name still works with a deprecation warning |
| Type changes are forbidden | A key that was a string cannot become an integer without a migration |
| Structural changes need migration | Moving a key from top-level to a section requires a migration command |

### Deprecation Cycle

When a config key needs to change:

1. **Version N**: New key is introduced alongside old key. Old key still works. Warning on startup if old key is used.
2. **Version N+1** (at least one minor release later): Old key produces an error with a clear message explaining the migration.
3. **Version N+2**: Old key is removed from the parser.

The minimum deprecation cycle is one minor version. This gives users at least one release to update their configs.

### Config Migration Command

For structural changes that cannot be handled by simple aliasing:

```bash
pm config migrate
```

This command:

- Reads the current `pollypm.toml`
- Applies all pending config migrations
- Writes the updated file
- Preserves comments and formatting where possible
- Creates a backup of the original file
- Reports what changed

Config migration is manual (not automatic on startup) because config files are user-edited and users should review changes.


## What Is NOT Migrated

Some state from earlier PollyPM development is not carried forward into v1 if doing so would compromise the design.

### Prototype State

If PollyPM had pre-v1 prototype state (experimental databases, ad-hoc config formats, prototype scripts), this state is not migrated:

- v1 starts with a clean state store, initialized by the v1 schema migrations
- Any prototype data that is valuable must be manually extracted and imported
- Prototype configs must be rewritten in the v1 `pollypm.toml` format

Rationale: carrying forward prototype debt creates maintenance burden and compatibility constraints that slow down v1 development. A clean start is worth the one-time cost of re-entering configuration.

### Pre-Plugin Transcripts

JSONL transcripts from before the formal transcript source plugin system (doc 04) are handled on a best-effort basis:

- If the transcript format is compatible with a v1 transcript source plugin, it will be read
- If the format is incompatible, the transcript is ignored (not deleted, just not indexed)
- No special-purpose parsing code is maintained for legacy transcript formats

### Incomplete Project History

Project dossiers (doc 07) start fresh on first import rather than trying to reconstruct from incomplete state:

- The project history import runs against the current state of the repository and issue tracker
- It does not attempt to reconstruct historical context from partial logs or old checkpoints
- Historical context accumulates naturally as PollyPM operates on the project going forward

Rationale: reconstructing history from incomplete artifacts is error-prone and produces unreliable context. Starting fresh and building forward produces higher-quality project knowledge.


## Override Hierarchy

The override hierarchy — built-in defaults, then user-global overrides, then project-local overrides — is a core architectural decision that must be preserved across all migrations.

```
built-in defaults → ~/.pollypm/ (user-global) → <project>/.pollypm/ (project-local)
```

This hierarchy applies to configuration, rules, plugin settings, and policies. It is the mechanism by which PollyPM remains opinionated but pluggable: built-in defaults provide strong out-of-the-box behavior, and each override layer allows progressively more specific customization.

### Preservation Across Migrations

Any migration that touches configuration, rules, or policy files must preserve this hierarchy:

- Built-in defaults can be updated freely — they are shipped with PollyPM and users do not edit them directly
- User-global overrides in `~/.pollypm/` are user-owned and must never be modified by migrations
- Project-local overrides in `<project>/.pollypm/` are project-owned and must never be modified by migrations
- Migrations that change built-in defaults must verify that existing user-global and project-local overrides still compose correctly with the new defaults

### Agent-Driven Configuration Patches

When agents or automated processes need to modify configuration, they create override files — they never modify built-in defaults. This means:

- Built-in defaults can be safely updated in new PollyPM versions without clobbering user customizations
- Agent-generated overrides are clearly identifiable (they live in the override directories, not alongside built-in files)
- Conflicts between built-in updates and user overrides are resolved by the standard override hierarchy, not by merge logic
- Rolling back an agent-generated configuration change is as simple as removing the override file


## Rollback Strategy

Every change has a rollback plan. The specifics depend on the type of change.

| Change Type | Rollback Method | Notes |
|-------------|----------------|-------|
| Code changes | `git revert` the merge commit | If schema migration was included, also restore DB backup |
| Schema migrations | Restore pre-migration database backup | Data written after migration may be lost |
| Config changes | Restore backup from `pm config migrate` | Also revert code depending on new keys |
| Plugin changes | Disable in config or remove plugin file | Core unaffected; plugin state remains inert |


## Migration Checklist

Before merging any change that involves state or config evolution:

| Item | Check |
|------|-------|
| Schema migration script exists | If the change modifies the database schema |
| Migration is idempotent | Running it twice has the same effect |
| Migration has a backup step | Pre-migration backup is created |
| Existing configs still load | Current `pollypm.toml` files are unaffected |
| New config keys have defaults | Defaults preserve existing behavior |
| Deprecated keys produce warnings | Old names are not silently dropped |
| Plugin interfaces are preserved | Existing plugins still load and function |
| Rollback plan is documented | In the PR description or commit message |
| Tests cover the migration | Both the migration itself and the migrated state |
| Full test suite passes | Including integration tests with the new schema |


## Opinionated but Pluggable

The migration and stability rules in this document are PollyPM's opinionated defaults. They represent strong conventions, but every policy is configurable:

- **Migration approach** (forward-only, additive, automatic on startup) is the default. Projects with different database management needs can override migration behavior.
- **Stability principles** (backward compatibility, deprecation cycles, plugin-first extensibility) are defaults enforced by convention. Projects can adopt stricter or more relaxed stability policies in project-local configuration.
- The **override hierarchy** (built-in, user-global, project-local) is itself the core mechanism that makes everything else pluggable. It is the one architectural decision that is not overridable — it is the foundation on which all overrides rest.

This pattern — strong defaults that are fully replaceable — applies throughout PollyPM. Checkpoint strategy, security policies, testing requirements, and migration approach are all configurable and overridable.


## Resolved Decisions

1. **Incremental delivery, not big-bang.** Features are broken into small pieces, each independently tested and merged. Big-bang integrations are explicitly prohibited because they accumulate risk and make debugging impossible.

2. **Plugins for extensibility.** New backends, adapters, and strategies are plugins. Core changes are reserved for infrastructure that all plugins depend on. This keeps the core stable and changes localized.

3. **Forward-only migrations.** Database migrations go up, never down. Rollback is done by restoring backups, not by running reverse migrations. This keeps migration logic simple and avoids the bugs that come from maintaining bidirectional schema transforms.

4. **Config backward compatibility is required.** New config keys have defaults. Deprecated keys produce warnings. Breaking config changes require a migration command. Users are never surprised by config-related failures after an update.

5. **Breaking changes require a migration command.** When a change cannot be backward-compatible (structural config changes, major schema reorganizations), a migration command handles the transition. Migrations never run silently — users must opt in.

6. **No prototype state debt.** Pre-v1 prototype state is not migrated if doing so would compromise the v1 design. A clean start with the v1 schema is preferred over carrying forward compatibility constraints from experimental code.


## Cross-Doc References

- Account isolation and configuration model: [02-configuration-accounts-and-isolation.md](02-configuration-accounts-and-isolation.md)
- Plugin system and extensibility: [04-extensibility-and-plugin-system.md](04-extensibility-and-plugin-system.md)
- Provider adapter plugins: [05-provider-sdk.md](05-provider-sdk.md)
- Issue management plugins: [06-issue-management.md](06-issue-management.md)
- Project history import: [07-project-history-import.md](07-project-history-import.md)
- Heartbeat strategy plugins: [10-heartbeat-and-supervision.md](10-heartbeat-and-supervision.md)
- Checkpoint evolution and storage: [12-checkpoints-and-recovery.md](12-checkpoints-and-recovery.md)
- Testing and verification requirements: [14-testing-and-verification.md](14-testing-and-verification.md)
