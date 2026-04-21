# PollyPM Extensibility Architecture

This document is retained only as a lightweight redirect for older links. It
is no longer the source of truth for current plugin discovery paths, manifests,
or author workflow.

Use these docs instead:

- `docs/plugin-authoring.md` — current how-to for building and installing plugins
- `docs/plugin-discovery-spec.md` — discovery precedence and manifest rules
- `docs/provider-plugin-sdk.md` — provider adapter surface
- `docs/extensible-rail-spec.md` — rail extension surface
- `docs/v1/04-extensibility-and-plugin-system.md` — broader v1 architecture/spec chapter

Historical note:

- Older paths such as `<project>/.pollypm-state/plugins/` and
  `~/.config/pollypm/plugins/` are obsolete.
- Current discovery roots are built-ins, Python entry points,
  `~/.pollypm/plugins/*/`, and `<project>/.pollypm/plugins/*/`.
