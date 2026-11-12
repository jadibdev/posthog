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
| `results_memory.json`     | The three replace cells with the replace's own memory sampled (see Memory).                                                 |

`python summarize.py results.json` prints one line per cell.

`results_rebased.json` holds the 8 cells of Actions run [43232873400](https://github.com/jadibdev/posthog-ci/actions/runs/43232873400) (smoke, then memory, then the two 1M replace cells run separately), measured at `2c8926e3797`, the PR head after the rebase onto `upstream/master`, with the harness at `2a9e616`, on a 4-vCPU GitHub-hosted runner rather than the codespace; across that run and run [43208223597](https://github.com/jadibdev/posthog-ci/actions/runs/43208223597) at the same commit, 1M at 0% churn took 49.6–57.7 s, 1M at 100% took 116.2–128.8 s, and the replace's own memory was 21–25 MB.

Headline figures:

- **Removing 100k stale members:** per-person took 311–318 s. Bulk took 0.36–0.40 s in three repeats, about 850× faster. The slowest of four bulk runs took 2.42 s, still 129× faster.
- **Replacing 1M members at 0% churn:** about 48 s. Seven of eight samples fell in 47–54 s.
- **Replacing 1M members at 100% churn:** about 123 s.
- **Memory:** the replace's own memory is 21–27 MB, flat from 10k to 1M members. The process around it holds about 940 MB before the replace starts, so the bar as first written fails; see the section below.

## Memory

The bar written before the first run was a peak worker RSS of 512 MB or less. As written, it fails in this harness: the dev-mode Django process holds 939–965 MB before the replace starts, and peaked at 961–986 MB. That absolute figure measures the process, not the replace, so the bar was amended after the run to the replace's own memory, with both readings reported.

The first runs recorded the process's lifetime peak (`ru_maxrss`), and found the replace raised it by 22–30 MB, flat from 10k to 1M members. A lifetime peak hides any use below an earlier high point, so that is a lower bound. The `memory` mode measures the replace directly: a thread samples current RSS every 50 ms while it runs, and the result is that peak minus current RSS just before it.

Measured that way (`results_memory.json`, run from a fresh worktree of this branch by following this README):

| Cell           | RSS before the replace | Peak during it | The replace's own memory | Samples |
| -------------- | ---------------------: | -------------: | -----------------------: | ------: |
| 10k, 5% churn  |                 940 MB |         961 MB |                    21 MB |      49 |
| 1M, 0% churn   |                 940 MB |         962 MB |                    22 MB |     835 |
| 1M, 100% churn |                 940 MB |         967 MB |                    27 MB |   2,278 |

The replace's own memory is flat from 10k to 1M members, which is the O(page) behavior the bar exists to check, and far inside 512 MB. The absolute bar, as written, still fails: the process itself sits at about 940 MB before the replace starts. A peak shorter than the 50 ms sampling interval could fall between samples, but the lifetime-peak readings from the first runs agree with these figures.

## Notes on the raw results

- `clickhouse_work` in `results.json`, `results_repeats.json` and `results_extra_1m_0.json` comes from a whole-second `query_log` window. It can include the harness's own seed inserts when they finish in the same second the replace starts. The harness now excludes its own queries by tag. No headline figure uses this field.

## How to run

You need:

- the PostHog dev stack, migrated with `bin/hogli migrations:run` (it needs `sqlx` on `PATH` to migrate the persons database);
- the PostHog Python environment, either active or as `.venv` in the checkout;
- a release build of `personhog-replica` and `personhog-router` from this branch, in `/tmp/cargo-target/release`:

  ```bash
  cd rust && CARGO_TARGET_DIR=/tmp/cargo-target cargo build --release -p personhog-replica -p personhog-router
  ```

- the Rust toolchain, to build them.

Set `CLICKHOUSE_DATABASE` to the ClickHouse database the dev stack was migrated into. It defaults to `default`. A stack migrated through `hogli` uses `posthog`.

`run_bench.sh` works from the checkout that holds it, including a fresh worktree of this branch. It starts the replica and router if they aren't running, sets the environment, and runs one mode:

```bash
products/cohorts/backend/benchmarks/run_bench.sh smoke     # three quick cells
products/cohorts/backend/benchmarks/run_bench.sh all       # the full matrix
products/cohorts/backend/benchmarks/run_bench.sh repeats   # variance repeats
products/cohorts/backend/benchmarks/run_bench.sh memory    # the replace's own memory, sampled
```

## Where this differs from production

- **ClickHouse is one node.** The insert runs with `insert_quorum=1`, where production waits for a majority of replicas.
- **Every personhog call is local.** In production each call crosses the network. That costs the per-person path 100,000 round trips at 100k and bulk only 20, so the real gap is likely wider.
- **The settle wait is forced to one second,** its production value. Dev and test settings zero it.
- **The persons are synthetic,** with flat properties.
- **The machine is a 4-core, 16 GB GitHub Codespace.**
