# Heartbeat Sweep Flow

How the heartbeat monitors all sessions every 60 seconds.

```mermaid
flowchart TD
    A["Cockpit scheduler tick<br/>(every 5s)"] --> B{"run_due()<br/>checks jobs.json"}
    B -->|"not due"| A
    B -->|"due (60s interval)"| C["supervisor.run_heartbeat()"]

    C --> D["sync_token_ledger<br/>Ingest transcript JSONL"]

    C --> E["For each session"]

    E --> F["Capture pane snapshot<br/>tmux capture-pane"]
    F --> G["Hash for change detection"]
    G --> H{"Classify session"}

    H -->|"active output"| I["HEALTHY"]
    H -->|"no change, prompt visible"| J["NEEDS_FOLLOWUP"]
    H -->|"error message / auth fail"| K["BLOCKED"]
    H -->|"task complete signal"| L["DONE"]

    I --> M["Record heartbeat +<br/>level0 checkpoint<br/>in SQLite"]
    J --> M
    K --> M
    L --> M

    M --> N{"Alert needed?"}
    N -->|"yes"| O["Raise alert<br/>(208 total, 8 open)"]
    N -->|"no"| P["Clear existing alert<br/>if resolved"]

    O --> Q{"Needs recovery?"}
    P --> R["Next session"]
    Q -->|"no"| R
    Q -->|"yes"| S["Queue followup<br/>to operator"]
    S --> R

    R --> E

    R -->|"all sessions done"| T["Record event:<br/>'Heartbeat sweep completed<br/>with N open alerts'"]

    style A fill:#f39c12,stroke:#e67e22,color:#fff
    style C fill:#2ecc71,stroke:#27ae60,color:#fff
    style H fill:#e74c3c,stroke:#c0392b,color:#fff
    style I fill:#2ecc71,stroke:#27ae60,color:#fff
    style J fill:#f1c40f,stroke:#f39c12,color:#000
    style K fill:#e74c3c,stroke:#c0392b,color:#fff
    style L fill:#3498db,stroke:#2980b9,color:#fff
    style T fill:#9b59b6,stroke:#8e44ad,color:#fff
```

## Key Numbers

| Metric | Value |
|--------|-------|
| Heartbeat records | 16,569 |
| Level 0 checkpoints | 16,528 |
| Lifecycle events | 13,063 |
| Total alerts raised | 208 |
| Currently open alerts | 8 |
| Token samples | 227 |
| Hourly aggregations | 72 |
