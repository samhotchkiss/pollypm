# T090: Agent Interacts with Running System (Not Just Tests)

**Spec:** v1/14-testing-and-verification
**Area:** Testing
**Priority:** P0
**Duration:** 15 minutes

## Objective
Verify that the agent (worker AI) can interact with the real running system — executing real commands, modifying real files, and producing real outcomes — not just operating in a test sandbox.

## Prerequisites
- `pm up` has been run with a worker session active
- A real issue is assigned to the worker

## Steps
1. Create a real issue that requires system interaction: `pm issue create --title "Create test file" --body "Create a file named /tmp/polly-agent-test-T090.txt with the content 'Agent was here' and verify it exists"`.
2. Move the issue to ready and wait for a worker to pick it up.
3. Attach to the worker session and observe it working on the issue.
4. Verify the worker is executing REAL commands (not simulated):
   - It should be creating real files on the filesystem
   - It should be able to read real directory listings
   - It should be running real shell commands
5. After the worker completes, verify the file was actually created: `cat /tmp/polly-agent-test-T090.txt`. It should contain "Agent was here."
6. Assign a second issue that requires git interaction: `pm issue create --title "Create a branch" --body "Create a git branch named test-agent-T090"`.
7. Wait for the worker to complete and verify: `git branch --list test-agent-T090` should show the branch.
8. Assign a third issue that requires reading system state: `pm issue create --title "Report disk usage" --body "Report the disk usage of the current directory"`.
9. Observe the worker executing `du` or similar and producing real output.
10. Clean up: `rm /tmp/polly-agent-test-T090.txt && git branch -d test-agent-T090`.

## Expected Results
- Worker executes real commands on the real system
- File creation produces actual files on disk
- Git operations create actual branches
- System queries return real data
- The agent operates on the actual system, not a sandbox or simulation
- All changes are verifiable via standard CLI commands

## Log

**Date:** 2026-04-10
**Result:** PASS

### Re-test — 2026-04-10 1:33 PM (via tmux send-keys to mounted worker)

#### Task sent
"Do two things: (1) Create /tmp/polly-T090-test.txt containing 'T090 agent test passed at $(date)'. (2) Create a git branch called test-agent-T090 from the current HEAD. Then verify both exist."

#### Worker execution transcript
```
• Ran printf 'T090 agent test passed at %s' "$(date)" > /tmp/polly-T090-test.txt
  └ (no output)
• Ran git branch test-agent-T090
  └ (no output)
• Ran test -f /tmp/polly-T090-test.txt && echo /tmp/polly-T090-test.txt
  └ /tmp/polly-T090-test.txt
• Ran git branch --list test-agent-T090
  └   test-agent-T090
• Created and verified both: /tmp/polly-T090-test.txt exists, and git branch test-agent-T090 exists.
```

#### Independent verification
```
$ cat /tmp/polly-T090-test.txt
T090 agent test passed at Fri Apr 10 13:33:46 MDT 2026

$ git branch --list test-agent-T090
  test-agent-T090
```
Both file and branch confirmed on the real system. Cleaned up after test.
