# Morning Test Plan — 25 Stages

Each stage tests a specific aspect of the system through the actual user
interface. The focus is on observing agent judgment: Does Polly break work
into good tasks? Does Russell catch real problems? Does the system flow
without manual intervention?

---

## Stage 1: System Launch
**Do**: Run `pm up`. Open the cockpit.
**Watch for**: Rail renders with correct projects. Dashboard loads. No crashes. Rail width correct (file #102 if not).

## Stage 2: Dashboard Sanity
**Do**: Click the main dashboard (Polly in rail).
**Watch for**: Task counts, active work, recently completed, activity feed. All real data from existing projects.

## Stage 3: Hand Polly the Recipe Share Spec
**Do**: Send to Polly: "I have a new project. Read the spec at /Users/sam/dev/pollypm/docs/project-specs/01-recipe-share.md, register the project at /Users/sam/dev/recipe-share, and break it into tasks."
**Watch for**:
- Does Polly read the spec before creating tasks?
- Does she break it into reasonably sized pieces (not too big, not too granular)?
- Does she set up dependency chains correctly?
- Does she assign reviewer=russell?
- Does she queue the first unblocked task?
- Does she use `pm task create` (not `pm send`)?

## Stage 4: Watch the First Worker
**Do**: Wait for the first task to be claimed.
**Watch for**:
- Per-task worker session appears in storage closet
- Worker has a clear prompt with the task description
- Worker operates in an isolated worktree
- Task session appears under the project in the rail

## Stage 5: Click Into the Worker Session
**Do**: Click the task session under recipe-share in the rail.
**Watch for**: Live Claude session mounts in the right pane. You can see what the worker is doing.

## Stage 6: Watch Russell Review
**Do**: Wait for the first task to hit review.
**Watch for**:
- Russell actually reads the code (git diff, file reads)
- Russell checks acceptance criteria
- Russell verifies the code builds/runs
- Does Russell approve too easily, or is he genuinely checking quality?

## Stage 7: Does Russell Reject When Appropriate?
**Do**: Watch Russell's review decisions across multiple tasks.
**Watch for**:
- Does he catch real issues (bad imports, missing error handling, untested code)?
- Does he reject with specific, actionable feedback?
- Or does he rubber-stamp everything?
- If everything passes, the acceptance criteria may be too vague — that's a Polly problem.

## Stage 8: Dependency Chain Flow
**Do**: Watch tasks auto-unblock after approvals.
**Watch for**:
- Blocked tasks move to queued when their blockers complete
- Workers pick up newly queued tasks
- The chain flows without manual intervention
- File sync: `ls /Users/sam/dev/recipe-share/issues/` shows correct folders

## Stage 9: Hand Polly the GitHub Dashboard Spec
**Do**: Send to Polly: "New project. Read /Users/sam/dev/pollypm/docs/project-specs/02-github-dashboard.md, register at /Users/sam/dev/github-dash, and plan it out."
**Watch for**:
- Does Polly's task breakdown account for the API complexity?
- Does she create a caching/fetcher task before the UI tasks?
- Are the tasks actually implementable in isolation?
- Does she over-decompose (20 tiny tasks) or under-decompose (2 huge tasks)?

## Stage 10: Parallel Projects
**Do**: Both recipe-share and github-dash should have active tasks now.
**Watch for**:
- Dashboard shows work across both projects
- Workers don't interfere with each other (separate worktrees)
- Russell handles reviews from both projects
- Rail shows both projects in the "active" group with yellow indicators

## Stage 11: Project Dashboard Verification
**Do**: Click recipe-share in the rail.
**Watch for**:
- Dashboard highlights, sub-items expand
- Summary bar shows correct task counts
- Active tasks sorted by status (in_progress first)
- Completed tasks section with recent completions
- All data matches `pm task list -p recipe_share`

## Stage 12: Tasks View Deep Dive
**Do**: Click "Tasks" under recipe-share.
**Watch for**:
- Interactive list with status icons
- Click a completed task — detail view shows execution history
- If a task was rejected and reworked, see the full timeline
- Context log entries visible
- Press Escape → back to list. Press R → refreshes.

## Stage 13: Hand Polly the Markdown Blog Spec
**Do**: Send to Polly: "Third project. Read /Users/sam/dev/pollypm/docs/project-specs/03-markdown-blog.md, register at /Users/sam/dev/markdown-blog, plan it."
**Watch for**:
- Does Polly recognize this needs a build pipeline?
- Does she create content tasks (writing blog posts) separately from code tasks?
- Does she plan the deploy step as a final task?

## Stage 14: Human Review Flow
**Do**: Tell Polly: "For the blog project, I want to personally review the blog post content. Use user-review flow for the content tasks."
**Watch for**:
- Polly creates content tasks with `-f user-review`
- When content is ready, it lands at `human_review` node
- Russell is NOT nudged for these
- You can approve from the Tasks detail view ([a] keybinding)

## Stage 15: Recipe Share — Does It Actually Work?
**Do**: Once enough recipe-share tasks are done, try running it.
**Watch for**:
- `cd /Users/sam/dev/recipe-share && uv run uvicorn recipe_share.app:app`
- Homepage loads with recipes
- Search works
- Form submission works
- Responsive on mobile width
- If it doesn't work, that's a quality problem — Russell should have caught it

## Stage 16: Hand Polly the Expense Tracker Spec
**Do**: Send to Polly: "Fourth project. Read /Users/sam/dev/pollypm/docs/project-specs/04-expense-tracker.md, register at /Users/sam/dev/expense-tracker, plan it."
**Watch for**:
- Does Polly recognize auth needs to come first?
- Does she separate the CSV import engine from the UI?
- Does she plan the auto-categorization as its own task?
- Are tasks small enough that a worker can complete each in one session?

## Stage 17: Hold and Resume
**Do**: Tell Polly to put one of the expense-tracker tasks on hold. Wait, then resume it.
**Watch for**: Task moves to on_hold, then back to in_progress (not queued) on resume.

## Stage 18: Cancel a Task
**Do**: Tell Polly to cancel one of the planned tasks with a reason.
**Watch for**: Task moves to cancelled. Polly acknowledges. Dependent tasks stay blocked (cancelled doesn't auto-unblock).

## Stage 19: Hand Polly the Team Standup Spec
**Do**: Send to Polly: "Last project. Read /Users/sam/dev/pollypm/docs/project-specs/05-team-standup.md, register at /Users/sam/dev/team-standup, plan it."
**Watch for**:
- Does Polly recognize the WebSocket and async challenges?
- Does she plan the real-time feed as a distinct task from the submission form?
- Does she separate the email digest from the core app?

## Stage 20: System at Scale — 5 Projects Active
**Do**: Check the main dashboard with 5 projects active.
**Watch for**:
- Dashboard renders quickly (not sluggish)
- "Now" section shows tasks across all 5 projects
- "Ready" section shows queued tasks
- Activity feed shows events from multiple projects
- Rail groups active projects above inactive ones

## Stage 21: GitHub Dashboard — Verify API Integration
**Do**: Once github-dash has enough tasks done, try it.
**Watch for**:
- App starts and fetches real data from your GitHub repos
- Charts render
- Caching works (second load is instant)
- If the gh API integration is broken, Russell missed it

## Stage 22: Inbox and Notifications
**Do**: Check the inbox for messages from Polly.
**Watch for**:
- Polly notifies you when projects complete or need attention
- Inbox tabs work (Open, Agent, Archived, Decisions)
- Can read messages and reply

## Stage 23: Settings Views
**Do**: Click Settings under a project, then global Settings.
**Watch for**: Both render. Project settings show worker info. Global settings show accounts.

## Stage 24: Run the Full Test Suite
**Do**: `uv run pytest tests/ -x -q --ignore=tests/integration/test_knowledge_extract_integration.py --ignore=tests/integration/test_history_import_integration.py`
**Watch for**: All 885 tests pass. No regressions from tonight's changes.

## Stage 25: Demo Readiness Check
**Do**: Step back and assess.
**Watch for**:
- Can you demo the cockpit to someone and it looks good?
- Can you show a project going from spec → tasks → implementation → review → done?
- Can you show Russell rejecting and the rework cycle?
- Can you show working software that was built by the system?
- Is there anything that would embarrass you in a live demo?
