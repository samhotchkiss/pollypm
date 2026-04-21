# Plugin Trust Model

PollyPM plugins, provider packages, and other extension entry points run in-process with the same user privileges as PollyPM itself. A third-party extension can read and write your files, inspect PollyPM state, spawn subprocesses, access the network, and change runtime behavior or prompts. PollyPM v1 does not sandbox, sign, or capability-restrict extensions, so only install plugins and providers you trust and review them like any other code dependency.
