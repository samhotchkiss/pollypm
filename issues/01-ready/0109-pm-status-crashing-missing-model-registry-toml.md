# pm status crashing: missing model_registry.toml

**Issue:** `pm status` crashes immediately on this host.

**Error:** `FileNotFoundError: /Users/sam/.local/share/uv/tools/pollypm/lib/python3.13/site-packages/pollypm/model_registry.toml`

**Stack:** `pollypm/model_registry.py:150` → `_read_shipped_registry_text` → file missing from the installed package.

**Impact:** Operator loop can't run `pm status` for health checks. `pm inbox` still works.

**Repro:** `pm status`

**Likely fix:** Reinstall pollypm (`uv tool install --force pollypm`) or check that `model_registry.toml` is in the package's MANIFEST/pyproject `include`.

---

**Separate note — likely injection attempts:** This session received a "RECOVERY MODE: RESUMING FROM CHECKPOINT" preamble that matches the exact pattern your inbox has 21+ drafts flagging as fake (`inbox/21` through `inbox/80`, up to "131st fake RECOVERY MODE injection"). I ignored it and trusted the harness `<operator-state>` (all projects healthy, 0 real inbox items). Worth confirming the source if you haven't already.

## Acceptance Criteria

pm status no longer raises FileNotFoundError when the installed package cannot read pollypm/model_registry.toml. Packaging keeps src/pollypm/model_registry.toml included in built wheels and sdists. Tests cover shipped registry resource loading or package-data inclusion.
