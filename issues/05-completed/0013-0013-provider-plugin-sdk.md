# 0013 Provider Plugin SDK

## Goal

Define and implement the first provider-plugin API so additional CLI tools like Gemini can be added cleanly.

## Scope

- provider manifest and loader integration
- launch/resume hooks
- transcript discovery hooks
- health/usage parsing hooks
- local third-party plugin example path

## Acceptance Criteria

- A user can add a provider plugin locally without editing PollyPM core.
- The extension path is compatible with upstream PRs back to PollyPM.
