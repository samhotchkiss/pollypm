---
name: ios-simulator
description: Build, launch, and test iOS apps in the Xcode simulator from the command line.
when_to_trigger:
  - ios
  - swift
  - iphone simulator
  - xcode build
  - ios test
kind: magic_skill
attribution: https://github.com/skills-sh/skills
---

# iOS Simulator

## When to use

Use when you need to build, launch, or test an iOS app without opening Xcode's GUI. Reach for this whenever you are iterating on a Swift codebase from an agent context — CI pipelines, one-off integration checks, or UI automation sessions. Skip for initial scaffolding; generate with Xcode once, then automate after.

## Process

1. List available simulators: `xcrun simctl list devices available`. Pick a stable target (iPhone 15 on latest iOS). Record the UDID — you will script against it, not against a name that changes.
2. Boot the simulator: `xcrun simctl boot <UDID>`. Open the Simulator.app to see it: `open -a Simulator`.
3. Build for the simulator destination: `xcodebuild -workspace App.xcworkspace -scheme App -configuration Debug -destination "platform=iOS Simulator,id=<UDID>" -derivedDataPath build/`. Cache `build/` between runs to avoid clean rebuilds.
4. Install the built `.app`: `xcrun simctl install <UDID> build/Build/Products/Debug-iphonesimulator/App.app`.
5. Launch with launch args: `xcrun simctl launch --console-pty <UDID> com.company.app -- ARG1 VALUE1`. `--console-pty` streams stdout so you see logs.
6. For UI tests: `xcodebuild test -workspace App.xcworkspace -scheme AppUITests -destination "platform=iOS Simulator,id=<UDID>"`. Use the `-resultBundlePath` flag to capture a `.xcresult` you can inspect.
7. Capture screenshots: `xcrun simctl io <UDID> screenshot screenshot.png`. For video: `xcrun simctl io <UDID> recordVideo output.mp4` (Ctrl-C to stop).
8. Tear down: `xcrun simctl shutdown <UDID>` (keeps data) or `xcrun simctl erase <UDID>` (factory reset). Erase between test runs that need clean state.

## Example invocation

```bash
UDID=$(xcrun simctl list devices available | grep "iPhone 15 " | head -1 | grep -oE '[0-9A-F-]{36}')
xcrun simctl boot "$UDID"

xcodebuild \
  -workspace App.xcworkspace \
  -scheme App \
  -configuration Debug \
  -destination "platform=iOS Simulator,id=$UDID" \
  -derivedDataPath build/ \
  build

xcrun simctl install "$UDID" build/Build/Products/Debug-iphonesimulator/App.app
xcrun simctl launch --console-pty "$UDID" com.company.app
xcrun simctl io "$UDID" screenshot ./.pollypm/artifacts/task-47/home.png
xcrun simctl shutdown "$UDID"
```

## Outputs

- Built `.app` at `build/Build/Products/Debug-iphonesimulator/App.app`.
- Launch logs captured (via `--console-pty`).
- Screenshots / video in the task artifact directory.
- `.xcresult` bundle from test runs.

## Common failure modes

- Referencing simulators by name rather than UDID — names change with Xcode updates.
- Clean rebuilds every run because `derivedDataPath` is not cached; adds minutes per iteration.
- Launching without `--console-pty`; no logs, no debugging.
- Forgetting to erase the simulator between test runs that depend on clean state.
