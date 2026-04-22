# Launch Readiness

What is ready, what is blocked, and what is the path to launch.

```mermaid
pie title Launch Readiness (11 solid, 3 must-fix, 3 should-have, 4 nice-to-have)
    "Already Solid" : 11
    "Must Have (Blocked)" : 3
    "Should Have (Partial)" : 3
    "Nice to Have" : 4
```

## Must Have -- Blockers

```mermaid
flowchart TD
    subgraph blockers["MUST FIX (Not Ready)"]
        B1["Operator session working<br/>Needs re-auth or Codex fallback<br/>P0 | ~1 day"]
        B2["Scheduler dedup<br/>7 duplicate heartbeat jobs<br/>P0 | ~2 hours"]
        B3["Cockpit state cleanup<br/>Stale state blocks recovery<br/>P1 | ~2 hours"]
    end

    B1 -->|"enables"| Autonomous["Autonomous<br/>Operation"]
    B2 -->|"enables"| Autonomous
    B3 -->|"enables"| Autonomous

    Autonomous -->|"unlocks"| Launch["LAUNCH READY"]

    style B1 fill:#e74c3c,stroke:#c0392b,color:#fff
    style B2 fill:#e74c3c,stroke:#c0392b,color:#fff
    style B3 fill:#f39c12,stroke:#e67e22,color:#fff
    style Autonomous fill:#2ecc71,stroke:#27ae60,color:#fff
    style Launch fill:#2ecc71,stroke:#27ae60,color:#fff
```

## Already Solid (11 components)

```mermaid
flowchart LR
    subgraph solid["READY"]
        S1["Core tmux<br/>management"]
        S2["Heartbeat<br/>monitoring"]
        S3["Recovery<br/>pipeline"]
        S4["Knowledge<br/>extraction"]
        S5["Token/cost<br/>tracking"]
        S6["File-based<br/>issue tracker"]
        S7["Account<br/>isolation"]
        S8["402-test<br/>suite"]
        S9["Cockpit TUI<br/>navigation"]
        S10["Onboarding<br/>flow"]
        S11["5 managed<br/>projects"]
    end

    style S1 fill:#2ecc71,stroke:#27ae60,color:#fff
    style S2 fill:#2ecc71,stroke:#27ae60,color:#fff
    style S3 fill:#2ecc71,stroke:#27ae60,color:#fff
    style S4 fill:#2ecc71,stroke:#27ae60,color:#fff
    style S5 fill:#2ecc71,stroke:#27ae60,color:#fff
    style S6 fill:#2ecc71,stroke:#27ae60,color:#fff
    style S7 fill:#2ecc71,stroke:#27ae60,color:#fff
    style S8 fill:#2ecc71,stroke:#27ae60,color:#fff
    style S9 fill:#2ecc71,stroke:#27ae60,color:#fff
    style S10 fill:#2ecc71,stroke:#27ae60,color:#fff
    style S11 fill:#2ecc71,stroke:#27ae60,color:#fff
```

## Estimated Path to Launch

```mermaid
gantt
    title Path to Launch (~1.5 days)
    dateFormat YYYY-MM-DD

    section P0 Blockers
    Fix operator re-auth           :crit, op, 2026-04-11, 1d
    Deduplicate scheduler jobs     :crit, sched, 2026-04-11, 2h

    section P1 Blockers
    Cockpit state cleanup          :active, cockpit, after sched, 2h

    section Validation
    End-to-end autonomous test     :milestone, after cockpit, 0d
```

## Verdict

**Not launch-ready yet.** The operator crash loop (139 failed recoveries) and the remaining operational gaps mean the system cannot run autonomously. A human must still babysit sessions. Fixing the remaining top blockers unblocks autonomous multi-project operation.
