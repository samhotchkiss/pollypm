# Vendored: visual-explainer

This directory is a vendored copy of the `visual-explainer` Claude Code plugin
authored by nicobailon. It is shipped inside PollyPM so that Archie (and any
other agent) can invoke the full skill — SKILL.md, commands, templates,
references, and scripts — without a network fetch or manual plugin install.

## Upstream

- Repository: https://github.com/nicobailon/visual-explainer
- Subtree vendored: `plugins/visual-explainer/`
- Upstream commit: `9a97a5818bebbcb61cccf8941d533b82a1ce958b`
- Upstream commit date: 2026-03-28
- Upstream plugin version: `0.6.3` (see `.claude-plugin/plugin.json`)
- License: MIT (see `LICENSE`)

## Vendored on

- Date: 2026-04-17
- Vendored by: PollyPM (feat/magic-visual-explainer-vendor)

## How this plugs in

PollyPM's magic loader (`pollypm.rules.discover_magic`) walks
`src/pollypm/defaults/magic/`. As of the vendor commit, the loader treats a
subdirectory containing `SKILL.md` as a directory-style magic skill; the
skill's canonical file is `SKILL.md` and its commands live under
`commands/`. Single-file skills (`itsalive.md`, `deploy-site.md`) continue
to work unchanged.

## Updating

To refresh against upstream:

1. `git clone --depth=1 https://github.com/nicobailon/visual-explainer.git /tmp/vexp-upstream`
2. `rm -rf src/pollypm/defaults/magic/visual-explainer/{SKILL.md,commands,templates,references,scripts,.claude-plugin,LICENSE}`
3. `cp -R /tmp/vexp-upstream/plugins/visual-explainer/. src/pollypm/defaults/magic/visual-explainer/`
4. `cp /tmp/vexp-upstream/LICENSE src/pollypm/defaults/magic/visual-explainer/LICENSE`
5. Update this file with the new commit SHA, commit date, and plugin version.

Do NOT vendor `.git/`, `README.md`, `CHANGELOG.md`, `banner.png`, or any
other files outside the `plugins/visual-explainer/` subtree. We only want
the runnable plugin contents.
