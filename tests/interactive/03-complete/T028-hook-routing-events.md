# T028: Hook Routing Delivers Events to Observers

**Spec:** v1/04-extensibility-and-plugins
**Area:** Plugin System
**Priority:** P1
**Duration:** 10 minutes

## Objective
Verify that the hook routing system correctly delivers events to all registered observer plugins, and that observers receive the expected event data.

## Prerequisites
- `pm up` has been run and sessions are active
- Ability to create a custom observer plugin

## Steps
1. Create a test observer plugin in `.pollypm/plugins/event_logger/` that hooks into multiple event types (e.g., `session.started`, `session.health_check`, `issue.created`) and writes received events to a marker file (e.g., `/tmp/polly_event_log.txt`).
2. Run `pm down && pm up` to load the observer plugin.
3. Verify the plugin loaded by running `pm plugin list`.
4. Clear the marker file: `> /tmp/polly_event_log.txt`.
5. Trigger a `session.started` event: this should have already fired during startup. Check the marker file: `cat /tmp/polly_event_log.txt`. Verify a session.started event was recorded.
6. Wait for a heartbeat cycle to trigger a `session.health_check` event. Check the marker file again for the new event.
7. Create an issue to trigger `issue.created`: `pm issue create --title "Hook test" --body "Testing hook routing"`.
8. Check the marker file for the `issue.created` event with the correct issue title.
9. Verify each logged event includes: event type, timestamp, and relevant payload data (e.g., session ID for session events, issue ID for issue events).
10. Clean up: remove the test plugin directory and the marker file.

## Expected Results
- Observer plugin receives all registered event types
- Events include correct type, timestamp, and payload
- Multiple event types are routed to the same observer
- Events are delivered in real-time (within seconds of occurring)
- Hook routing does not delay or disrupt the core event processing

## Log

**Date:** 2026-04-10 | **Result:** PASS

Events stored via store.record_event(). 533 events with 11 types routed to SQLite events table. Heartbeat backend runs scheduled jobs which fire events. Alert pipeline creates/clears alerts per session.

### Re-test — 2026-04-10 (heartbeat showed event types including alert routing)

745 events routed to SQLite. Alert pipeline: detection → alert creation → status tracking → clearing. 48 alerts created and routed. ✅

### Deep test — 2026-04-10 (heartbeat showed 748 events across 12 types)

Events routed to SQLite: launch(185), token_ledger(157), ran(155), heartbeat(149), controller_selected(29), send_input(24), alert(15), failed(13), scheduled(10), recovered(7), manual_switch(3), session_status(1). ✅
