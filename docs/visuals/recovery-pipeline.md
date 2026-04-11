# Recovery Pipeline

Escalation from failure detection through recovery to degraded state.

```mermaid
stateDiagram-v2
    [*] --> Detection: Heartbeat detects failure

    state Detection {
        [*] --> Classify
        Classify --> PaneDead: pane_dead
        Classify --> MissingWindow: missing_window
        Classify --> AuthBroken: auth_broken
        Classify --> ProviderOutage: provider_outage
    }

    Detection --> RecoverImmediately: pane_dead / missing_window
    Detection --> TryDifferentAccount: auth_broken
    Detection --> BlockAndRetry: provider_outage (10min backoff)

    state Recovery ["_maybe_recover_session"] {
        [*] --> CheckLease
        CheckLease --> Deferred: Human holds lease
        CheckLease --> RateLimit: No lease conflict
        RateLimit --> Blocked5per30: Over 5 in 30min window
        RateLimit --> HardLimit: Under rate limit
        HardLimit --> STOP: Over 20 total attempts
        HardLimit --> BuildCandidates: Under hard limit

        state BuildCandidates {
            [*] --> SameProvider: Try same provider first
            SameProvider --> CrossProvider: If same fails
        }

        BuildCandidates --> RestartSession: _restart_session
        RestartSession --> KillOldWindow
        KillOldWindow --> LaunchNew
    }

    RecoverImmediately --> Recovery
    TryDifferentAccount --> Recovery
    BlockAndRetry --> Recovery

    Recovery --> Success: Session recovered
    Recovery --> Failure: Recovery failed

    Success --> ClearAlerts: Clear alerts + update runtime
    Failure --> Reschedule: Recurring failure
    Failure --> Degraded: Permanent failure

    ClearAlerts --> [*]
    Reschedule --> Detection: Retry on next sweep
    Degraded --> [*]: Session marked degraded
```

## Operator Recovery: Case Study

The operator session (Polly) demonstrates the pipeline at its limits:

```mermaid
flowchart LR
    A["Auth expired<br/>Claude Keychain"] --> B["139 recovery<br/>attempts"]
    B --> C["Hard limit<br/>exceeded (20)"]
    C --> D["Session DOWN<br/>Degraded state"]
    D --> E["Needs manual<br/>re-auth fix"]

    style A fill:#e74c3c,stroke:#c0392b,color:#fff
    style B fill:#f39c12,stroke:#e67e22,color:#fff
    style C fill:#e74c3c,stroke:#c0392b,color:#fff
    style D fill:#7f8c8d,stroke:#95a5a6,color:#fff
    style E fill:#3498db,stroke:#2980b9,color:#fff
```

## Rate Limits

| Guard | Threshold | Effect |
|-------|-----------|--------|
| Lease check | Human holds lease | Defer recovery |
| Rate limit | 5 per 30min window | Queue for later |
| Hard limit | 20 total attempts | STOP permanently |
