---
name: python-performance-optimization
description: Profile first, optimize second — hot paths, async, vectorization, caching for Python services.
when_to_trigger:
  - slow python
  - profile python
  - python performance
  - speed up python
kind: magic_skill
attribution: https://github.com/skills-sh/skills
---

# Python Performance Optimization

## When to use

Use when a Python service or script is slower than acceptable. Performance work without measurement is gambling — this skill enforces the profile-first discipline and then applies the right lever. Do not optimize speculatively; optimize where the profiler points.

## Process

1. **Measure before touching code.** Wall-clock the whole operation, then profile. `cProfile` for CPU (`python -m cProfile -o out.prof script.py` then view with `snakeviz`). `py-spy record -o flame.svg -- python script.py` for live-running services. `scalene` for CPU+memory+GPU in one pass.
2. **Look at the flamegraph, not the table.** Tables hide the shape. A flat top in a flamegraph is your hot function. If the top is split across many tiny boxes, the problem is call volume — not per-call cost.
3. **Attack the biggest box.** If `json.loads` is 60% of runtime and you optimize a 5% function, you wasted the session. Always the biggest box first.
4. **Pick the right lever:**
   - CPU-bound pure Python hot loop? -> Cython, Numba, or Rust extension via PyO3.
   - Numerical array work? -> NumPy vectorization, Polars over pandas for new code.
   - I/O-bound? -> `asyncio` (single process, many concurrent I/O), `httpx` over `requests`.
   - Repeated computation with same inputs? -> `functools.lru_cache` or a Redis cache.
   - Too many serializations? -> `orjson` beats stdlib `json` 3-10x.
   - Process-bound across cores? -> `concurrent.futures.ProcessPoolExecutor`.
5. **Measure after.** Rerun the same benchmark. If the gain is <20%, revert — the complexity cost of the "optimization" exceeds its value.
6. **Profile in realistic conditions.** Small dev data hides O(n²) pain. Load 100x your dev data or run against production shadow traffic.
7. **Watch for GC pauses.** `gc.set_debug(gc.DEBUG_STATS)` surfaces GC time. Long-lived objects cycle to generation 2 and get rarely scanned; short-lived objects dominate scans. If GC is >5% of runtime, reduce allocation or disable in hot paths (`gc.disable()`-`gc.enable()`).
8. **C extensions as the ceiling.** Before writing one, confirm NumPy / Polars / Rust-via-PyO3 will not do. Maintaining a C extension is a real ongoing cost.

## Example invocation

```bash
# Step 1: measure
$ time python etl.py
real  12m34s

# Step 2: profile
$ py-spy record -o flame.svg --duration 60 -- python etl.py
# Open flame.svg — 72% of time in `row_to_dict` called 12M times.

# Step 3: look at row_to_dict
# Old: nested loops, many dict allocations per row.

# Step 4: pick the lever — pandas -> polars vectorization
# Old
def row_to_dict(rows):
    return [{'id': r[0], 'title': r[1], 'score': float(r[2])} for r in rows]

# New — skip row iteration entirely, operate on columns
import polars as pl
df = pl.read_database('SELECT id, title, score FROM ...', conn)
# Downstream consumer takes a DataFrame directly; no row_to_dict needed.

# Step 5: remeasure
$ time python etl.py
real  1m18s
# 90% reduction. Ship.
```

## Outputs

- A before/after benchmark in the same environment.
- A flamegraph pinpointing the hot path.
- A named lever applied (vectorization, async, cache, extension).
- A commit message citing the measurement: "perf(etl): polars vectorization cuts 12m -> 1m".

## Common failure modes

- Optimizing without profiling; guesses are wrong.
- Optimizing a 2% function for a 10x gain — net wall-clock change: meaningless.
- Adding `asyncio` to CPU-bound code; makes it slower.
- Writing a C extension before trying NumPy; ongoing maintenance cost for no unique gain.
