---
name: load-test
description: Simple load test scripting with k6 or Locust — define profile, run, interpret results.
when_to_trigger:
  - load test
  - stress test
  - throughput
  - benchmark api
  - performance test
kind: magic_skill
attribution: https://github.com/grafana/k6
---

# Load Test

## When to use

Use when you need to know how a service behaves under load — before a launch, before a traffic spike, or when debugging a latency regression. Do not load-test as a substitute for profiling; if the bottleneck is a single hot function, a profiler finds it faster than a load test.

## Process

1. Pick the tool. **k6** (JavaScript, single binary, Prometheus output) is the default. **Locust** (Python, web UI, distributed) when you want ad-hoc interactive runs. Never use `ab` except for 30-second sanity checks.
2. Define the **profile** before the script. Four shapes cover most cases:
   - **Smoke**: 1 VU for 1 min — does the endpoint respond at all?
   - **Load**: target RPS held for 10 min — does it meet SLO under realistic traffic?
   - **Stress**: ramp past target to find the knee — where does it fall over?
   - **Soak**: target RPS for 4+ hours — does it leak or drift?
   Pick one per run; do not combine.
3. Write a realistic scenario, not just `GET /health`. Real users call 3-5 endpoints per session, with pauses. Model that.
4. Use **thresholds** to make the test pass/fail, not just print graphs. `http_req_duration: ['p(95)<500']` fails the test if p95 > 500ms. A load test without thresholds is a graph, not a check.
5. Run from a machine with headroom. If your client is CPU-starved, you are measuring your client. Use a separate host or a k6 Cloud run for anything over 500 RPS.
6. Capture server-side metrics during the run: CPU, memory, DB query rate, error rate. Graph them alongside the load generator output — the correlation is where the story is.
7. Interpret results in plain language: "p95 = 340ms at 500 RPS sustained; CPU hit 70% on the API tier, DB idle. Can push to 750 RPS. Knee: at ~900 RPS the connection pool saturates and p95 spikes to 2s."
8. File a follow-up issue with the specific bottleneck if you found one; otherwise note the capacity and move on.

## Example invocation

```javascript
// k6 load test — 500 RPS sustained for 10 min
import http from 'k6/http';
import { check, sleep } from 'k6';

export const options = {
  scenarios: {
    load: {
      executor: 'constant-arrival-rate',
      rate: 500,
      timeUnit: '1s',
      duration: '10m',
      preAllocatedVUs: 100,
      maxVUs: 300,
    },
  },
  thresholds: {
    http_req_failed: ['rate<0.01'],      // <1% errors
    http_req_duration: ['p(95)<500'],    // p95 < 500ms
  },
};

export default function () {
  const list = http.get('https://api.example.com/tasks');
  check(list, { '200 list': (r) => r.status === 200 });

  const show = http.get('https://api.example.com/tasks/abc123');
  check(show, { '200 show': (r) => r.status === 200 });

  sleep(Math.random() * 2);
}
```

```bash
k6 run --out prometheus=http://prom:9090 load.js
```

## Outputs

- A k6 or Locust script checked into the repo under `tests/load/`.
- A run report: profile, duration, thresholds, pass/fail.
- Server-side metrics correlated with the client-side graph.
- A plain-language summary of the capacity and the knee.

## Common failure modes

- Load-testing from your laptop and measuring your laptop's CPU.
- No thresholds; the test always "passes" because nothing was asserted.
- Hitting `/health` only; finds no real bottlenecks.
- Reading only the load-tool graph and missing server-side exhaustion signals.
