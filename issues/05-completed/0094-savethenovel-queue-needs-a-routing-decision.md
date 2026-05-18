# [CANCELLED] SaveTheNovel queue needs a routing decision

**What happened:**  was promoted from draft to queued. It is a human/operator handoff because  (production DreamHost deploy) and  (advisor review) have both been queued without claim activity.\n\n**Decision needed:** Decide whether to keep pushing the deploy/advisor queue, cancel stale advisory work, or reroute the deploy task to an available worker.\n\n**Review:** ID:       savethenovel/179
Title:    Project savethenovel has 2 queued task(s) but no claim / execution / status-change activity for ~46 min.
Status:   queued
Priority: high
Project:  savethenovel
Type:     task
Desc:     TIER HANDOFF

Question: Project savethenovel is wedged in a way the architect can't unstick. What structural change do you want to apply?

Evidence:
- queued_subjects:
  - savethenovel/94
  - savethenovel/149
- queued_last_updated:
  - savethenovel/94: 2026-05-08T05:15:35.085899+00:00
  - savethenovel/149: 2026-05-09T01:58:29.526757+00:00
- last_activity_at: 2026-05-18T17:06:57.840243+00:00
- last_activity_event: task.status_changed
- threshold_seconds: 1800
Roles:    {"requester": "user", "operator": "user"}
Tokens:   in=0 out=0 sessions=0
Context:
  [cli] Task is queueable but underspecified: missing acceptance criteria; missing verification expectation; missing relevant files or explicit 'discover files'., ID:       savethenovel/94
Title:    Execute production DreamHost deploy
Status:   queued
Priority: high
Project:  savethenovel
Type:     task
Desc:     Actually deploy the completed Save The Novels static site to DreamHost using the Module 8 deploy package. Start by reading docs/deploy-runbook.md and scripts/deploy.sh. Confirm whether .env contains DEPLOY_HOST, DEPLOY_USER, and a real DEPLOY_WEB_ROOT. If DreamHost web root or SSH access is missing, stop and escalate with pm notify using a plain-English blocker. If configured, run npm run build, npm run deploy for dry-run review, npm run deploy:execute for the real rsync upload, then verify the live site.
Roles:    {"worker": "worker", "reviewer": "reviewer", "requester": "architect"}
Tokens:   in=0 out=0 sessions=0, and ID:       savethenovel/149
Title:    Advisor review for savethenovel
Status:   queued
Priority: normal
Project:  savethenovel
Type:     task
Desc:     Review recent project trajectory, identify stalls or human blockers, and emit a structured advisor decision.
Roles:    {"advisor": "advisor"}
Tokens:   in=0 out=0 sessions=0.
