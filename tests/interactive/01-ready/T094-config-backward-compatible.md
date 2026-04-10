# T094: Config Changes Backward-Compatible

**Spec:** v1/15-migration-and-stability
**Area:** Migration
**Priority:** P1
**Duration:** 10 minutes

## Objective
Verify that configuration file format changes are backward-compatible: old config files (without new fields) still work correctly, with new fields receiving sensible defaults.

## Prerequisites
- A working Polly installation with existing configuration
- Knowledge of any new config fields added in recent updates

## Steps
1. Read the current config file: `cat .pollypm/pollypm.toml` (or equivalent).
2. Record the current settings: `pm config show > /tmp/config-current.txt`.
3. Identify a new config field (from recent changelog or release notes). If none exist, create a test scenario by noting the expected default for a known field.
4. Remove the new field from the config file (simulate an old config that doesn't have the new field). Edit the config file to remove the field.
5. Run `pm config show` and verify:
   - No error parsing the config
   - The removed field now shows its default value
   - All other fields retain their existing values
6. Run `pm up` and verify sessions start correctly with the old-format config.
7. Add an unknown field to the config: add `totally_fake_field = "test"` to the config file.
8. Run `pm config show` — the system should either ignore the unknown field or warn about it, but NOT crash.
9. Run `pm up` with the unknown field present — it should still work.
10. Restore the original config and verify everything works as expected.

## Expected Results
- Old config files (missing new fields) load without errors
- New fields get sensible default values when missing
- All existing fields retain their values
- Unknown/obsolete fields do not cause crashes (ignored or warned)
- Sessions start correctly with old-format configs
- Backward compatibility is maintained across versions

## Log
