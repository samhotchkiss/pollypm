# [CANCELLED] Decision needed: pollypm queued tasks are not being claimed

**Finding:** [launch] attach_existing: cockpit healthy: attach without respawning the rail
Switching to tmux session pollypm has queued work but no recent claim/execution activity.\n\n**Queued tasks:**\n-  — real high-priority platform bug: fix  gate ordering.\n-  — advisor review task.\n\n**What I found:** ID:       media/1
Title:    Library-wide music duplicate and Various Artists cleanup
Status:   queued
Priority: high
Project:  media
Type:     epic
Desc:     Comprehensive cleanup of the music library on the seedbox at sfsam@frost.usbx.me:/home/sfsam/media/Music. Builds on prior work (whole-album duplicates handled by bulk-triage; albums like Yelawolf War Story, Willie Nelson Live At Budokan, Toby Keith Clancy's Tavern, Lumineers Wrigley, Yellow Stitches already cleaned). Now extending scope to: (1) per-track duplicates within otherwise-clean albums (e.g. Live/Throwing Copper (1994) has two case-different copies of '02 - Selling the Drama'); (2) the 216-folder Various Artists graveyard, where many entries are clearly single-artist albums orphaned from their proper artist folder, sometimes containing tracks that already exist under the real artist (e.g. Various Artists/Throwing Copper holds a third copy of Selling The Drama). User example: Throwing Copper by Live.
Roles:    {"worker": "worker_media", "reviewer": "russell"}
Tokens:   in=0 out=0 sessions=0 currently returns , not either [launch] attach_existing: cockpit healthy: attach without respawning the rail
Switching to tmux session pollypm task, so the issue appears to be queue/assignment routing or worker capacity rather than a missing draft promotion.\n\n**Action I took:** I am cancelling the audit handoff draft  because queueing it would create another unclaimed meta-task.\n\n**Decision needed:** either assign/claim  to an available worker, adjust queue priority/routing so [launch] attach_existing: cockpit healthy: attach without respawning the rail
Switching to tmux session pollypm work is picked up, or explicitly defer/cancel the queued [launch] attach_existing: cockpit healthy: attach without respawning the rail
Switching to tmux session pollypm tasks.
