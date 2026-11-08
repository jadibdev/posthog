# Static cohort replace benchmark

This branch measures the explicit replace option for query-populated static cohorts ([#121486](https://github.com/PostHog/posthog/issues/121486)), as built at `55f323a`.
It is not part of CI and not part of the replace PR: the PR links here so the numbers it quotes can be checked and re-run.

## What it measures

- **Removal:** removes the same stale members from Postgres two ways.
  One is the existing per-person delete in a loop, a baseline that never ships.
  The other is the bulk `DeleteCohortMembersByPersonIds` RPC, one call per page.
- **Replace:** a full replace population, end to end, through the real `insert_cohort_from_query` task.
  It covers cohort sizes of 10k, 100k and 1M members at 0%, 5%, 50% and 100% churn.
  Each cell checks:
  - that both stores end exact;
  - that there's exactly one ClickHouse mutation;
  - that the run fields are cleared and `is_calculating` is released;
  - the added memory, and how long ClickHouse takes to read exact.
- **Repeats:** the five key cells, run three times in interleaved rounds to measure run-to-run variance.

## Results

| File                      | Contents                                                                                                                    |
| ------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `results.json`            | First full run: 20 cells, each once.                                                                                        |
| `results_repeats.json`    | Three interleaved rounds of the five key cells.                                                                             |
| `results_extra_1m_0.json` | Four extra runs of the 1M replace at 0% churn, after one repeat came in slow (102 s; the other seven samples took 47–54 s). |

`python summarize.py results.json` prints one line per cell.

Headline figures:

- **Removing 100k stale members:** per-person took 311–318 s. Bulk took 0.36–0.40 s in three repeats, about 850× faster. The slowest of four bulk runs took 2.42 s, still 129× faster.
- **Replacing 1M members at 0% churn:** about 48 s. Seven of eight samples fell in 47–54 s.
- **Replacing 1M members at 100% churn:** about 123 s.
- **Added memory:** at most 30 MB in any cell.

## How to run

You need:

- the PostHog dev stack;
- a release build of `personhog-replica` and `personhog-router` from this branch, in `/tmp/cargo-target/release`;
- the Rust toolchain on `PATH`.

`run_bench.sh` starts the replica and router if they aren't running, sets the environment, and runs one mode:

```bash
products/cohorts/backend/benchmarks/run_bench.sh smoke     # three quick cells
products/cohorts/backend/benchmarks/run_bench.sh all       # the full matrix
products/cohorts/backend/benchmarks/run_bench.sh repeats   # variance repeats
```

## Where this differs from production

- **ClickHouse is one node.** The insert runs with `insert_quorum=1`, where production waits for a majority of replicas.
- **Every personhog call is local.** In production each call crosses the network. That costs the per-person path 100,000 round trips at 100k and bulk only 20, so the real gap is likely wider.
- **The settle wait is forced to one second,** its production value. Dev and test settings zero it.
- **The persons are synthetic,** with flat properties.
- **The machine is a 4-core, 16 GB GitHub Codespace.**
