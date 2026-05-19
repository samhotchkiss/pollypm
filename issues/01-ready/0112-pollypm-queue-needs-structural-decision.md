# PollyPM queue needs structural decision

Project pollypm still has queued work but no claim, execution, or status-change activity during the watchdog scan window.

Current queued tasks:
- pollypm/36: Fix critique output_present gate ordering
- pollypm/42: Advisor review for pollypm
- pollypm/109: pm status crashing: missing model_registry.toml

The stale draft watchdog handoff is pollypm/111. I am not queueing that draft because it is a human-facing structural decision, not worker implementation work, and adding it to the project queue would worsen the idle-queue signal.

Question from watchdog: Project pollypm is wedged in a way the architect cannot unstick. What structural change do you want to apply?

