# [CANCELLED] SaveTheNovel queue needs a routing decision

**What happened:** savethenovel/179 was promoted from draft to queued. It is a human/operator handoff because savethenovel/94 (production DreamHost deploy) and savethenovel/149 (advisor review) have both been queued without claim activity.

**Decision needed:** Decide whether to keep pushing the deploy/advisor queue, cancel stale advisory work, or reroute the deploy task to an available worker.

**Review:** pm task get savethenovel/179, pm task get savethenovel/94, and pm task get savethenovel/149.
