#!/bin/bash
# 48h-magic-run loop guardian — durability backstop for G2.
# Dormant while the primary in-session driver keeps the journal fresh.
# If the journal goes stale > THRESH min (primary session died), re-arm the
# loop by invoking one headless `claude -p` tick. Lockfile prevents overlap.
# Mirrors the proven codex-watcher tmux+caffeinate pattern. Best-effort backstop
# (cannot be fully validated without killing the primary session).
set -u
JOURNAL=/Users/sam/dev/pollypm/docs/test-plan/journals/2026-05-30-48h-magic.md
PROMPT=/Users/sam/dev/pollypm/docs/test-plan/.loop-tick-prompt.txt
LOG=/Users/sam/dev/pollypm/reports/loop-guardian.log
LOCK=/tmp/pm-loop-guardian.lock
THRESH=30   # minutes of journal staleness before re-arm
mkdir -p "$(dirname "$LOG")"
echo "[$(date '+%F %T')] guardian started (thresh ${THRESH}min)" >> "$LOG"
while true; do
  now=$(date +%s)
  mtime=$(stat -f %m "$JOURNAL" 2>/dev/null || echo 0)
  age=$(( (now - mtime) / 60 ))
  if [ "$age" -gt "$THRESH" ]; then
    if mkdir "$LOCK" 2>/dev/null; then
      echo "[$(date '+%F %T')] journal stale ${age}min > ${THRESH} — re-arming loop via claude -p" >> "$LOG"
      cd /Users/sam/dev/pollypm || true
      /Users/sam/.local/bin/claude -p "$(cat "$PROMPT")" --dangerously-skip-permissions >> "$LOG" 2>&1
      echo "[$(date '+%F %T')] re-arm tick exited ($?)" >> "$LOG"
      rmdir "$LOCK" 2>/dev/null
    fi
  fi
  sleep 600
done
