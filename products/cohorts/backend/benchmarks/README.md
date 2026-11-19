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

`results_postsync.json` holds the 8 cells of Actions run [43321877220](https://github.com/jadibdev/posthog-ci/actions/runs/43321877220) (smoke, then memory, then the two 1M replace cells run separately), measured at `773f3681148`, the PR head after the final sync onto `upstream/master`, with the harness at `efd7d67`, on a 4-vCPU GitHub-hosted runner; 1M at 0% churn took 40.2 and 45.3 s, 1M at 100% took 86.9 and 95.1 s, and the replace's own memory was 16.8–25.1 MB.

`results_10m.json` holds the 10M replace cell at 0% churn from Actions run [43474445082](https://github.com/jadibdev/posthog-ci/actions/runs/43474445082) (run separately after the smoke cells via `bench_cells=replace:10000000:0.0`), measured at `773f3681148`, the PR head before the review fixes, with the harness at `d342109` on a 4-vCPU GitHub-hosted runner (`posthog-ci` at `4f1688d`, `insert_quorum = 1`, local `personhog`, synthetic people, 1 sample); replace took 959.1 s, ClickHouse was exact 4.9 s after the replace finished with 1 ClickHouse mutation (`postgres_exact = true`), and the replace's own memory (`replace_rss_mb`) was 17.7 MB (process `peak_rss_mb` 982.6 MB). The rerun after the review fixes is `results_10m_final.json`, from Actions run [43734394520](https://github.com/jadibdev/posthog-ci/actions/runs/43734394520) via `bench_cells=replace:10000000:0.0`, measured at `ba59b3768cf` (before the rebase onto master; the rebased PR head `3e184fbd34f` adds the staff re-sync guard and MCP changes, which aren't on the replace path), with the harness at `52dc4ea325f` on a 4-vCPU GitHub-hosted runner (local `personhog`, synthetic people, 1 sample); replace took 534.7 s, ClickHouse read 488,156,138 rows during the replace (8.57B before), ClickHouse was exact 5.3 s after the replace finished with 1 ClickHouse mutation (`postgres_exact = true`), and the replace's own memory (`replace_rss_mb`) was 43.8 MB (process `peak_rss_mb` 1018.1 MB).

`results_10m_hashed.json` holds the 10M replace cell at 0% churn with hashed person UUIDs (matched identical in Postgres and ClickHouse before seeding) from Actions run [44321946072](https://github.com/jadibdev/posthog-ci/actions/runs/44321946072) (via `bench_cells=replace:10000000:0.0`), measured at `5ab451d68de` with the harness at `3b7e1a33cc1` on a 4-vCPU GitHub-hosted runner (`insert_quorum = 1`, local `personhog`, synthetic people, 1 sample); replace took 676.2 s (`insert_cohort_people_into_pg` 368.81 s), the 200 membership checks read 2,888,047,000 ClickHouse rows (109.23 s ClickHouse time / 176.07 s Python time; 3,386,897,041 total `Select` rows read), ClickHouse was exact 4.9 s after the replace finished with 1 ClickHouse mutation (`postgres_exact = true`), 289 retries were 0, and the replace's own memory (`replace_rss_mb`) was 40.5 MB (process `peak_rss_mb` 1026.2 MB).

`results_10m_hashed_r5.json` holds the review 5 rerun of the 10M hashed-UUID replace cell at 0% churn (with 4-worker person UUID lookup fan-out and per-page 289 retry) from Actions run [44588380147](https://github.com/jadibdev/posthog-ci/actions/runs/44588380147) (via `bench_cells=replace:10000000:0.0`), measured at `d28770530ee` with the harness at `3bbade6e1ab` on a 4-vCPU Intel Xeon 6975P-C GitHub-hosted runner (`insert_quorum = 1`, local `personhog`, synthetic people, 1 sample); replace took 542.7 s (`insert_cohort_people_into_pg` 314.62 s, `prune_members_absent_from_replace` 214.97 s), the 200 membership checks read 2,888,047,000 ClickHouse rows (87.87 s ClickHouse time / 139.54 s Python time; 3,386,897,041 total `Select` rows read), ClickHouse was exact 4.5 s after the replace finished with 1 ClickHouse mutation (`postgres_exact = true`), 289 retries were 0 across 2,202 quorum read calls, and the replace's own memory (`replace_rss_mb`) was 61.2 MB (process `peak_rss_mb` 1047.9 MB).

`results_10m_churn10.json` holds the 10M replace cell at 10% churn from Actions run [43484430296](https://github.com/jadibdev/posthog-ci/actions/runs/43484430296) (run separately after the smoke cells via `bench_cells=replace:10000000:0.1`), measured at `773f3681148`, the PR head before the review fixes, with the harness at `d342109` on a 4-vCPU GitHub-hosted runner (`posthog-ci` at `4f1688d`, `insert_quorum = 1`, local `personhog`, synthetic people, 1 sample); replace took 987.0 s, ClickHouse was exact 4.2 s after the replace finished with 1 ClickHouse mutation (`postgres_exact = true`), and the replace's own memory (`replace_rss_mb`) was 18.5 MB (process `peak_rss_mb` 984.1 MB). It wasn't re-run after the review fixes; the 0% churn cell was.

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
