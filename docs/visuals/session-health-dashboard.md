# Session Health Dashboard

Current state of all 5 managed sessions as of April 11, 2026.

```mermaid
block-beta
    columns 5

    block:hb:1
        columns 1
        hb_name["pm-heartbeat"]
        hb_provider["Claude 2.1.101"]
        hb_status["HEALTHY"]
    end

    block:w1:1
        columns 1
        w1_name["worker-pollypm"]
        w1_provider["Codex gpt-5.4"]
        w1_status["IDLE"]
    end

    block:w2:1
        columns 1
        w2_name["worker-otter_camp"]
        w2_provider["Codex gpt-5.4"]
        w2_status["IDLE"]
    end

    block:w3:1
        columns 1
        w3_name["worker-pollypm-web"]
        w3_provider["Codex gpt-5.4"]
        w3_status["IDLE"]
    end

    block:op:1
        columns 1
        op_name["operator (Polly)"]
        op_provider["Claude"]
        op_status["DOWN"]
    end

    style hb_status fill:#2ecc71,color:#fff
    style w1_status fill:#f1c40f,color:#000
    style w2_status fill:#f1c40f,color:#000
    style w3_status fill:#f1c40f,color:#000
    style op_status fill:#e74c3c,color:#fff
```

## Detailed Status

```mermaid
flowchart LR
    subgraph Healthy
        HB["pm-heartbeat<br/>Sweeping every 60s<br/>16,569 heartbeats recorded"]
    end

    subgraph Idle["Idle (Awaiting Work)"]
        W1["worker-pollypm<br/>cwd: /Users/sam/dev/pollypm"]
        W2["worker-otter_camp<br/>cwd: worktrees/otter_camp-pa"]
        W3["worker-pollypm-web<br/>cwd: pollypm-website"]
    end

    subgraph Down
        OP["operator (Polly)<br/>139 recovery attempts<br/>Auth expired - needs re-login"]
    end

    subgraph Alerts["Open Alerts (8)"]
        A1["Operator auth_broken"]
        A2["Operator recovery_exhausted"]
        A3["Various session alerts"]
    end

    HB -.->|"monitors"| W1
    HB -.->|"monitors"| W2
    HB -.->|"monitors"| W3
    HB -.->|"monitors"| OP
    OP -.-x|"cannot manage"| Idle

    style HB fill:#2ecc71,stroke:#27ae60,color:#fff
    style W1 fill:#3498db,stroke:#2980b9,color:#fff
    style W2 fill:#3498db,stroke:#2980b9,color:#fff
    style W3 fill:#3498db,stroke:#2980b9,color:#fff
    style OP fill:#e74c3c,stroke:#c0392b,color:#fff
    style A1 fill:#e74c3c,stroke:#c0392b,color:#fff
    style A2 fill:#e74c3c,stroke:#c0392b,color:#fff
    style A3 fill:#f39c12,stroke:#e67e22,color:#fff
```

## Scheduler Status

```mermaid
gantt
    title Active Scheduler Jobs
    dateFormat X
    axisFormat %s

    section Heartbeat
    heartbeat-1  :active, 0, 60
    heartbeat-2  :active, 60, 120
    heartbeat-3  :active, 120, 180
    heartbeat-4  :active, 180, 240
    heartbeat-5  :active, 240, 300
    heartbeat-6  :active, 300, 360
    heartbeat-7  :active, 360, 420

    section Knowledge
    extract-1    :active, 0, 900
    extract-2    :active, 900, 1800
    extract-3    :active, 1800, 2700
```

Note: 7 duplicate heartbeat jobs exist due to missing dedup on cockpit restart. Should be 1.
